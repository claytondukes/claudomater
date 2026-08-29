"""omater CLI: end-to-end wiring of the Phase 0 pieces."""

from __future__ import annotations

import json
import os
import time

import pytest

from claudomater.cli import EXIT_DEGRADE, EXIT_ERROR, EXIT_OK, EXIT_PAUSE, main
from claudomater.runlog import RunLog
from claudomater.usage import FAKE_USAGE_ENV


def write_fake_usage(tmp_path, monkeypatch, data, age_s=0):
    path = tmp_path / "fake-usage.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    if age_s:
        old = time.time() - age_s
        os.utime(path, (old, old))
    monkeypatch.setenv(FAKE_USAGE_ENV, str(path))


@pytest.fixture
def no_user_config(tmp_path):
    """Point --user-config at a missing file so the operator's real
    ~/.omater/config.yaml never leaks into tests."""
    return str(tmp_path / "no-user-config.yaml")


class TestUsageCommand:
    def test_ok_exit_code(self, tmp_path, monkeypatch, capsys, no_user_config):
        write_fake_usage(tmp_path, monkeypatch, {"five_hour": 10, "seven_day": 10, "scoped": 10})
        rc = main(["usage", "--json", "--user-config", no_user_config])
        assert rc == EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["action"] == "ok"
        assert out["usage"]["source"] == "fake"

    def test_pause_exit_code(self, tmp_path, monkeypatch, no_user_config):
        write_fake_usage(tmp_path, monkeypatch, {"five_hour": 96, "seven_day": 10, "scoped": 10})
        assert main(["usage", "--user-config", no_user_config]) == EXIT_PAUSE

    def test_degrade_exit_code(self, tmp_path, monkeypatch, no_user_config):
        write_fake_usage(tmp_path, monkeypatch, {"five_hour": 10, "seven_day": 10, "scoped": 85})
        assert main(["usage", "--user-config", no_user_config]) == EXIT_DEGRADE

    def test_stale_fake_pauses_fail_closed(self, tmp_path, monkeypatch, no_user_config):
        write_fake_usage(
            tmp_path, monkeypatch, {"five_hour": 1, "seven_day": 1, "scoped": 1}, age_s=900
        )
        assert main(["usage", "--user-config", no_user_config]) == EXIT_PAUSE

    def test_missing_window_prints_and_pauses_without_crashing(
        self, tmp_path, monkeypatch, capsys, no_user_config
    ):
        """The fail-closed path (a window missing from the snapshot) must
        yield exit 3 with readable output — not a TypeError traceback."""
        write_fake_usage(tmp_path, monkeypatch, {"seven_day": 10, "scoped": 10})
        rc = main(["usage", "--user-config", no_user_config])
        assert rc == EXIT_PAUSE
        out = capsys.readouterr().out
        assert "failing closed" in out


class TestPolicyCommand:
    def test_policy_json_reflects_deployment_type(self, tmp_path, capsys):
        (tmp_path / ".omater.yaml").write_text(
            "project: demo\ndeployment_type: production\n", encoding="utf-8"
        )
        assert main(["policy", str(tmp_path), "--json"]) == EXIT_OK
        policy = json.loads(capsys.readouterr().out)
        assert policy["deployment_type"] == "production"
        assert policy["models"]["sr_review"] == "claude-fable-5"
        assert policy["review_floor"] == "SHOULD-FIX"

    def test_policy_without_config_errors(self, tmp_path, capsys):
        assert main(["policy", str(tmp_path)]) == EXIT_ERROR
        assert "not found" in capsys.readouterr().err

    def test_invalid_config_errors(self, tmp_path, capsys):
        (tmp_path / ".omater.yaml").write_text(
            "project: demo\nforge: bitbucket\nmerge:\n  reviewer: copilot\n",
            encoding="utf-8",
        )
        assert main(["policy", str(tmp_path)]) == EXIT_ERROR
        assert "GitHub-only" in capsys.readouterr().err


class TestInitCommand:
    def test_init_then_verify_roundtrip(self, tmp_path, omater_on_path):
        assert main(["init", str(tmp_path)]) == EXIT_OK
        assert main(["init", str(tmp_path), "--verify"]) == EXIT_OK

    def test_verify_fails_on_unprovisioned_repo(self, tmp_path, capsys):
        assert main(["init", str(tmp_path), "--verify"]) == 1
        assert "DRIFT" in capsys.readouterr().err


class TestHookCommand:
    def test_denies_outside_write_via_stdin(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(
            "sys.stdin",
            __import__("io").StringIO(
                json.dumps(
                    {"tool_name": "Write", "tool_input": {"file_path": "/tmp_x/f"}}
                )
            ),
        )
        assert main(["hook", "pre-tool-use", "--root", str(tmp_path)]) == EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_allows_inside_write_silently(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(
            "sys.stdin",
            __import__("io").StringIO(
                json.dumps(
                    {
                        "tool_name": "Write",
                        "tool_input": {"file_path": str(tmp_path / "ok.txt")},
                    }
                )
            ),
        )
        assert main(["hook", "pre-tool-use", "--root", str(tmp_path)]) == EXIT_OK
        assert capsys.readouterr().out == ""

    def test_garbage_stdin_allows(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO("not json"))
        assert main(["hook", "pre-tool-use", "--root", str(tmp_path)]) == EXIT_OK
        assert capsys.readouterr().out == ""

    def test_failing_stdin_read_allows_without_raising(self, tmp_path, monkeypatch, capsys):
        """An OSError reading stdin must allow, not raise — a raised hook is
        a disarmed fence for that invocation."""

        class BrokenStdin:
            def read(self, *a, **k):
                raise OSError("stdin gone")

        monkeypatch.setattr("sys.stdin", BrokenStdin())
        assert main(["hook", "pre-tool-use", "--root", str(tmp_path)]) == EXIT_OK
        assert capsys.readouterr().out == ""


class TestControlCommand:
    def test_control_writes_to_current_run(self, tmp_path, capsys):
        log = RunLog.create(tmp_path)
        assert main(["control", "resume", "--root", str(tmp_path)]) == EXIT_OK
        assert [c["action"] for c in log.read_controls()] == ["resume"]

    def test_control_without_run_errors(self, tmp_path, capsys):
        assert main(["control", "resume", "--root", str(tmp_path)]) == EXIT_ERROR
        assert "no current run" in capsys.readouterr().err

    def test_control_targets_named_run(self, tmp_path):
        log = RunLog.create(tmp_path, run_id="named-run")
        assert (
            main(["control", "abort", "--root", str(tmp_path), "--run", "named-run"])
            == EXIT_OK
        )
        assert [c["action"] for c in log.read_controls()] == ["abort"]

    def test_symlinked_run_dir_is_rejected(self, tmp_path, capsys):
        """A symlink planted at .omater/runs/<id> must not carry control
        writes outside the runs directory."""
        from claudomater.runlog import runs_root

        RunLog.create(tmp_path, run_id="real-run")
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (runs_root(tmp_path) / "evil-link").symlink_to(outside)
        rc = main(["control", "resume", "--root", str(tmp_path), "--run", "evil-link"])
        assert rc == EXIT_ERROR
        assert "resolves outside" in capsys.readouterr().err
        assert not (outside / "control.jsonl").exists()

    def test_run_path_traversal_is_rejected(self, tmp_path, capsys):
        RunLog.create(tmp_path)
        rc = main(["control", "resume", "--root", str(tmp_path), "--run", "../../evil"])
        assert rc == EXIT_ERROR
        assert "simple name" in capsys.readouterr().err
        assert not (tmp_path / "evil").exists()

    def test_top_level_shorthands(self, tmp_path):
        """`omater resume | abort | approve` — the shapes the notifications name."""
        log = RunLog.create(tmp_path)
        for action in ("resume", "approve", "abort"):
            assert main([action, "--root", str(tmp_path)]) == EXIT_OK
        assert [c["action"] for c in log.read_controls()] == [
            "resume",
            "approve",
            "abort",
        ]


class TestStartCommand:
    def test_start_logs_the_resolved_policy(self, tmp_path, capsys, omater_on_path):
        """AC: changing deployment_type visibly changes the model chain,
        review floor, and CI tier IN THE RUN LOG."""
        main(["init", str(tmp_path)])
        (tmp_path / ".omater.yaml").write_text(
            "project: demo\ndeployment_type: mission-critical\n", encoding="utf-8"
        )
        assert main(["start", str(tmp_path)]) == EXIT_OK
        log = RunLog.adopt(tmp_path)
        policy_events = [e for e in log.events() if e["event"] == "policy"]
        assert len(policy_events) == 1
        policy = policy_events[0]["detail"]
        assert policy["deployment_type"] == "mission-critical"
        assert policy["models"]["sr_review"] == "claude-fable-5"
        assert policy["review_floor"] == "NOTE"
        assert policy["ci_on_push"] == "fast+smoke"
        progress = (log.run_dir / "progress.log").read_text()
        assert "claude-fable-5" in progress and "NOTE" in progress

    def test_start_refuses_provisioning_drift(self, tmp_path, capsys):
        (tmp_path / ".omater.yaml").write_text("project: demo\n", encoding="utf-8")
        assert main(["start", str(tmp_path)]) == EXIT_ERROR
        assert "drift" in capsys.readouterr().err

    def test_start_refuses_second_live_run(self, tmp_path, capsys, omater_on_path):
        main(["init", str(tmp_path)])
        assert main(["start", str(tmp_path)]) == EXIT_OK
        assert main(["start", str(tmp_path)]) == EXIT_ERROR
        assert "live run" in capsys.readouterr().err


class TestNotifyCommand:
    def test_notify_without_webhook_errors(self, tmp_path, capsys, no_user_config):
        assert main(["notify", "RUN-COMPLETE", "hi", "--user-config", no_user_config]) == EXIT_ERROR
        assert "no slack webhook" in capsys.readouterr().err
