"""Generic plugin-owned gateway runtime services and trusted ingress."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import logging
from typing import Any, Optional

from gateway.platforms.event import MessageEvent, ProcessingOutcome
from gateway.session import SessionSource

logger = logging.getLogger("gateway.run")


def append_gateway_plugin_prompts(
    combined: str, source: SessionSource, session_key: str, gateway: Any
) -> str:
    """Append session-stable plugin prompt contributions to the turn's system context."""
    from hermes_cli.plugins_gateway_runtime import get_gateway_prompt_providers

    for provider_name, provider in get_gateway_prompt_providers().items():
        try:
            contribution = provider(source=source, session_key=session_key, gateway=gateway)
        except Exception:  # plugin code: one failing provider must not break the turn
            logger.warning("gateway prompt provider %s failed", provider_name, exc_info=True)
            continue
        if contribution:
            combined = (combined + "\n\n" + str(contribution).strip()).strip()
    return combined


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
        metadata: Optional[dict[str, Any]] = None,
        subprocess_env: Optional[dict[str, str]] = None,
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

        adapter = self._delivery_adapter_for(source)
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


# Service lifecycle takes the runner as an argument (not a mixin method) because upstream tests
# drive GatewayRunner.start/stop on duck-typed fakes that know nothing of this runtime.
async def start_plugin_gateway_services(runner: Any) -> None:
    from hermes_cli.plugins_gateway_runtime import get_gateway_service_factories

    services = vars(runner).setdefault("_plugin_gateway_services", {})
    for name, factory in get_gateway_service_factories().items():
        try:
            service = factory(runner)
            if inspect.isawaitable(service):
                service = await service
            start = getattr(service, "start", None)
            shutdown = getattr(service, "shutdown", None)
            if not callable(start) or not callable(shutdown):
                raise TypeError("gateway service must provide async start() and shutdown()")
            result = start()
            if inspect.isawaitable(result):
                await result
            services[name] = service
        except Exception:  # plugin code: one failing service must not block gateway startup
            logger.warning("Plugin gateway service %s failed to start", name, exc_info=True)


async def shutdown_plugin_gateway_services(runner: Any) -> None:
    registry = vars(runner).get("_plugin_gateway_services")
    if not isinstance(registry, dict):
        return
    services = list(registry.items())
    registry.clear()
    for name, service in reversed(services):
        try:
            result = service.shutdown()
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=10)
        except Exception:  # plugin code: keep stopping the remaining services
            logger.warning("Plugin gateway service %s failed to stop", name, exc_info=True)

