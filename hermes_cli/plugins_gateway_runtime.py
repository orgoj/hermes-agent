"""Downstream gateway-runtime plugin registrations: background services and prompt providers.

Mixed into ``PluginContext`` / ``PluginManager``; the registries themselves live on the
manager (``_gateway_service_factories``, ``_gateway_prompt_providers``) so the ledger can
roll them back with every other registration.
"""

from __future__ import annotations

from typing import Callable

from hermes_cli.plugins_ledger import PluginRegistration


class PluginContextGatewayRuntimeMixin:
    """``ctx.register_gateway_service`` / ``ctx.register_gateway_prompt_provider``."""

    def register_gateway_service(self, name: str, factory: Callable) -> PluginRegistration:
        """Register a gateway-owned service factory with async start/shutdown lifecycle."""
        clean = str(name or "").strip()
        if not clean:
            raise ValueError("gateway service name is required")
        if not callable(factory):
            raise ValueError(f"gateway service {clean!r} factory must be callable")
        if clean in self._manager._gateway_service_factories:
            raise ValueError(f"gateway service {clean!r} is already registered")
        return self._register_entry(
            "gateway_service",
            clean,
            self._manager._gateway_service_factories,
            factory,
            "Plugin %s registered gateway service: %s",
            clean,
        )

    def register_gateway_prompt_provider(
        self, name: str, callback: Callable
    ) -> PluginRegistration:
        """Register a session-stable gateway prompt contributor."""
        clean = str(name or "").strip()
        if not clean or not callable(callback):
            raise ValueError("gateway prompt provider requires a name and callback")
        if clean in self._manager._gateway_prompt_providers:
            raise ValueError(f"gateway prompt provider {clean!r} is already registered")
        return self._register_entry(
            "gateway_prompt_provider",
            clean,
            self._manager._gateway_prompt_providers,
            callback,
            "Plugin %s registered gateway prompt provider: %s",
            clean,
        )


class PluginManagerGatewayRuntimeMixin:
    """Snapshots of the gateway-runtime registries."""

    def get_gateway_service_factories(self) -> dict[str, Callable]:
        """Return a snapshot of plugin gateway service factories."""
        return dict(self._gateway_service_factories)

    def get_gateway_prompt_providers(self) -> dict[str, Callable]:
        """Return a snapshot of session-stable gateway prompt contributors."""
        return dict(self._gateway_prompt_providers)


def get_gateway_service_factories() -> dict[str, Callable]:
    """Return registered gateway background-service factories."""
    from hermes_cli.plugins import get_plugin_manager

    return get_plugin_manager().get_gateway_service_factories()


def get_gateway_prompt_providers() -> dict[str, Callable]:
    """Return registered gateway system-prompt contributors."""
    from hermes_cli.plugins import get_plugin_manager

    return get_plugin_manager().get_gateway_prompt_providers()
