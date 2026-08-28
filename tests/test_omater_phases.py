"""Phase runner: structured-result contract, retry-once-then-escalate,
write-ahead logging, wip(phase-crash) salvage, transcript scrub, verifiers."""

from __future__ import annotations

import json
import subprocess

import pytest

from claudomater.config import UserConfig
from claudomater.guardrails import Decision
from claudomater.phases import (
    ExecutionResult,
    PhaseRunner,
    PhaseSpec,
    PhaseTimeout,
    extract_json_result,
    salvage_uncommitted,
)
from claudomater.runlog import RunLog
from claudomater.scrub import scrub_text
from claudomater.verifiers import (
    VerifierContext,
    VerifierError,
    Verdict,
    build,
    files_exist,
    git_worktree_clean,
    result_field,
    run_verifiers,
)


class FakeExecutor:
    """Returns queued outputs; records the model each attempt ran on."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls: list[str] = []

    def run(self, spec, model):
        self.calls.append(model)
        item = self.outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return ExecutionResult(text=item, token_usage={"output_tokens": 10})


class FakeNotifier:
    enabled = True
    last_error = None

    def __init__(self):
        self.sent = []

    def notify(self, kind, message, project=None, detail=None):
        self.sent.append((kind, message))
        return True


GOOD = 'work done\n```json\n{"status": "complete", "lessons_applied": []}\n```\n'
NO_JSON = "I finished everything, trust me."


def make_runner(tmp_path, outputs, **kw):
    log = RunLog.create(tmp_path)
    executor = FakeExecutor(outputs)
    notifier = FakeNotifier()
    runner = PhaseRunner(
        tmp_path, log, executor, notifier=notifier, project="demo", **kw
    )
    return runner, log, executor, notifier


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )


@pytest.fixture
def git_repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    # `omater init` guarantees this in real projects
    (tmp_path / ".gitignore").write_text(".omater/\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


class TestClaudeCliExecutor:
    def test_named_decisions_in_argv_and_cwd(self, tmp_path):
        """Headless `claude -p` denies un-granted tools by default; the
        executor bypasses permissions BY DECISION, with the write fence as
        the containment, and runs in the project root."""
        from claudomater.phases import ClaudeCliExecutor

        ex = ClaudeCliExecutor(cwd=tmp_path)
        argv = ex.build_argv(PhaseSpec("dev", "m", "do the thing"), "claude-opus-5")
        assert argv[:3] == ["claude", "-p", "do the thing"]
        assert "--permission-mode" in argv
        assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
        assert ex.cwd == tmp_path

    def test_permission_mode_is_a_knob(self):
        from claudomater.phases import ClaudeCliExecutor

        argv = ClaudeCliExecutor(permission_mode=None).build_argv(
            PhaseSpec("dev", "m", "p"), "m"
        )
        assert "--permission-mode" not in argv


class TestExtractJsonResult:
    def test_takes_last_json_fence(self):
        text = '```json\n{"a": 1}\n```\nmore\n```json\n{"b": 2}\n```'
        assert extract_json_result(text) == {"b": 2}

    def test_falls_back_to_last_bare_object(self):
        assert extract_json_result('noise {"x": 1} tail {"y": [1,2]} end') == {"y": [1, 2]}

    def test_none_when_no_object(self):
        assert extract_json_result(NO_JSON) is None
        assert extract_json_result("[1,2,3]") is None

    def test_malformed_fence_falls_through(self):
        text = '```json\n{"broken": \n```\n{"ok": true}'
        assert extract_json_result(text) == {"ok": True}


class TestRunPhase:
    def test_success_path(self, tmp_path):
        runner, log, executor, notifier = make_runner(tmp_path, [GOOD])
        outcome = runner.run_phase(
            PhaseSpec("create", "claude-fable-5", "do it", required_fields=("status",))
        )
        assert outcome.status == "verified"
        assert outcome.result["status"] == "complete"
        assert outcome.attempts == 1
        events = [e["event"] for e in log.events()]
        assert events.index("phase-spawn") < events.index("phase-verified")
        assert notifier.sent == []

    def test_write_ahead_spawn_precedes_execution(self, tmp_path):
        """The spawn intent must be in events.jsonl BEFORE the executor runs."""
        log = RunLog.create(tmp_path)
        seen = {}

        class SpyExecutor:
            def run(self, spec, model):
                seen["events_at_exec"] = [e["event"] for e in log.events()]
                return ExecutionResult(text=GOOD)

        runner = PhaseRunner(tmp_path, log, SpyExecutor())
        runner.run_phase(PhaseSpec("dev", "m", "p"))
        assert "phase-spawn" in seen["events_at_exec"]

    def test_missing_json_result_retries_then_escalates(self, tmp_path):
        runner, log, executor, notifier = make_runner(tmp_path, [NO_JSON, NO_JSON])
        outcome = runner.run_phase(PhaseSpec("dev", "m", "p"))
        assert outcome.status == "escalated"
        assert outcome.attempts == 2
        assert all("no-structured-result" in r for r in outcome.failure_reasons)
        events = [e["event"] for e in log.events()]
        assert events.count("phase-failed") == 2
        assert "phase-escalated" in events
        assert notifier.sent and notifier.sent[0][0] == "ESCALATED"

    def test_retry_recovers(self, tmp_path):
        runner, log, executor, _ = make_runner(tmp_path, [NO_JSON, GOOD])
        outcome = runner.run_phase(PhaseSpec("dev", "m", "p"))
        assert outcome.status == "verified"
        assert outcome.attempts == 2

    def test_missing_required_field_fails(self, tmp_path):
        out = 'x\n```json\n{"other": 1}\n```'
        runner, *_ = make_runner(tmp_path, [out, out])
        outcome = runner.run_phase(
            PhaseSpec("dev", "m", "p", required_fields=("status",))
        )
        assert outcome.status == "escalated"
        assert "missing required fields" in outcome.failure_reasons[0]

    def test_nonzero_exit_fails_even_with_json_output(self, tmp_path):
        """A JSON blob in a FAILED executor run's output is not a result —
        the agent errored and the attempt must fail."""

        class FailingExitExecutor:
            def __init__(self):
                self.n = 0

            def run(self, spec, model):
                self.n += 1
                return ExecutionResult(text=GOOD, returncode=1 if self.n == 1 else 0)

        log = RunLog.create(tmp_path)
        runner = PhaseRunner(tmp_path, log, FailingExitExecutor())
        outcome = runner.run_phase(PhaseSpec("dev", "m", "p"))
        assert outcome.status == "verified"
        assert outcome.attempts == 2
        assert "executor-failed: exit 1" in outcome.failure_reasons[0]

    def test_timeout_is_a_failed_attempt(self, tmp_path):
        runner, log, *_ = make_runner(
            tmp_path, [PhaseTimeout("phase 'dev' exceeded 1s"), GOOD]
        )
        outcome = runner.run_phase(PhaseSpec("dev", "m", "p"))
        assert outcome.status == "verified"
        assert "timeout" in outcome.failure_reasons[0]

    def test_verifier_failure_retries_then_escalates(self, tmp_path):
        runner, log, *_ = make_runner(tmp_path, [GOOD, GOOD])
        outcome = runner.run_phase(
            PhaseSpec("dev", "m", "p", verifiers=[{"files_exist": ["missing-file.txt"]}])
        )
        assert outcome.status == "escalated"
        assert "verifier-failed" in outcome.failure_reasons[0]

    def test_token_usage_recorded_in_run_events(self, tmp_path):
        """Cost accounting: every phase records token usage in event detail."""
        runner, log, *_ = make_runner(tmp_path, [GOOD])
        runner.run_phase(PhaseSpec("dev", "m", "p"))
        verified = [e for e in log.events() if e["event"] == "phase-verified"]
        assert verified[0]["detail"]["token_usage"] == {"output_tokens": 10}

    def test_empty_output_still_leaves_a_transcript(self, tmp_path):
        """'The agent produced nothing' is post-mortem information — an
        empty-output attempt must still leave its transcript file."""
        runner, log, *_ = make_runner(tmp_path, ["", GOOD])
        outcome = runner.run_phase(PhaseSpec("dev", "m", "p"))
        assert outcome.status == "verified"
        assert log.transcript_path("dev", 1).exists()
        assert log.transcript_path("dev", 1).read_text() == ""

    def test_transcripts_written_and_scrubbed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_SECRET", "hunter2secret")
        leaky = 'MY_SECRET=hunter2secret\n```json\n{"status": "done"}\n```'
        runner, log, *_ = make_runner(tmp_path, [leaky], secrets_deny=("MY_SECRET",))
        runner.run_phase(PhaseSpec("dev", "m", "p"))
        transcript = log.transcript_path("dev", 1).read_text()
        assert "hunter2secret" not in transcript
        assert "[REDACTED:MY_SECRET]" in transcript

    def test_skip_model_skips_the_phase(self, tmp_path):
        runner, log, executor, _ = make_runner(tmp_path, [GOOD])
        outcome = runner.run_phase(PhaseSpec("sr-review", "skip", "p"))
        assert outcome.status == "skipped"
        assert executor.calls == []
        assert "phase-skipped" in [e["event"] for e in log.events()]

    def test_timeout_still_leaves_a_scrubbed_transcript(self, tmp_path, monkeypatch):
        """The attempts most in need of a post-mortem are the ones that
        timed out — their partial output must be retained (scrubbed)."""
        monkeypatch.setenv("MY_SECRET", "hunter2secret")
        timeout = PhaseTimeout("phase 'dev' exceeded 1s", partial_text="leak hunter2secret mid-work")
        runner, log, *_ = make_runner(tmp_path, [timeout, GOOD], secrets_deny=("MY_SECRET",))
        outcome = runner.run_phase(PhaseSpec("dev", "m", "p"))
        assert outcome.status == "verified"
        partial = log.transcript_path("dev", 1).read_text()
        assert "mid-work" in partial
        assert "hunter2secret" not in partial

    def test_verifier_detail_is_scrubbed_in_events_and_notification(
        self, tmp_path, monkeypatch
    ):
        """Verifier output (e.g. a test gauntlet's stderr) can quote secrets;
        nothing unscrubbed may reach events.jsonl or Slack."""
        monkeypatch.setenv("MY_SECRET", "hunter2secret")

        def leaky_verifier(ctx):
            return Verdict("command_ok", False, "stderr said: MY_SECRET=hunter2secret")

        runner, log, _, notifier = make_runner(
            tmp_path, [GOOD, GOOD], secrets_deny=("MY_SECRET",)
        )
        runner.run_phase(PhaseSpec("dev", "m", "p", verifiers=[leaky_verifier]))
        raw_events = (log.run_dir / "events.jsonl").read_text()
        assert "hunter2secret" not in raw_events
        assert all("hunter2secret" not in msg for _, msg in notifier.sent)
        assert notifier.sent  # the ESCALATED notification did fire

    def test_crashing_verifier_is_a_failed_check_not_a_crash(self, tmp_path):
        def broken(ctx):
            raise RuntimeError("verifier blew up")

        runner, log, *_ = make_runner(tmp_path, [GOOD, GOOD])
        outcome = runner.run_phase(PhaseSpec("dev", "m", "p", verifiers=[broken]))
        assert outcome.status == "escalated"  # contained, logged, escalated
        assert "verifier blew up" in outcome.failure_reasons[0]

    def test_unknown_declarative_verifier_is_contained(self, tmp_path):
        runner, log, *_ = make_runner(tmp_path, [GOOD, GOOD])
        outcome = runner.run_phase(
            PhaseSpec("dev", "m", "p", verifiers=["no_such_verifier"])
        )
        assert outcome.status == "escalated"
        assert "could not run" in outcome.failure_reasons[0]

    def test_salvage_commits_dirty_tree_before_retry(self, git_repo):
        class DirtyingExecutor:
            def __init__(self):
                self.n = 0

            def run(self, spec, model):
                self.n += 1
                if self.n == 1:
                    (git_repo / "wip.txt").write_text("half-done", encoding="utf-8")
                    return ExecutionResult(text=NO_JSON)  # crash-shaped ending
                return ExecutionResult(text=GOOD)

        log = RunLog.create(git_repo)
        runner = PhaseRunner(git_repo, log, DirtyingExecutor())
        outcome = runner.run_phase(PhaseSpec("dev", "m", "p"))
        assert outcome.status == "verified"
        head_msg = git(git_repo, "log", "-1", "--format=%s").stdout
        assert "wip(phase-crash)" in head_msg
        assert "salvage-committed" in [e["event"] for e in log.events()]


class TestGuardrailGate:
    def test_retry_spawns_are_gated_too(self, tmp_path):
        """No NEW phase spawns after a threshold trips — a retry is a new
        spawn. A pause landing between attempt 1 and 2 must block attempt 2."""
        decisions = iter(
            [
                Decision(action="ok"),
                Decision(action="pause", reasons=["5h crossed 95% mid-attempt"]),
            ]
        )
        runner, log, executor, notifier = make_runner(
            tmp_path, [NO_JSON, GOOD], guardrail_check=lambda: next(decisions)
        )
        outcome = runner.run_phase(PhaseSpec("dev", "claude-opus-5", "p"))
        assert outcome.status == "paused"
        assert len(executor.calls) == 1  # attempt 2 never spawned
        assert notifier.sent[0][0] == "PAUSED-QUOTA"

    def test_degrade_landing_between_attempts_applies_to_the_retry(self, tmp_path):
        decisions = iter(
            [Decision(action="ok"), Decision(action="degrade", window="seven_day")]
        )
        runner, log, executor, _ = make_runner(
            tmp_path, [NO_JSON, GOOD], guardrail_check=lambda: next(decisions)
        )
        outcome = runner.run_phase(PhaseSpec("dev", "claude-fable-5", "p"))
        assert outcome.status == "verified"
        assert executor.calls == ["claude-fable-5", "claude-opus-5"]

    def test_pause_blocks_spawn_and_notifies(self, tmp_path):
        runner, log, executor, notifier = make_runner(
            tmp_path,
            [GOOD],
            guardrail_check=lambda: Decision(action="pause", reasons=["5h at 97%"]),
        )
        outcome = runner.run_phase(PhaseSpec("dev", "claude-opus-5", "p"))
        assert outcome.status == "paused"
        assert executor.calls == []  # no NEW phase spawns after the threshold trips
        assert notifier.sent[0][0] == "PAUSED-QUOTA"
        assert "phase-paused" in [e["event"] for e in log.events()]

    def test_degrade_swaps_model_and_notifies(self, tmp_path):
        runner, log, executor, notifier = make_runner(
            tmp_path,
            [GOOD],
            user_config=UserConfig(),
            guardrail_check=lambda: Decision(action="degrade", reasons=["scoped at 85%"]),
        )
        outcome = runner.run_phase(PhaseSpec("dev", "claude-fable-5", "p"))
        assert outcome.status == "verified"
        assert executor.calls == ["claude-opus-5"]  # degraded, visibly
        assert notifier.sent[0][0] == "DEGRADED"
        assert "phase-degraded" in [e["event"] for e in log.events()]

    def test_escalated_story_pauses_instead_of_degrading(self, tmp_path):
        runner, log, executor, notifier = make_runner(
            tmp_path,
            [GOOD],
            guardrail_check=lambda: Decision(action="degrade", reasons=["scoped"]),
        )
        outcome = runner.run_phase(
            PhaseSpec("dev", "claude-fable-5", "p", escalated=True)
        )
        assert outcome.status == "paused"
        assert executor.calls == []
        assert "never runs degraded" in outcome.pause_reason


class TestFinalSalvage:
    def test_dirty_tree_is_committed_when_escalating(self, git_repo):
        """The final attempt may die mid-write too — escalation must leave
        the branch committed for whoever picks the story up."""

        class AlwaysDirtyExecutor:
            def run(self, spec, model):
                (git_repo / "half.txt").write_text("partial", encoding="utf-8")
                return ExecutionResult(text=NO_JSON)

        log = RunLog.create(git_repo)
        runner = PhaseRunner(git_repo, log, AlwaysDirtyExecutor())
        outcome = runner.run_phase(PhaseSpec("dev", "m", "p"))
        assert outcome.status == "escalated"
        assert not git(git_repo, "status", "--porcelain").stdout.strip()

    def test_salvage_intent_logged_before_the_commit(self, git_repo):
        """Write-ahead applies to salvage too: the intent precedes the git op."""

        class DirtyOnce:
            def __init__(self):
                self.n = 0

            def run(self, spec, model):
                self.n += 1
                if self.n == 1:
                    (git_repo / "wip.txt").write_text("x", encoding="utf-8")
                    return ExecutionResult(text=NO_JSON)
                return ExecutionResult(text=GOOD)

        log = RunLog.create(git_repo)
        PhaseRunner(git_repo, log, DirtyOnce()).run_phase(PhaseSpec("dev", "m", "p"))
        events = [e["event"] for e in log.events()]
        assert events.index("salvage-attempt") < events.index("salvage-committed")


class TestSalvageUncommitted:
    def test_clean_tree_is_a_noop(self, git_repo):
        assert salvage_uncommitted(git_repo) is False

    def test_dirty_tree_gets_wip_commit(self, git_repo):
        (git_repo / "b.txt").write_text("b", encoding="utf-8")
        assert salvage_uncommitted(git_repo) is True
        assert not git(git_repo, "status", "--porcelain").stdout.strip()

    def test_non_git_dir_is_a_noop(self, tmp_path):
        assert salvage_uncommitted(tmp_path) is False

    def test_failed_add_leaves_no_staged_state(self, git_repo):
        """A partially-failed `git add -A` must not leave earlier entries
        staged — salvage is side-effect-free unless it succeeds."""
        (git_repo / "stageable.txt").write_text("ok", encoding="utf-8")
        unreadable = git_repo / "unreadable.txt"
        unreadable.write_text("secret", encoding="utf-8")
        unreadable.chmod(0o000)
        try:
            assert salvage_uncommitted(git_repo) is False
            staged = git(git_repo, "diff", "--cached", "--name-only").stdout.strip()
            assert staged == ""
        finally:
            unreadable.chmod(0o644)

    def test_failed_commit_leaves_no_staged_state(self, git_repo):
        """Salvage must be side-effect-free unless it succeeds: a failing
        commit (e.g. a hook) must not leave `add -A`'s staging behind."""
        hook = git_repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        (git_repo / "wip.txt").write_text("half", encoding="utf-8")
        assert salvage_uncommitted(git_repo) is False
        staged = git(git_repo, "diff", "--cached", "--name-only").stdout.strip()
        assert staged == ""
        assert (git_repo / "wip.txt").read_text() == "half"  # worktree untouched

    def test_run_logs_never_ride_along_even_without_gitignore(self, tmp_path):
        """Belt-and-braces: in a repo missing the .omater gitignore, salvage
        must still not commit run logs, and a repo dirty ONLY in .omater is
        not salvage-worthy."""
        git(tmp_path, "init", "-q")
        git(tmp_path, "config", "user.email", "t@t")
        git(tmp_path, "config", "user.name", "t")
        (tmp_path / "a.txt").write_text("a", encoding="utf-8")
        git(tmp_path, "add", "-A")
        git(tmp_path, "commit", "-q", "-m", "init")
        logs = tmp_path / ".omater" / "runs" / "r1"
        logs.mkdir(parents=True)
        (logs / "events.jsonl").write_text("{}\n", encoding="utf-8")
        assert salvage_uncommitted(tmp_path) is False  # only .omater is dirty

        (tmp_path / "wip.txt").write_text("half", encoding="utf-8")
        assert salvage_uncommitted(tmp_path) is True
        committed = git(tmp_path, "show", "--name-only", "--format=", "HEAD").stdout
        assert "wip.txt" in committed
        assert ".omater" not in committed


class TestVerifiers:
    def test_files_exist_globs(self, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "story.md").write_text("s", encoding="utf-8")
        ctx = VerifierContext(project_root=tmp_path)
        assert files_exist("docs/*.md")(ctx).ok
        assert not files_exist("docs/*.rst")(ctx).ok

    def test_result_field(self, tmp_path):
        ctx = VerifierContext(project_root=tmp_path, result={"status": "complete"})
        assert result_field("status", "complete")(ctx).ok
        assert not result_field("status", "other")(ctx).ok
        assert not result_field("missing")(ctx).ok

    def test_git_worktree_clean(self, git_repo):
        ctx = VerifierContext(project_root=git_repo)
        assert git_worktree_clean()(ctx).ok
        (git_repo / "dirty.txt").write_text("d", encoding="utf-8")
        verdict = git_worktree_clean()(ctx)
        assert not verdict.ok
        assert "dirty.txt" in verdict.detail

    def test_build_from_declarative_entries(self, tmp_path):
        (tmp_path / "x.md").write_text("x", encoding="utf-8")
        ok, verdicts = run_verifiers(
            [{"files_exist": ["x.md"]}, {"result_field": {"name": "status"}}],
            VerifierContext(project_root=tmp_path, result={"status": "done"}),
        )
        assert ok and len(verdicts) == 2

    def test_build_rejects_unknown(self):
        with pytest.raises(VerifierError, match="unknown verifier"):
            build("no_such_verifier")

    def test_callable_passes_through(self, tmp_path):
        marker = Verdict("custom", True)
        ok, verdicts = run_verifiers(
            [lambda ctx: marker], VerifierContext(project_root=tmp_path)
        )
        assert ok and verdicts == [marker]


class TestScrub:
    def test_env_value_redacted_everywhere(self, monkeypatch):
        out = scrub_text(
            "the token abc123secret leaked",
            ["API_KEY"],
            env={"API_KEY": "abc123secret"},
        )
        assert "abc123secret" not in out
        assert "[REDACTED:API_KEY]" in out

    def test_assignment_forms_redacted_without_env(self):
        out = scrub_text(
            'API_KEY=topsecretvalue and API_KEY: "othervalue"', ["API_KEY"], env={}
        )
        assert "topsecretvalue" not in out
        assert "othervalue" not in out

    def test_quoted_multiword_value_fully_redacted(self):
        out = scrub_text('API_KEY="two words secret"', ["API_KEY"], env={})
        assert "two words" not in out
        assert "[REDACTED:API_KEY]" in out

    def test_token_shapes_always_redacted(self):
        out = scrub_text("Authorization: Bearer abcdefghijklmnop123456", [], env={})
        assert "abcdefghijklmnop123456" not in out
        out2 = scrub_text("key sk-ant-abc123def456 here", [], env={})
        assert "sk-ant-abc123def456" not in out2

    def test_short_env_values_not_shredded(self):
        # a 1-char secret value must not cause every 'a' to be redacted
        out = scrub_text("a normal sentence", ["X"], env={"X": "a"})
        assert out == "a normal sentence"
