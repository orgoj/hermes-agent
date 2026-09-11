from types import SimpleNamespace

import pytest

from hermes_cli import main as cli_main
from hermes_cli import update_cmd


def test_downstream_marker_blocks_update_before_backup(tmp_path, monkeypatch, capsys):
    (tmp_path / ".hermes-update-blocked").write_text("blocked", encoding="utf-8")
    monkeypatch.setattr(cli_main, "PROJECT_ROOT", tmp_path)
    backup_called = False

    def backup(_args):
        nonlocal backup_called
        backup_called = True

    monkeypatch.setattr(cli_main, "_run_pre_update_backup", backup)

    with pytest.raises(SystemExit) as exc:
        update_cmd._cmd_update_impl(SimpleNamespace(), gateway_mode=False)

    assert exc.value.code == 2
    assert backup_called is False
    output = capsys.readouterr().out
    assert "Automatic Hermes update is disabled" in output
    assert "git merge --no-edit upstream/main" in output
    assert "DOWNSTREAM-MAINTENANCE.md" in output


def test_checkout_without_marker_keeps_normal_update_path(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "PROJECT_ROOT", tmp_path)

    update_cmd._refuse_blocked_checkout_update()
