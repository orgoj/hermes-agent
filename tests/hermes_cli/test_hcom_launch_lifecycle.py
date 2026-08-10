from types import SimpleNamespace

import cli


def test_hcom_lifecycle_is_inert_without_process_binding(monkeypatch):
    calls = []
    monkeypatch.delenv("HCOM_PROCESS_ID", raising=False)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: calls.append((a, k)))
    cli._notify_hcom_lifecycle("hermes-start")
    assert calls == []


def test_hcom_lifecycle_uses_exact_callback_argv_and_is_best_effort(monkeypatch):
    calls = []
    monkeypatch.setenv("HCOM_PROCESS_ID", "process-1")

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=1, stderr="expected test failure")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    cli._notify_hcom_lifecycle("hermes-status", "active")
    assert calls[0][0] == ["hcom", "hermes-status", "active"]
    assert calls[0][1]["check"] is False
