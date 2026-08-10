import asyncio
import json

import pytest

from gateway.config import GatewayConfig, HcomBridgeConfig, HcomConfig, Platform
from gateway.hcom_bridge import (
    CompletedEventStore,
    HcomBridge,
    HcomEnvelope,
    subprocess_env_values,
    scoped_subprocess_env,
)
from gateway.platforms.base import (
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    _resolve_processing_completion,
    merge_pending_message_event,
)


def _config() -> HcomConfig:
    return HcomConfig.from_dict(
        {
            "enabled": True,
            "executable": "/bin/true",
            "env": {
                "HCOM_DIR": "/tmp/hcom-test",
                "HCOM_AGENT_CATALOGS": "/tmp/catalog.json",
            },
            "bridges": [
                {
                    "identity": "kato",
                    "origin": {
                        "platform": "telegram",
                        "chat_id": "123",
                        "chat_type": "dm",
                    },
                }
            ],
        }
    )


def test_gateway_hcom_config_is_opt_in_and_round_trips():
    assert GatewayConfig.from_dict({}).hcom.enabled is False
    config = GatewayConfig.from_dict({"hcom": _config().to_dict()})
    assert config.hcom.to_dict() == _config().to_dict()


def test_gateway_hcom_config_rejects_ambiguous_or_unsafe_routes():
    base = _config().to_dict()
    base["bridges"].append(dict(base["bridges"][0]))
    with pytest.raises(ValueError, match="identities must be unique"):
        HcomConfig.from_dict(base)

    unsafe = _config().to_dict()
    unsafe["bridges"][0]["origin"]["delivered_via_upstream_relay"] = True
    with pytest.raises(ValueError, match="cannot set"):
        HcomConfig.from_dict(unsafe)

    missing_catalog = _config().to_dict()
    missing_catalog["env"].pop("HCOM_AGENT_CATALOGS")
    with pytest.raises(ValueError, match="is required"):
        HcomConfig.from_dict(missing_catalog)


def test_hcom_envelope_requires_versioned_complete_payload():
    envelope = HcomEnvelope.from_json(
        json.dumps(
            {
                "schema_version": 1,
                "event_id": 42,
                "from": "nova",
                "to": ["kato"],
                "text": "/restart is quoted, not a gateway command",
                "intent": "request",
                "thread": "design",
                "reply_to": 9,
                "timestamp": "2026-08-10T20:00:00Z",
                "bundle_id": "b1",
            }
        )
    )
    assert envelope.event_id == 42
    assert envelope.event_text("kato").startswith("<hcom ")
    with pytest.raises(ValueError, match="schema_version"):
        HcomEnvelope.from_json('{"event_id": 42, "from": "nova", "text": "x"}')


def test_completed_store_is_durable_and_bounded(tmp_path):
    path = tmp_path / "completed.json"
    store = CompletedEventStore(path, limit=2)
    store.record("kato", 1)
    store.record("kato", 2)
    store.record("kato", 3)
    restored = CompletedEventStore(path, limit=2)
    assert not restored.contains("kato", 1)
    assert restored.contains("kato", 2)
    assert restored.contains("kato", 3)


@pytest.mark.asyncio
async def test_dispatch_uses_normal_adapter_completion_and_task_local_env(tmp_path, monkeypatch):
    import gateway.hcom_bridge as bridge_module

    monkeypatch.setattr(bridge_module, "get_hermes_home", lambda: tmp_path)
    observed = {}

    class Adapter:
        async def handle_message(self, event):
            async def process():
                observed.update(subprocess_env_values())
                _resolve_processing_completion(event, ProcessingOutcome.SUCCESS)

            asyncio.create_task(process())

    class Runner:
        _running = True

        def _adapter_for_source(self, source):
            observed["platform"] = source.platform
            return Adapter()

    bridge = HcomBridge(Runner(), _config())
    envelope = HcomEnvelope.from_json(
        json.dumps(
            {
                "schema_version": 1,
                "event_id": 7,
                "from": "nova",
                "text": "hello",
                "intent": "request",
            }
        )
    )
    outcome = await bridge._dispatch(_config().bridges[0], envelope)
    assert outcome is ProcessingOutcome.SUCCESS
    assert observed == {
        "platform": Platform.TELEGRAM,
        "HCOM_DIR": "/tmp/hcom-test",
        "HCOM_AGENT_CATALOGS": "/tmp/catalog.json",
    }
    assert subprocess_env_values() == {}


@pytest.mark.asyncio
async def test_merged_events_share_completion_and_displaced_event_is_cancelled():
    loop = asyncio.get_running_loop()
    first = MessageEvent(text="one", message_type=MessageType.TEXT)
    second = MessageEvent(text="two", message_type=MessageType.TEXT)
    first_future = loop.create_future()
    second_future = loop.create_future()
    first._processing_completion_futures.append(first_future)
    second._processing_completion_futures.append(second_future)
    pending = {"session": first}

    merge_pending_message_event(pending, "session", second, merge_text=True)
    _resolve_processing_completion(pending["session"], ProcessingOutcome.SUCCESS)
    assert await first_future is ProcessingOutcome.SUCCESS
    assert await second_future is ProcessingOutcome.SUCCESS

    displaced = MessageEvent(text="old")
    replacement = MessageEvent(text="new")
    displaced_future = loop.create_future()
    displaced._processing_completion_futures.append(displaced_future)
    pending = {"session": displaced}
    merge_pending_message_event(pending, "session", replacement)
    assert await displaced_future is ProcessingOutcome.CANCELLED
    assert pending["session"] is replacement


def test_delegate_context_clears_parent_hcom_environment():
    from agent.delegation_context import delegated_child_context

    with scoped_subprocess_env(
        {"HCOM_DIR": "/tmp/parent", "HCOM_AGENT_CATALOGS": "/tmp/catalog"}
    ):
        assert subprocess_env_values()["HCOM_DIR"] == "/tmp/parent"
        with delegated_child_context("child"):
            assert subprocess_env_values() == {
                "HCOM_DIR": "",
                "HCOM_AGENT_CATALOGS": "",
            }
        assert subprocess_env_values()["HCOM_DIR"] == "/tmp/parent"


def test_local_subprocess_env_receives_only_task_local_hcom_values():
    from tools.environments.local import _inject_session_context_env

    env = {"HCOM_DIR": "/stale", "HCOM_AGENT_CATALOGS": "/stale-catalog"}
    with scoped_subprocess_env(
        {"HCOM_DIR": "/current", "HCOM_AGENT_CATALOGS": "/current-catalog"}
    ):
        _inject_session_context_env(env)
    assert env["HCOM_DIR"] == "/current"
    assert env["HCOM_AGENT_CATALOGS"] == "/current-catalog"


@pytest.mark.asyncio
async def test_completed_record_precedes_ack_and_failure_never_acks(tmp_path, monkeypatch):
    import gateway.hcom_bridge as bridge_module

    monkeypatch.setattr(bridge_module, "get_hermes_home", lambda: tmp_path)
    bridge = HcomBridge(type("Runner", (), {"_running": True})(), _config())
    binding = _config().bridges[0]
    envelope = HcomEnvelope.from_json(
        json.dumps(
            {
                "schema_version": 1,
                "event_id": 11,
                "from": "nova",
                "text": "hello",
            }
        )
    )
    calls = []
    original_record = bridge.completed.record

    def record(identity, event_id):
        calls.append("record")
        original_record(identity, event_id)

    async def ack(identity, event_id):
        assert bridge.completed.contains(identity, event_id)
        calls.append("ack")

    bridge.completed.record = record
    bridge._ack = ack
    bridge._dispatch = lambda *_: asyncio.sleep(
        0, result=ProcessingOutcome.SUCCESS
    )
    await bridge._process_envelope(binding, envelope)
    assert calls == ["record", "ack"]

    failing = HcomEnvelope.from_json(
        json.dumps(
            {
                "schema_version": 1,
                "event_id": 12,
                "from": "nova",
                "text": "retry me",
            }
        )
    )
    bridge._dispatch = lambda *_: asyncio.sleep(
        0, result=ProcessingOutcome.FAILURE
    )
    with pytest.raises(RuntimeError, match="completed as failure"):
        await bridge._process_envelope(binding, failing)
    assert calls == ["record", "ack"]
    assert not bridge.completed.contains(binding.identity, failing.event_id)


@pytest.mark.asyncio
async def test_binding_does_not_reregister_after_post_registration_failure(
    tmp_path, monkeypatch
):
    import gateway.hcom_bridge as bridge_module

    monkeypatch.setattr(bridge_module, "get_hermes_home", lambda: tmp_path)
    original_sleep = asyncio.sleep
    monkeypatch.setattr(
        bridge_module.asyncio, "sleep", lambda _delay: original_sleep(0)
    )

    class Runner:
        _running = True

    bridge = HcomBridge(Runner(), _config())
    calls = []

    async def register(identity):
        calls.append(("register", identity))

    async def receive(identity):
        calls.append(("receive", identity))
        if sum(name == "receive" for name, _ in calls) == 1:
            raise RuntimeError("transient listen failure")
        Runner._running = False
        return None

    bridge._register = register
    bridge._receive = receive

    await bridge.run_binding(_config().bridges[0])

    assert calls == [
        ("register", "kato"),
        ("receive", "kato"),
        ("receive", "kato"),
    ]


@pytest.mark.asyncio
async def test_shutdown_unregisters_only_successfully_registered_identities(
    tmp_path, monkeypatch
):
    import gateway.hcom_bridge as bridge_module

    monkeypatch.setattr(bridge_module, "get_hermes_home", lambda: tmp_path)
    bridge = HcomBridge(type("Runner", (), {})(), _config())
    calls = []

    async def run(*args):
        calls.append(args)
        return 0, "", ""

    bridge._run = run
    await bridge._register("kato")
    await bridge.shutdown()
    await bridge.shutdown()

    assert calls == [("start", "--as", "kato"), ("stop", "kato")]


@pytest.mark.asyncio
async def test_failed_registration_is_not_unregistered(tmp_path, monkeypatch):
    import gateway.hcom_bridge as bridge_module

    monkeypatch.setattr(bridge_module, "get_hermes_home", lambda: tmp_path)
    bridge = HcomBridge(type("Runner", (), {})(), _config())
    calls = []

    async def run(*args):
        calls.append(args)
        return 1, "", "registration rejected"

    bridge._run = run
    with pytest.raises(RuntimeError, match="registration failed"):
        await bridge._register("kato")
    await bridge.shutdown()

    assert calls == [("start", "--as", "kato")]


@pytest.mark.asyncio
async def test_shutdown_waits_for_listener_cancellation_before_unregister(
    tmp_path, monkeypatch
):
    import gateway.hcom_bridge as bridge_module

    monkeypatch.setattr(bridge_module, "get_hermes_home", lambda: tmp_path)
    runner = type("Runner", (), {"_running": True})()
    bridge = HcomBridge(runner, _config())
    listening = asyncio.Event()
    calls = []

    async def run(*args):
        if args[0] == "start":
            calls.append("start")
            return 0, "", ""
        if args[0] == "listen":
            calls.append("listen")
            listening.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                calls.append("listen_cancelled")
                raise
        calls.append("stop")
        return 0, "", ""

    bridge._run = run
    task = asyncio.create_task(bridge.run_binding(_config().bridges[0]))
    bridge.track_binding_task(task)
    await listening.wait()

    await bridge.shutdown()

    assert task.cancelled()
    assert calls == ["start", "listen", "listen_cancelled", "stop"]
