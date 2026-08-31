"""Pre-commit read-only guard (Phase 3 deliverable 1).

The guard blocks a PHASE AGENT's commits outside the run's declared write
scope, at the git layer, with the fence's P1-1 discipline (run-scoped,
agent-gated on OMATER_PHASE_AGENT == "1") and - unlike the fence - a
FAIL-CLOSED contract while gated: a gated commit must never pass unchecked,
including when omater itself is missing or the scope file is gone.

The integration tests here run REAL `git commit` invocations through the
installed hook script, because the deliverable's failure modes live in the
composition (shell gating, exec exit codes, git's temp index for pathspec
commits), not in any Python function taken alone.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from claudomater import commitguard
from claudomater.commitguard import GuardError

SRC = Path(__file__).resolve().parent.parent / "src"


def git(cwd: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )


def try_git(cwd: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True
    )


def head_sha(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def hermetic_git(monkeypatch):
    """Machine independence (the push.autoSetupRemote lesson, PR #14 round
    2): a developer's global git config - core.hooksPath especially - must
    not decide whether these tests pass. /dev/null'ing global+system config
    applies to in-process guard calls (they inherit os.environ) and to the
    subprocess commits below alike."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)


@pytest.fixture
def repo(tmp_path, hermetic_git):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q")
    git(r, "config", "user.email", "guard-test@example.invalid")
    git(r, "config", "user.name", "Guard Test")
    (r / "ui").mkdir()
    (r / "ui" / "app.ts").write_text("export {}\n", encoding="utf-8")
    (r / "CLAUDE.md").write_text("process rules\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


@pytest.fixture
def shim_bin(tmp_path_factory):
    """An `omater` on PATH that runs THIS checkout - the installed hook
    script execs `omater` by name, and the tests must exercise that exact
    last mile rather than calling hook_main() in-process."""
    bin_dir = tmp_path_factory.mktemp("guard-bin")
    shim = bin_dir / "omater"
    shim.write_text(
        "#!/bin/sh\n"
        f'export PYTHONPATH="{SRC}${{PYTHONPATH:+:$PYTHONPATH}}"\n'
        f'exec "{sys.executable}" -m claudomater "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return bin_dir


def commit_env(bin_dir: Path | None = None, marker: str | None = "1") -> dict:
    env = dict(os.environ)
    if bin_dir is not None:
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    if marker is None:
        env.pop("OMATER_PHASE_AGENT", None)
    else:
        env["OMATER_PHASE_AGENT"] = marker
    return env


class TestScopeGrammar:
    def test_prefixes_match_on_segment_boundaries(self):
        scope = ["ui"]
        assert commitguard.in_scope("ui/app.ts", scope)
        assert commitguard.in_scope("ui", scope)
        # `ui2/` riding the `ui` prefix is how a sibling directory would
        # silently join the declared scope
        assert not commitguard.in_scope("ui2/app.ts", scope)
        assert not commitguard.in_scope("uix", scope)

    def test_dot_declares_the_whole_repo(self):
        assert commitguard.in_scope("anything/at/all", ["."])

    def test_normalization_strips_dot_slash_and_trailing_slash(self):
        assert commitguard.normalize_scope(["./ui/", "docs"]) == ["ui", "docs"]

    def test_absolute_tilde_and_traversal_entries_fail_loudly(self):
        for bad in ["/etc", "~/x", "a/../b"]:
            with pytest.raises(GuardError):
                commitguard.normalize_scope([bad])

    def test_non_string_and_blank_entries_fail_loudly(self):
        for bad in [None, 3, "", "   "]:
            with pytest.raises(GuardError):
                commitguard.normalize_scope([bad])


class TestArming:
    def test_arm_installs_hook_and_scope_and_is_idempotent(self, repo):
        assert commitguard.arm(repo, ["ui"]) is True
        gd = commitguard.git_dir(repo)
        hook = gd / "hooks" / "pre-commit"
        assert hook.exists() and os.access(hook, os.X_OK)
        scope = json.loads((gd / commitguard.SCOPE_BASENAME).read_text())
        assert scope["scope"] == ["ui"]
        assert commitguard.arm(repo, ["ui"]) is False  # nothing to change

    def test_rearm_with_additions_is_the_driver_seam(self, repo):
        """A driver re-arms with per-run additions (the story file, say);
        the scope file must follow."""
        commitguard.arm(repo, ["ui"])
        assert commitguard.arm(repo, ["ui", "docs/story.md"]) is True
        gd = commitguard.git_dir(repo)
        scope = json.loads((gd / commitguard.SCOPE_BASENAME).read_text())
        assert scope["scope"] == ["ui", "docs/story.md"]

    def test_arm_refuses_a_foreign_hook(self, repo):
        hook = commitguard.git_dir(repo) / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        with pytest.raises(GuardError, match="foreign"):
            commitguard.arm(repo, ["ui"])

    def test_arm_refuses_a_redirected_hooks_dir(self, repo, tmp_path):
        """core.hooksPath pointing at a SHARED hooks dir: installing a
        run-scoped guard there would gate other repos' commits (the P1-1
        shape), and installing to .git/hooks anyway would arm a hook git
        never reads (the silent-disarm shape). Refuse, loudly."""
        shared = tmp_path / "shared-hooks"
        shared.mkdir()
        git(repo, "config", "core.hooksPath", str(shared))
        with pytest.raises(GuardError, match="core.hooksPath"):
            commitguard.arm(repo, ["ui"])

    def test_arm_accepts_hookspath_naming_the_repos_own_default(self, repo):
        """Measured on the real target 2026-08-31: ui3 sets core.hooksPath
        to its own absolute .git/hooks. Git reads hooks from exactly where
        the guard installs, so refusing on the config key's mere presence
        would refuse the primary target repo for nothing."""
        gd = commitguard.git_dir(repo)
        git(repo, "config", "core.hooksPath", str(gd / "hooks"))
        assert commitguard.arm(repo, ["ui"]) is True
        assert commitguard.verify(repo, require=True) == []

    def test_arm_on_a_plain_directory_raises(self, tmp_path, hermetic_git):
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(GuardError):
            commitguard.arm(plain, ["ui"])

    def test_disarm_removes_only_ours_and_is_idempotent(self, repo):
        commitguard.arm(repo, ["ui"])
        assert commitguard.disarm(repo) is True
        gd = commitguard.git_dir(repo)
        assert not (gd / "hooks" / "pre-commit").exists()
        assert not (gd / commitguard.SCOPE_BASENAME).exists()
        assert commitguard.disarm(repo) is False

    def test_disarm_leaves_a_foreign_hook_alone(self, repo):
        hook = commitguard.git_dir(repo) / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        assert commitguard.disarm(repo) is False
        assert hook.exists()

    def test_disarm_raises_when_the_guard_was_replaced_while_armed(self, repo):
        """Our scope file under a foreign hook means someone swapped the
        guard out mid-run - teardown must not tidy that into silence."""
        commitguard.arm(repo, ["ui"])
        hook = commitguard.git_dir(repo) / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        with pytest.raises(GuardError, match="replaced"):
            commitguard.disarm(repo)

    def test_a_corrupt_scope_file_is_healed_by_rearm(self, repo):
        """Copilot round-1 finding: the idempotence-comparison reads sat
        outside any handler, so a scope file with undecodable bytes made
        arm() crash with a raw UnicodeDecodeError instead of doing its
        job. The scope file is OUR run state and arm() is authoritative:
        an unreadable one compares as 'changed' and gets rewritten."""
        commitguard.arm(repo, ["ui"])
        scope_file = commitguard.git_dir(repo) / commitguard.SCOPE_BASENAME
        scope_file.write_bytes(b"\xff\xfe garbage")
        assert commitguard.arm(repo, ["ui"]) is True
        assert json.loads(scope_file.read_text())["scope"] == ["ui"]

    def test_filesystem_failures_in_arm_and_disarm_are_typed(
        self, repo, monkeypatch
    ):
        """Copilot round-1 finding (+ neighborhood): os.chmod in arm() and
        the unlinks in disarm() raised raw OSError past start_run's and
        teardown's typed GuardError handling - a readonly hooks dir
        crashed with a traceback instead of a user-facing error."""
        def boom(*_a, **_k):
            raise OSError("simulated readonly filesystem")

        monkeypatch.setattr(commitguard.os, "chmod", boom)
        with pytest.raises(GuardError, match="cannot"):
            commitguard.arm(repo, ["ui"])
        monkeypatch.undo()
        commitguard.arm(repo, ["ui"])
        monkeypatch.setattr(commitguard.Path, "unlink", boom)
        with pytest.raises(GuardError, match="cannot"):
            commitguard.disarm(repo)

    def test_rearm_restores_executability(self, repo):
        """Matching bytes with a stripped exec bit is a hook git silently
        skips - re-arming must repair the mode, not just the content."""
        commitguard.arm(repo, ["ui"])
        hook = commitguard.git_dir(repo) / "hooks" / "pre-commit"
        hook.chmod(0o644)
        commitguard.arm(repo, ["ui"])
        assert os.access(hook, os.X_OK)


class TestVerifyDrift:
    def test_healthy_armed_guard_verifies_clean(self, repo):
        commitguard.arm(repo, ["ui"])
        assert commitguard.verify(repo, require=True) == []

    def test_require_reports_a_missing_hook(self, repo):
        assert any(
            "not armed" in p for p in commitguard.verify(repo, require=True)
        )

    def test_leftover_scope_without_hook_reports_in_both_modes(self, repo):
        commitguard.arm(repo, ["ui"])
        (commitguard.git_dir(repo) / "hooks" / "pre-commit").unlink()
        for require in (True, False):
            assert any(
                "left behind" in p
                for p in commitguard.verify(repo, require=require)
            ), require

    def test_drifted_hook_bytes_report(self, repo):
        commitguard.arm(repo, ["ui"])
        hook = commitguard.git_dir(repo) / "hooks" / "pre-commit"
        hook.write_text(
            hook.read_text(encoding="utf-8") + "# tweak\n", encoding="utf-8"
        )
        hook.chmod(0o755)
        assert any("drifted" in p for p in commitguard.verify(repo, require=True))

    def test_non_executable_hook_reports(self, repo):
        commitguard.arm(repo, ["ui"])
        (commitguard.git_dir(repo) / "hooks" / "pre-commit").chmod(0o644)
        assert any(
            "not executable" in p for p in commitguard.verify(repo, require=True)
        )

    def test_missing_or_malformed_scope_file_reports(self, repo):
        commitguard.arm(repo, ["ui"])
        scope_file = commitguard.git_dir(repo) / commitguard.SCOPE_BASENAME
        scope_file.unlink()
        assert any(
            "scope file missing" in p
            for p in commitguard.verify(repo, require=True)
        )
        scope_file.write_text("not json", encoding="utf-8")
        assert any(
            "unreadable" in p for p in commitguard.verify(repo, require=True)
        )

    def test_between_runs_absent_is_healthy_and_leftover_reports(self, repo):
        assert commitguard.verify(repo, require=False) == []
        commitguard.arm(repo, ["ui"])
        assert any(
            "left behind" in p for p in commitguard.verify(repo, require=False)
        )

    def test_between_runs_a_foreign_hook_is_not_our_business(self, repo):
        hook = commitguard.git_dir(repo) / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        assert commitguard.verify(repo, require=False) == []

    def test_a_replaced_hook_reports_on_the_run_path(self, repo):
        commitguard.arm(repo, ["ui"])
        hook = commitguard.git_dir(repo) / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        assert any(
            "replaced" in p for p in commitguard.verify(repo, require=True)
        )

    def test_a_hookspath_flip_mid_run_is_drift(self, repo, tmp_path):
        """core.hooksPath set AFTER arming redirects git away from our
        hook while every guard file sits untouched - the guard is disarmed
        with nothing on disk changed. require=True must catch it."""
        commitguard.arm(repo, ["ui"])
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        git(repo, "config", "core.hooksPath", str(elsewhere))
        assert any(
            "core.hooksPath" in p for p in commitguard.verify(repo, require=True)
        )

    def test_non_repo_is_healthy_between_runs_and_a_problem_on_the_run_path(
        self, tmp_path, hermetic_git
    ):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert commitguard.verify(plain, require=False) == []
        assert commitguard.verify(plain, require=True) != []


class TestEvaluateFailClosed:
    def test_in_scope_staged_changes_allow(self, repo):
        commitguard.arm(repo, ["ui"])
        (repo / "ui" / "new.ts").write_text("x\n", encoding="utf-8")
        git(repo, "add", "ui/new.ts")
        allow, reason = commitguard.evaluate(repo)
        assert allow and reason is None

    def test_out_of_scope_staged_changes_deny_naming_everything(self, repo):
        commitguard.arm(repo, ["ui"])
        (repo / "CLAUDE.md").write_text("edited\n", encoding="utf-8")
        (repo / "rogue.txt").write_text("x\n", encoding="utf-8")
        git(repo, "add", "CLAUDE.md", "rogue.txt")
        allow, reason = commitguard.evaluate(repo)
        assert not allow
        assert "CLAUDE.md" in reason and "rogue.txt" in reason
        assert "ui" in reason  # the declared scope is part of the message

    def test_an_empty_scope_denies_and_says_none_was_declared(self, repo):
        commitguard.arm(repo, [])
        (repo / "ui" / "new.ts").write_text("x\n", encoding="utf-8")
        git(repo, "add", "ui/new.ts")
        allow, reason = commitguard.evaluate(repo)
        assert not allow
        assert "declared no committable paths" in reason

    def test_a_missing_scope_file_denies(self, repo):
        commitguard.arm(repo, ["ui"])
        (commitguard.git_dir(repo) / commitguard.SCOPE_BASENAME).unlink()
        (repo / "ui" / "new.ts").write_text("x\n", encoding="utf-8")
        git(repo, "add", "ui/new.ts")
        allow, reason = commitguard.evaluate(repo)
        assert not allow and "fail-closed" in reason

    def test_a_malformed_scope_file_denies(self, repo):
        commitguard.arm(repo, ["ui"])
        scope_file = commitguard.git_dir(repo) / commitguard.SCOPE_BASENAME
        (repo / "ui" / "new.ts").write_text("x\n", encoding="utf-8")
        git(repo, "add", "ui/new.ts")
        for bad in ("not json", "[]", '{"scope": "ui"}', '{"scope": [1]}'):
            scope_file.write_text(bad, encoding="utf-8")
            allow, reason = commitguard.evaluate(repo)
            assert not allow and "fail-closed" in reason, bad

    def test_nothing_staged_allows(self, repo):
        commitguard.arm(repo, ["ui"])
        allow, _ = commitguard.evaluate(repo)
        assert allow

    def test_deleting_an_out_of_scope_file_denies(self, repo):
        """A deletion is a write to the repo's history like any other."""
        commitguard.arm(repo, ["ui"])
        git(repo, "rm", "-q", "CLAUDE.md")
        allow, reason = commitguard.evaluate(repo)
        assert not allow and "CLAUDE.md" in reason

    def test_a_rename_out_of_scope_shows_both_sides(self, repo):
        """--no-renames is load-bearing: with rename detection on, moving
        a guarded file INTO scope surfaces only the in-scope destination,
        and the deletion of the out-of-scope original rides through
        invisibly."""
        commitguard.arm(repo, ["ui"])
        git(repo, "mv", "CLAUDE.md", "ui/CLAUDE.md")
        allow, reason = commitguard.evaluate(repo)
        assert not allow
        assert "CLAUDE.md" in reason  # the deleted original, outside scope

    def test_a_newline_in_a_filename_stays_one_path(self, repo):
        """-z is load-bearing: newline-split output would read `ui/a\\nb`
        as two paths, one of them out of scope - a false deny here, and
        smuggling room in the mirror case."""
        weird = repo / "ui" / "a\nb.txt"
        weird.write_text("x\n", encoding="utf-8")
        git(repo, "add", "ui")
        commitguard.arm(repo, ["ui"])
        allow, reason = commitguard.evaluate(repo)
        assert allow, reason


class TestGitCommitIntegration:
    """The full composition, through real `git commit` and the installed
    script: shell gating, PATH resolution, exec exit codes, git's temp
    index. These are the slice's acceptance proofs."""

    def test_gated_out_of_scope_commit_is_blocked(self, repo, shim_bin):
        commitguard.arm(repo, ["ui"])
        before = head_sha(repo)
        (repo / "CLAUDE.md").write_text("agent edit\n", encoding="utf-8")
        git(repo, "add", "CLAUDE.md")
        proc = try_git(
            repo, "commit", "-m", "out of scope", env=commit_env(shim_bin)
        )
        assert proc.returncode != 0
        assert "BLOCKED" in proc.stderr and "CLAUDE.md" in proc.stderr
        assert head_sha(repo) == before  # nothing landed

    def test_gated_in_scope_commit_lands(self, repo, shim_bin):
        commitguard.arm(repo, ["ui"])
        before = head_sha(repo)
        (repo / "ui" / "feature.ts").write_text("ok\n", encoding="utf-8")
        git(repo, "add", "ui/feature.ts")
        proc = try_git(
            repo, "commit", "-qm", "in scope", env=commit_env(shim_bin)
        )
        assert proc.returncode == 0, proc.stderr
        assert head_sha(repo) != before

    def test_operator_commits_pass_untouched(self, repo, shim_bin):
        """No marker, or a non-canonical marker ('0'): the hook exits 0
        before invoking omater at all - a human's commits are never gated
        (P1-1), even when out of the declared scope."""
        commitguard.arm(repo, ["ui"])
        for marker in (None, "0"):
            (repo / "CLAUDE.md").write_text(
                f"operator edit {marker}\n", encoding="utf-8"
            )
            git(repo, "add", "CLAUDE.md")
            proc = try_git(
                repo,
                "commit",
                "-qm",
                "operator",
                env=commit_env(shim_bin, marker=marker),
            )
            assert proc.returncode == 0, (marker, proc.stderr)

    def test_a_missing_omater_blocks_gated_commits(self, repo):
        """FAIL-CLOSED through the last mile: with omater not on PATH the
        script's exec fails nonzero and git aborts. A gated commit must
        never pass unchecked - this is where the fence's fail-open rule
        deliberately inverts."""
        commitguard.arm(repo, ["ui"])
        (repo / "ui" / "f.ts").write_text("x\n", encoding="utf-8")
        git(repo, "add", "ui/f.ts")
        env = commit_env(None)
        env["PATH"] = "/usr/bin:/bin"  # git lives here; omater does not
        proc = try_git(repo, "commit", "-m", "gated, no omater", env=env)
        assert proc.returncode != 0

    def test_a_missing_scope_file_blocks_gated_commits(self, repo, shim_bin):
        commitguard.arm(repo, ["ui"])
        (commitguard.git_dir(repo) / commitguard.SCOPE_BASENAME).unlink()
        (repo / "ui" / "f.ts").write_text("x\n", encoding="utf-8")
        git(repo, "add", "ui/f.ts")
        proc = try_git(repo, "commit", "-m", "no scope", env=commit_env(shim_bin))
        assert proc.returncode != 0
        assert "fail-closed" in proc.stderr

    def test_pathspec_commits_judge_the_temp_index(self, repo, shim_bin):
        """`git commit -- path` commits from a TEMPORARY index git exposes
        to hooks via GIT_INDEX_FILE (measured: the env value can be a
        relative path). The guard must judge exactly what this commit will
        contain - not the full staging area."""
        commitguard.arm(repo, ["ui"])
        (repo / "ui" / "f.ts").write_text("x\n", encoding="utf-8")
        (repo / "CLAUDE.md").write_text("agent edit\n", encoding="utf-8")
        git(repo, "add", "ui/f.ts", "CLAUDE.md")
        ok = try_git(
            repo, "commit", "-qm", "just ui", "--", "ui/f.ts",
            env=commit_env(shim_bin),
        )
        assert ok.returncode == 0, ok.stderr  # CLAUDE.md staged but not in THIS commit
        blocked = try_git(
            repo, "commit", "-m", "the rest", "--", "CLAUDE.md",
            env=commit_env(shim_bin),
        )
        assert blocked.returncode != 0
        assert "CLAUDE.md" in blocked.stderr


class TestHookMainGate:
    def test_ungated_returns_zero_without_touching_anything(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("OMATER_PHASE_AGENT", raising=False)
        # not even a git repo: the gate exits before any evaluation
        assert commitguard.hook_main(tmp_path) == 0

    def test_non_canonical_marker_is_ungated(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OMATER_PHASE_AGENT", "0")
        assert commitguard.hook_main(tmp_path) == 0

    def test_gated_deny_exits_nonzero_with_the_reason(
        self, repo, monkeypatch, capsys
    ):
        commitguard.arm(repo, [])
        (repo / "ui" / "f.ts").write_text("x\n", encoding="utf-8")
        git(repo, "add", "ui/f.ts")
        monkeypatch.setenv("OMATER_PHASE_AGENT", "1")
        assert commitguard.hook_main(repo) == 1
        assert "BLOCKED" in capsys.readouterr().err

    def test_gated_internal_errors_block(self, repo, monkeypatch, capsys):
        monkeypatch.setenv("OMATER_PHASE_AGENT", "1")

        def boom(_root):
            raise RuntimeError("simulated")

        monkeypatch.setattr(commitguard, "evaluate", boom)
        assert commitguard.hook_main(repo) == 1
        assert "fail-closed" in capsys.readouterr().err


class TestRunComposition:
    """start_run arms, a failed start rolls back, teardown clears, and
    `omater init --verify` reports leftovers - the same lifecycle the
    fence proved for P1-1, now for the guard."""

    def _project(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        git(root, "init", "-q")
        git(root, "config", "user.email", "t@example.invalid")
        git(root, "config", "user.name", "T")
        art = root / "art"
        art.mkdir()
        git(art, "init", "-q")
        git(art, "config", "user.email", "t@example.invalid")
        git(art, "config", "user.name", "T")
        (root / ".omater.yaml").write_text(
            "project: guardproj\n"
            "artifact_roots: [art]\n"
            "commit_scope:\n"
            '  ".": [src]\n'
            "  art: [docs]\n",
            encoding="utf-8",
        )
        (root / ".gitignore").write_text(
            ".omater/\n.pytest_cache/\n", encoding="utf-8"
        )
        return root, art

    def test_start_run_arms_every_declared_repo_and_logs_the_scopes(
        self, tmp_path, hermetic_git, omater_on_path
    ):
        from claudomater.run import start_run

        root, art = self._project(tmp_path)
        log, _cfg = start_run(root)
        try:
            for repo_path in (root, art):
                assert commitguard.verify(repo_path, require=True) == []
            events = [
                e for e in log.events() if e.get("event") == "commit-guard"
            ]
            assert events and events[0]["detail"]["repos"] == {
                ".": ["src"],
                "art": ["docs"],
            }
        finally:
            log.finish("run-aborted", {"reason": "test teardown"})

    def test_teardown_cli_clears_root_and_artifact_repos(
        self, tmp_path, hermetic_git, omater_on_path
    ):
        from claudomater.cli import main
        from claudomater.run import start_run

        root, art = self._project(tmp_path)
        log, _cfg = start_run(root)
        log.finish("run-aborted", {"reason": "test teardown"})
        assert main(["teardown", str(root)]) == 0
        for repo_path in (root, art):
            assert commitguard.verify(repo_path, require=False) == []
            assert not (
                commitguard.git_dir(repo_path) / "hooks" / "pre-commit"
            ).exists()

    def test_a_failed_start_does_not_leave_the_guard_armed(
        self, tmp_path, hermetic_git, omater_on_path, monkeypatch
    ):
        from claudomater import run as run_mod
        from claudomater.run import start_run
        from claudomater.runlog import RunError

        root, art = self._project(tmp_path)

        def boom(_root, run_id=None):
            raise RunError("simulated create failure")

        monkeypatch.setattr(run_mod.RunLog, "create", boom)
        with pytest.raises(RunError, match="simulated"):
            start_run(root)
        for repo_path in (root, art):
            assert commitguard.verify(repo_path, require=False) == [], repo_path

    def test_a_live_runs_guard_survives_a_conflicting_start(
        self, tmp_path, hermetic_git, omater_on_path
    ):
        from claudomater.run import start_run
        from claudomater.runlog import RunError

        root, _art = self._project(tmp_path)
        log, _cfg = start_run(root)
        try:
            with pytest.raises(RunError):
                start_run(root)  # one-live-run conflict
            assert commitguard.verify(root, require=True) == []
        finally:
            log.finish("run-aborted", {"reason": "test teardown"})

    def test_teardown_without_config_reports_the_artifact_roots_it_cannot_reach(
        self, tmp_path, hermetic_git, omater_on_path, capsys
    ):
        """Guard evidence at the root + an unreadable config = artifact
        roots we cannot enumerate, and possibly a guard still armed in one.
        Exit nonzero and say so - and the warning is HONEST: the artifact
        repo really does keep its guard here."""
        from claudomater.cli import main
        from claudomater.run import start_run

        root, art = self._project(tmp_path)
        log, _cfg = start_run(root)
        log.finish("run-aborted", {"reason": "test teardown"})
        (root / ".omater.yaml").unlink()
        assert main(["teardown", str(root)]) != 0
        assert "NOT checked" in capsys.readouterr().err
        assert commitguard.verify(art, require=False) != []  # really left armed

    def test_configless_teardown_with_no_guard_stays_a_quiet_success(
        self, tmp_path, hermetic_git
    ):
        """The fence alone never needed config: tearing down a dir where
        only the fence was armed (or nothing at all) must keep exiting 0,
        or every pre-guard workflow breaks."""
        from claudomater import hooks
        from claudomater.cli import main

        plain = tmp_path / "plain"
        plain.mkdir()
        hooks.provision(plain)
        assert main(["teardown", str(plain)]) == 0

    def test_init_verify_reports_a_leftover_guard(
        self, tmp_path, hermetic_git, omater_on_path
    ):
        from claudomater.initcmd import run_init, run_verify

        root, art = self._project(tmp_path)
        run_init(root)  # keeps the existing config, tops up gitignore
        commitguard.arm(art, ["docs"])
        problems = run_verify(root)
        assert any("left behind" in p for p in problems)
