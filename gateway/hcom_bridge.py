"""Opt-in hcom ingress for long-running gateway sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

from gateway.config import HcomBridgeConfig, HcomConfig
from gateway.platforms.base import MessageEvent, ProcessingOutcome
from gateway.session import SessionSource
from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)

_HCOM_DIR: ContextVar[object] = ContextVar("gateway_hcom_dir")
_HCOM_AGENT_CATALOGS: ContextVar[object] = ContextVar("gateway_hcom_agent_catalogs")
_UNSET = object()
_COMPLETED_LIMIT = 2048


def subprocess_env_values() -> dict[str, str]:
    """Return hcom values bound to the current gateway turn, if any."""
    values: dict[str, str] = {}
    for name, var in (
        ("HCOM_DIR", _HCOM_DIR),
        ("HCOM_AGENT_CATALOGS", _HCOM_AGENT_CATALOGS),
    ):
        value = var.get(_UNSET)
        if value is not _UNSET:
            values[name] = str(value)
    return values


@contextmanager
def scoped_subprocess_env(values: Mapping[str, str]) -> Iterator[None]:
    tokens: list[tuple[ContextVar[object], Token[object]]] = []
    try:
        tokens.append((_HCOM_DIR, _HCOM_DIR.set(values.get("HCOM_DIR", ""))))
        tokens.append(
            (
                _HCOM_AGENT_CATALOGS,
                _HCOM_AGENT_CATALOGS.set(values.get("HCOM_AGENT_CATALOGS", "")),
            )
        )
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


@contextmanager
def cleared_subprocess_env() -> Iterator[None]:
    """Prevent delegate_task children from inheriting the parent hcom route."""
    with scoped_subprocess_env({}):
        yield


@dataclass(frozen=True)
class HcomEnvelope:
    event_id: int
    sender: str
    text: str
    intent: str
    thread: Optional[str]
    reply_to: Optional[int]
    timestamp: Optional[str]
    recipients: Any
    bundle_id: Optional[str]

    @classmethod
    def from_json(cls, raw: str) -> "HcomEnvelope":
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("hcom envelope must be an object")
        if data.get("schema_version") not in (1, "1"):
            raise ValueError("unsupported hcom envelope schema_version")
        event_id = int(data["event_id"])
        sender = str(data.get("from") or "").strip()
        text = data.get("text")
        if event_id < 1 or not sender or not isinstance(text, str):
            raise ValueError("hcom envelope requires event_id, from, and text")
        reply_to = data.get("reply_to")
        return cls(
            event_id=event_id,
            sender=sender,
            text=text,
            intent=str(data.get("intent") or "inform"),
            thread=str(data["thread"]) if data.get("thread") is not None else None,
            reply_to=int(reply_to) if reply_to is not None else None,
            timestamp=str(data["timestamp"]) if data.get("timestamp") is not None else None,
            recipients=data.get("to"),
            bundle_id=str(data["bundle_id"]) if data.get("bundle_id") is not None else None,
        )

    def event_text(self, identity: str) -> str:
        attrs = [
            f"from={self.sender}",
            f"to={identity}",
            f"event_id={self.event_id}",
            f"intent={self.intent}",
        ]
        if self.thread:
            attrs.append(f"thread={self.thread}")
        if self.reply_to is not None:
            attrs.append(f"reply_to={self.reply_to}")
        return f"<hcom {' '.join(attrs)}>\n{self.text}\n</hcom>"


class CompletedEventStore:
    def __init__(self, path: Path, limit: int = _COMPLETED_LIMIT):
        self.path = path
        self.limit = limit
        self._items: "OrderedDict[str, None]" = OrderedDict()
        self._load()

    @staticmethod
    def key(identity: str, event_id: int) -> str:
        return f"{identity}:{event_id}"

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for key in data.get("completed", []):
                self._items[str(key)] = None
        except FileNotFoundError:
            return
        except Exception as exc:
            raise RuntimeError(f"cannot read hcom completed-event store: {exc}") from exc

    def contains(self, identity: str, event_id: int) -> bool:
        return self.key(identity, event_id) in self._items

    def record(self, identity: str, event_id: int) -> None:
        key = self.key(identity, event_id)
        self._items[key] = None
        self._items.move_to_end(key)
        while len(self._items) > self.limit:
            self._items.popitem(last=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(
            self.path,
            {"completed": list(self._items)},
            indent=None,
            mode=0o600,
        )


class HcomBridge:
    def __init__(self, runner: Any, config: HcomConfig):
        self.runner = runner
        self.config = config
        self.executable = config.executable
        self.child_env = os.environ.copy()
        for key in ("HCOM_PROCESS_ID", "HCOM_INSTANCE_NAME", "HCOM_TOOL"):
            self.child_env.pop(key, None)
        self.child_env.update(config.env)
        self._registered_identities: set[str] = set()
        self._binding_tasks: set[asyncio.Task[Any]] = set()
        self.completed = CompletedEventStore(
            Path(get_hermes_home()) / "hcom_completed_events.json"
        )

    @staticmethod
    def _resolve_executable(value: str) -> str:
        candidate = str(value or "hcom").strip()
        if os.path.isabs(candidate):
            if not os.access(candidate, os.X_OK):
                raise RuntimeError(f"gateway.hcom executable is not executable: {candidate}")
            return candidate
        resolved = shutil.which(candidate)
        if not resolved:
            raise RuntimeError(f"gateway.hcom executable was not found: {candidate}")
        return resolved

    async def _run(self, *args: str) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            self._resolve_executable(self.executable),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.child_env,
        )
        try:
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            raise
        return (
            int(process.returncode or 0),
            stdout.decode("utf-8", errors="replace").strip(),
            stderr.decode("utf-8", errors="replace").strip(),
        )

    async def _register(self, identity: str) -> None:
        code, _, stderr = await self._run("start", "--as", identity)
        if code:
            raise RuntimeError(f"hcom registration failed for {identity}: {stderr}")
        self._registered_identities.add(identity)

    async def _unregister(self, identity: str) -> None:
        code, _, stderr = await self._run("stop", identity)
        if code:
            raise RuntimeError(f"hcom unregister failed for {identity}: {stderr}")
        self._registered_identities.discard(identity)

    def track_binding_task(self, task: asyncio.Task[Any]) -> None:
        self._binding_tasks.add(task)
        task.add_done_callback(self._binding_tasks.discard)

    async def shutdown(self) -> None:
        """Stop listeners, then disconnect every identity registered by the bridge."""
        tasks = tuple(self._binding_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # run_binding() normally unregisters in its finally block. This fallback
        # covers a failed/cancelled unregister without leaving shutdown brittle.
        for identity in tuple(self._registered_identities):
            try:
                await asyncio.wait_for(self._unregister(identity), timeout=5)
            except Exception as exc:
                logger.warning("hcom bridge failed to unregister %s: %s", identity, exc)

    async def _receive(self, identity: str) -> Optional[HcomEnvelope]:
        code, stdout, stderr = await self._run(
            "listen",
            "--name",
            identity,
            "--json",
            "--manual-ack",
            "--timeout",
            "86400",
        )
        if code:
            raise RuntimeError(f"hcom receive failed for {identity}: {stderr}")
        if not stdout:
            return None
        return HcomEnvelope.from_json(stdout.splitlines()[-1])

    async def _ack(self, identity: str, event_id: int) -> None:
        code, _, stderr = await self._run(
            "ack", "--name", identity, str(event_id)
        )
        if code:
            raise RuntimeError(f"hcom ack failed for {identity}/{event_id}: {stderr}")

    async def _dispatch(
        self, binding: HcomBridgeConfig, envelope: HcomEnvelope
    ) -> ProcessingOutcome:
        source = SessionSource.from_dict(binding.origin)
        adapter = self.runner._adapter_for_source(source)
        if adapter is None:
            raise RuntimeError(
                f"no live {source.platform.value} adapter for hcom identity {binding.identity}"
            )
        future: "asyncio.Future[ProcessingOutcome]" = (
            asyncio.get_running_loop().create_future()
        )
        event = MessageEvent(
            text=envelope.event_text(binding.identity),
            source=source,
            user_id=envelope.sender,
            user_name=envelope.sender,
            internal=True,
            message_id=str(envelope.event_id),
            metadata={
                "hcom": {
                    "identity": binding.identity,
                    "event_id": envelope.event_id,
                    "from": envelope.sender,
                    "intent": envelope.intent,
                    "thread": envelope.thread,
                    "reply_to": envelope.reply_to,
                    "timestamp": envelope.timestamp,
                    "to": envelope.recipients,
                    "bundle_id": envelope.bundle_id,
                }
            },
        )
        event._processing_completion_futures.append(future)
        event._hcom_subprocess_env = dict(self.config.env)
        with scoped_subprocess_env(self.config.env):
            await adapter.handle_message(event)
        return await future

    async def _process_envelope(
        self, binding: HcomBridgeConfig, envelope: HcomEnvelope
    ) -> None:
        if self.completed.contains(binding.identity, envelope.event_id):
            await self._ack(binding.identity, envelope.event_id)
            return
        outcome = await self._dispatch(binding, envelope)
        if outcome != ProcessingOutcome.SUCCESS:
            raise RuntimeError(
                f"hcom event {binding.identity}/{envelope.event_id} completed as {outcome.value}"
            )
        self.completed.record(binding.identity, envelope.event_id)
        await self._ack(binding.identity, envelope.event_id)

    async def run_binding(self, binding: HcomBridgeConfig) -> None:
        backoff = 1
        registered = False
        try:
            while self.runner._running:
                try:
                    if not registered:
                        await self._register(binding.identity)
                        registered = True
                    envelope = await self._receive(binding.identity)
                    if envelope is None:
                        backoff = 1
                        continue
                    await self._process_envelope(binding, envelope)
                    backoff = 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "hcom bridge %s retrying in %ds: %s",
                        binding.identity,
                        backoff,
                        exc,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(60, backoff * 2)
        finally:
            if registered and binding.identity in self._registered_identities:
                try:
                    await asyncio.wait_for(self._unregister(binding.identity), timeout=5)
                except Exception as exc:
                    logger.warning(
                        "hcom bridge failed to unregister %s: %s",
                        binding.identity,
                        exc,
                    )
