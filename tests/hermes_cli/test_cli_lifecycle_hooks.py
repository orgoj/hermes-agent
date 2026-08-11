import cli


def test_cli_lifecycle_notifies_plugins_and_is_best_effort(monkeypatch):
    calls = []

    def invoke_hook(name, **kwargs):
        calls.append((name, kwargs))
        raise RuntimeError("expected test failure")

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", invoke_hook)

    cli._notify_cli_lifecycle("on_cli_ready")

    assert calls == [("on_cli_ready", {"cli_surface": "interactive"})]
