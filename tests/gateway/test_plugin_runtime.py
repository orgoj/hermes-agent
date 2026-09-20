import asyncio
from types import SimpleNamespace

import pytest

from gateway.platforms.base import (
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    _resolve_processing_completion,
    merge_pending_message_event,
)
from gateway.plugin_context import scoped_subprocess_env, subprocess_env_values


@pytest.mark.asyncio
async def test_internal_dispatch_uses_platform_completion_and_task_local_env():
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    observed = {}

    class Adapter:
        async def handle_message(self, event):
            observed["event_env"] = event._plugin_subprocess_env
            _resolve_processing_completion(event, ProcessingOutcome.SUCCESS)

    runner = object.__new__(GatewayRunner)
    runner._delivery_adapter_for = lambda _source: Adapter()
    source = SessionSource.from_dict(
        {"platform": "telegram", "chat_id": "123", "chat_type": "dm"}
    )

    outcome = await runner.dispatch_internal_message(
        source=source,
        text="hello",
        user_id="external",
        subprocess_env={"EXTERNAL_ROUTE": "route-1"},
    )

    assert outcome is ProcessingOutcome.SUCCESS
    assert observed == {"event_env": {"EXTERNAL_ROUTE": "route-1"}}
    assert subprocess_env_values() == {}


@pytest.mark.asyncio
async def test_internal_dispatch_displays_external_input_before_processing():
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    calls = []

    class Adapter:
        async def send(self, chat_id, text, metadata=None):
            calls.append(("send", chat_id, text, metadata))
            return type("Result", (), {"success": True, "message_id": "901"})()

        async def handle_message(self, event):
            calls.append(("handle", event.text, event.message_id))
            _resolve_processing_completion(event, ProcessingOutcome.SUCCESS)

    runner = object.__new__(GatewayRunner)
    runner._delivery_adapter_for = lambda _source: Adapter()
    runner._thread_metadata_for_source = lambda _source: {"thread_id": "topic-1"}
    source = SessionSource.from_dict(
        {"platform": "telegram", "chat_id": "123", "chat_type": "dm"}
    )

    outcome = await runner.dispatch_internal_message(
        source=source,
        text="internal envelope",
        user_id="external",
        visible_inbound_text="External message from @nova: hello",
    )

    assert outcome is ProcessingOutcome.SUCCESS
    assert calls == [
        (
            "send",
            "123",
            "External message from @nova: hello",
            {"thread_id": "topic-1"},
        ),
        ("handle", "internal envelope", "901"),
    ]


@pytest.mark.asyncio
async def test_internal_dispatch_resolves_exact_session_route_and_pins_it():
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    observed = {}
    source = SessionSource.from_dict(
        {
            "platform": "telegram",
            "chat_id": "123",
            "chat_type": "dm",
            "thread_id": "topic-2",
        }
    )

    class Store:
        def lookup_by_session_key(self, session_key):
            assert session_key == "agent:main:telegram:dm:123:topic-2"
            return SimpleNamespace(session_id="session-2", origin=source)

    class Adapter:
        async def send(self, chat_id, text, metadata=None):
            observed["send"] = (chat_id, text, metadata)
            return SimpleNamespace(success=True, message_id="telegram-902")

        async def handle_message(self, event):
            observed["event"] = event
            _resolve_processing_completion(event, ProcessingOutcome.SUCCESS)

    runner = object.__new__(GatewayRunner)
    runner.session_store = Store()
    runner._delivery_adapter_for = lambda _source: Adapter()
    runner._thread_metadata_for_source = lambda resolved: {
        "thread_id": resolved.thread_id
    }

    outcome = await runner.dispatch_internal_message(
        session_key="agent:main:telegram:dm:123:topic-2",
        text="correlated reply",
        user_id="nova",
        metadata={"hcom": {"event_id": 42}},
        visible_inbound_text="visible reply",
    )

    assert outcome is ProcessingOutcome.SUCCESS
    assert observed["send"] == (
        "123",
        "visible reply",
        {"thread_id": "topic-2"},
    )
    event = observed["event"]
    assert event.source.thread_id == "topic-2"
    assert event.message_id == "telegram-902"
    assert event.metadata == {
        "hcom": {"event_id": 42},
        "gateway_session_key": "agent:main:telegram:dm:123:topic-2",
        "gateway_session_id": "session-2",
        "gateway_session_strict": True,
    }


@pytest.mark.asyncio
async def test_platform_processing_binds_plugin_subprocess_environment():
    from gateway.platforms.base import BasePlatformAdapter

    observed = {}

    class Adapter:
        async def _process_message_background(self, _event, _session_key):
            observed.update(subprocess_env_values())

    event = MessageEvent(text="hello")
    event._plugin_subprocess_env = {"EXTERNAL_ROUTE": "route-1"}

    await BasePlatformAdapter._process_message_with_event_context(
        Adapter(), event, "session"
    )

    assert observed == {"EXTERNAL_ROUTE": "route-1"}
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


def test_delegate_context_clears_parent_plugin_environment():
    from agent.delegation_context import delegated_child_context

    with scoped_subprocess_env({"EXTERNAL_ROUTE": "parent"}):
        assert subprocess_env_values() == {"EXTERNAL_ROUTE": "parent"}
        with delegated_child_context("child"):
            assert subprocess_env_values() == {}
        assert subprocess_env_values() == {"EXTERNAL_ROUTE": "parent"}


def test_local_subprocess_receives_only_task_local_plugin_values():
    from tools.environments.local import _inject_session_context_env

    env = {"EXTERNAL_ROUTE": "stale"}
    with scoped_subprocess_env({"EXTERNAL_ROUTE": "current"}):
        _inject_session_context_env(env)
    assert env["EXTERNAL_ROUTE"] == "current"


@pytest.mark.asyncio
async def test_gateway_service_lifecycle_is_owned_and_bounded():
    from gateway.run import GatewayRunner

    calls = []

    class Service:
        async def start(self):
            calls.append("start")

        async def shutdown(self):
            calls.append("shutdown")

    runner = object.__new__(GatewayRunner)
    runner._plugin_gateway_services = {}

    import hermes_cli.plugins as plugins

    original = plugins.get_gateway_service_factories
    plugins.get_gateway_service_factories = lambda: {"test": lambda _runner: Service()}
    try:
        await runner._start_plugin_gateway_services()
        await runner._shutdown_plugin_gateway_services()
    finally:
        plugins.get_gateway_service_factories = original

    assert calls == ["start", "shutdown"]
    assert runner._plugin_gateway_services == {}
