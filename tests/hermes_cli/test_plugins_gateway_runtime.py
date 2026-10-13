"""Tests for the gateway-runtime plugin registrations (hermes_cli.plugins_gateway_runtime)."""

import pytest

from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


def _ctx(manager: PluginManager) -> PluginContext:
    return PluginContext(PluginManifest(name="runtime-plugin", source="user"), manager)


def test_register_gateway_service():
    manager = PluginManager()
    factory = lambda runner: runner

    _ctx(manager).register_gateway_service("external-ingress", factory)

    assert manager.get_gateway_service_factories() == {"external-ingress": factory}


def test_register_gateway_service_rejects_duplicates():
    ctx = _ctx(PluginManager())
    ctx.register_gateway_service("external-ingress", lambda runner: runner)

    with pytest.raises(ValueError, match="already registered"):
        ctx.register_gateway_service("external-ingress", lambda runner: runner)


def test_register_gateway_prompt_provider():
    manager = PluginManager()
    provider = lambda **kwargs: kwargs["session_key"]

    _ctx(manager).register_gateway_prompt_provider("runtime-context", provider)

    assert manager.get_gateway_prompt_providers() == {"runtime-context": provider}


def test_register_gateway_prompt_provider_rejects_duplicates():
    ctx = _ctx(PluginManager())
    ctx.register_gateway_prompt_provider("runtime-context", lambda **_: "")

    with pytest.raises(ValueError, match="already registered"):
        ctx.register_gateway_prompt_provider("runtime-context", lambda **_: "")
