"""Generic plugin-owned gateway runtime services and trusted ingress."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import logging
from typing import Any, Dict, Optional

from gateway.platforms.event import MessageEvent, ProcessingOutcome
from gateway.session import SessionSource

logger = logging.getLogger("gateway.run")


class GatewayPluginRuntimeMixin:
    """Host lifecycle and exact-route dispatch for external gateway plugins."""

    async def dispatch_internal_message(
        self,
        *,
        source: Optional[SessionSource] = None,
        session_key: str = "",
        text: str,
        user_id: str,
        user_name: str = "",
        message_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        subprocess_env: Optional[Dict[str, str]] = None,
        visible_inbound_text: Optional[str] = None,
    ) -> ProcessingOutcome:
        """Dispatch trusted plugin ingress through an existing platform session."""
        pinned_session_id = ""
        if session_key:
            entry = await self.async_session_store.lookup_by_session_key(session_key)
            if entry is None or entry.origin is None:
                raise RuntimeError(f"internal message session is unavailable: {session_key}")
            source = dataclasses.replace(entry.origin)
            pinned_session_id = entry.session_id
        elif source is None:
            raise ValueError("dispatch_internal_message requires source or session_key")

        adapter = self._adapter_for_source(source)
        if adapter is None:
            raise RuntimeError(f"no live {source.platform.value} adapter for internal message")

        platform_message_id = ""
        if visible_inbound_text:
            result = await adapter.send(
                source.chat_id,
                visible_inbound_text,
                metadata=self._thread_metadata_for_source(source),
            )
            if not getattr(result, "success", False):
                raise RuntimeError(f"failed to display internal message on {source.platform.value}")
            platform_message_id = str(getattr(result, "message_id", "") or "")

        event_metadata = dict(metadata or {})
        if session_key:
            event_metadata.update(
                {
                    "gateway_session_key": session_key,
                    "gateway_session_id": pinned_session_id,
                    "gateway_session_strict": True,
                }
            )
        future: asyncio.Future[ProcessingOutcome] = asyncio.get_running_loop().create_future()
        event = MessageEvent(
            text=text,
            source=source,
            user_id=user_id,
            user_name=user_name or user_id,
            internal=True,
            # External event ids belong in metadata, never in a platform reply anchor.
            message_id=platform_message_id or message_id,
            metadata=event_metadata,
        )
        event._processing_completion_futures.append(future)
        event._plugin_subprocess_env = dict(subprocess_env or {})
        await adapter.handle_message(event)
        return await future

    async def _start_plugin_gateway_services(self) -> None:
        from hermes_cli.plugins import get_gateway_service_factories

        for name, factory in get_gateway_service_factories().items():
            try:
                service = factory(self)
                if inspect.isawaitable(service):
                    service = await service
                start = getattr(service, "start", None)
                shutdown = getattr(service, "shutdown", None)
                if not callable(start) or not callable(shutdown):
                    raise TypeError("gateway service must provide async start() and shutdown()")
                result = start()
                if inspect.isawaitable(result):
                    await result
                self._plugin_gateway_services[name] = service
            except Exception as exc:
                logger.warning("Plugin gateway service %s failed to start: %s", name, exc)

    async def _shutdown_plugin_gateway_services(self) -> None:
        registry = getattr(self, "_plugin_gateway_services", None)
        if not isinstance(registry, dict):
            return
        services = list(registry.items())
        registry.clear()
        for name, service in reversed(services):
            try:
                result = service.shutdown()
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, timeout=10)
            except Exception as exc:
                logger.warning("Plugin gateway service %s failed to stop: %s", name, exc)
