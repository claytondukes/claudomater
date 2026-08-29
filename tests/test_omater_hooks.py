"""Write-fence hook: deny-on-recognized out-of-tree writes, provisioning,
drift detection, and `omater init`."""

from __future__ import annotations

import json

from claudomater import hooks
from claudomater.initcmd import GITIGNORE_LINE, run_init, run_verify


def payload(tool, **tool_input):
    return {"tool_name": tool, "tool_input": tool_input, "cwd": None}


class TestWriteToolFence:
    def test_write_inside_root_allowed(self, tmp_path):
        allow, _ = hooks.evaluate_pre_tool_use(
            payload("Write", file_path=str(tmp_path / "src" / "a.py")), tmp_path
        )
        assert allow

    def test_write_outside_root_denied_with_redirect_hint(self, tmp_path):
        allow, reason = hooks.evaluate_pre_tool_use(
            payload("Write", file_path="/tmp_probe/out.txt"), tmp_path
        )
        assert not allow
        assert "scratch" in reason

    def test_edit_outside_root_denied(self, tmp_path):
        allow, _ = hooks.evaluate_pre_tool_use(
            payload("Edit", file_path="/etc/hosts"), tmp_path
        )
        assert not allow

    def test_relative_path_resolved_against_cwd(self, tmp_path):
        p = payload("Write", file_path="../outside.txt")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_relative_cwd_falls_back_to_project_root(self, tmp_path):
        """A relative cwd in the payload is untrusted — resolving against
        the hook process's own working directory would make decisions
        environment-dependent."""
        p = payload("Write", file_path="inside.txt")
        p["cwd"] = "some/relative/dir"
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason  # resolved against root, in-tree
        p = payload("Write", file_path="../escape.txt")
        p["cwd"] = "some/relative/dir"
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow  # root/../escape.txt is out of tree

    def test_scratch_dir_allowed(self, tmp_path):
        allow, _ = hooks.evaluate_pre_tool_use(
            payload(
                "Write", file_path=str(tmp_path / hooks.SCRATCH_SUBDIR / "notes.md")
            ),
            tmp_path,
        )
        assert allow

    def test_deny_hint_names_the_declared_scratch_dir(self, tmp_path):
        allow, reason = hooks.evaluate_pre_tool_use(
            payload("Write", file_path="/tmp_probe/out.txt"),
            tmp_path,
            env={hooks.SCRATCH_ENV: "/var/scratch"},
        )
        assert not allow
        assert "/var/scratch" in reason

    def test_relative_env_scratch_anchors_to_project_root(self, tmp_path):
        """A relative OMATER_SCRATCH_DIR must resolve against the project
        root, not the hook process CWD — CWD-relative interpretation both
        denies legitimate scratch writes AND quietly allows writes into an
        arbitrary CWD-adjacent directory (a fence hole)."""
        import pathlib

        env = {hooks.SCRATCH_ENV: "myscratch"}
        allow, reason = hooks.evaluate_pre_tool_use(
            payload("Write", file_path=str(tmp_path / "myscratch" / "x.txt")),
            tmp_path,
            env=env,
        )
        assert allow, reason
        cwd_scratch = pathlib.Path.cwd() / "myscratch" / "x.txt"
        allow, _ = hooks.evaluate_pre_tool_use(
            payload("Write", file_path=str(cwd_scratch)), tmp_path, env=env
        )
        assert not allow  # the CWD interpretation must NOT be in the allowed set

    def test_declared_env_scratch_allowed(self, tmp_path):
        allow, _ = hooks.evaluate_pre_tool_use(
            payload("Write", file_path="/var/scratch/x.txt"),
            tmp_path,
            env={hooks.SCRATCH_ENV: "/var/scratch"},
        )
        assert allow

    def test_harness_scratchpad_allowed(self, tmp_path):
        allow, _ = hooks.evaluate_pre_tool_use(
            payload("Write", file_path="/private/tmp/claude-501/session/scratchpad/x"),
            tmp_path,
        )
        assert allow

    def test_notebook_path_checked_too(self, tmp_path):
        allow, _ = hooks.evaluate_pre_tool_use(
            payload("NotebookEdit", notebook_path="/tmp_nb/x.ipynb"), tmp_path
        )
        assert not allow


class TestBashFence:
    def test_redirect_outside_root_denied(self, tmp_path):
        allow, reason = hooks.evaluate_pre_tool_use(
            payload("Bash", command="echo hi > /tmp_probe/out.txt"), tmp_path
        )
        assert not allow
        assert "/tmp_probe/out.txt" in reason

    def test_append_redirect_denied(self, tmp_path):
        allow, _ = hooks.evaluate_pre_tool_use(
            payload("Bash", command="date >> /var/log/mine.log"), tmp_path
        )
        assert not allow

    def test_tee_denied(self, tmp_path):
        allow, _ = hooks.evaluate_pre_tool_use(
            payload("Bash", command="cat x | tee /etc/conf"), tmp_path
        )
        assert not allow

    def test_cp_target_denied(self, tmp_path):
        allow, _ = hooks.evaluate_pre_tool_use(
            payload("Bash", command="cp build/out.bin /usr/local/bin/out"), tmp_path
        )
        assert not allow

    def test_mkdir_and_touch_denied(self, tmp_path):
        for cmd in ("mkdir -p /tmp_stuff/dir", "touch /root/marker"):
            allow, _ = hooks.evaluate_pre_tool_use(
                payload("Bash", command=cmd), tmp_path
            )
            assert not allow, cmd

    def test_dd_of_denied(self, tmp_path):
        allow, _ = hooks.evaluate_pre_tool_use(
            payload("Bash", command="dd if=/dev/zero of=/swapfile bs=1M count=1"),
            tmp_path,
        )
        assert not allow

    def test_in_tree_writes_allowed(self, tmp_path):
        for cmd in (
            f"echo hi > {tmp_path}/out.txt",
            "echo hi > relative/out.txt",
            f"mkdir -p {tmp_path}/newdir",
            f"cp a.txt {tmp_path}/b.txt",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert allow, (cmd, reason)

    def test_dev_null_and_stderr_redirects_allowed(self, tmp_path):
        for cmd in ("run --quiet > /dev/null 2>&1", "cmd 2>/dev/null"):
            allow, reason = hooks.evaluate_pre_tool_use(
                payload("Bash", command=cmd), tmp_path
            )
            assert allow, (cmd, reason)

    def test_heredoc_body_is_data_not_commands(self, tmp_path):
        """Writing an in-tree script whose CONTENT mentions an absolute
        redirect must not be denied — heredoc bodies are data."""
        cmd = (
            f"cat > {tmp_path}/deploy.sh <<'EOF'\n"
            "#!/bin/bash\n"
            "echo starting >> /var/log/deploy.log\n"
            "EOF"
        )
        allow, reason = hooks.evaluate_pre_tool_use(
            payload("Bash", command=cmd), tmp_path
        )
        assert allow, reason

    def test_heredoc_intro_redirect_still_denied(self, tmp_path):
        cmd = "cat <<'EOF' > /etc/target\nharmless content\nEOF"
        allow, _ = hooks.evaluate_pre_tool_use(payload("Bash", command=cmd), tmp_path)
        assert not allow

    def test_quoted_strings_are_data(self, tmp_path):
        """A commit message mentioning `> /var/log/x` is prose, not a write."""
        cmd = 'git commit -m "docs: pipe output > /var/log/x explained"'
        allow, reason = hooks.evaluate_pre_tool_use(
            payload("Bash", command=cmd), tmp_path
        )
        assert allow, reason

    def test_unquoted_redirect_next_to_quotes_still_denied(self, tmp_path):
        cmd = 'echo "hello world" > /tmp_probe/out.txt'
        allow, _ = hooks.evaluate_pre_tool_use(payload("Bash", command=cmd), tmp_path)
        assert not allow

    def test_quoted_source_does_not_hide_the_copy_target(self, tmp_path):
        """Quoted args are data but must keep their SLOT: erasing them made
        `cp "a b" /tmp/out` lose its source token and slip past the fence."""
        for cmd in (
            'cp "a b" /tmp_probe/out',
            "mv 'spaced name.txt' /usr/local/bin/x",
        ):
            allow, _ = hooks.evaluate_pre_tool_use(
                payload("Bash", command=cmd), tmp_path
            )
            assert not allow, cmd

    def test_symlinked_root_spellings_compare_equal(self, tmp_path):
        """macOS /tmp -> /private/tmp: a write via the resolved spelling of a
        symlinked root must not be falsely denied (that stalls the phase)."""
        real = tmp_path / "real-project"
        real.mkdir()
        link = tmp_path / "link-project"
        link.symlink_to(real)
        allow, reason = hooks.evaluate_pre_tool_use(
            payload("Write", file_path=str(real / "src" / "a.py")), link
        )
        assert allow, reason
        # and the reverse: linked spelling of the path, real root
        allow, reason = hooks.evaluate_pre_tool_use(
            payload("Write", file_path=str(link / "src" / "a.py")), real
        )
        assert allow, reason

    def test_read_only_commands_allowed(self, tmp_path):
        for cmd in ("cat /etc/hosts", "ls -la /usr/local", "grep -r foo /var/log"):
            allow, _ = hooks.evaluate_pre_tool_use(
                payload("Bash", command=cmd), tmp_path
            )
            assert allow, cmd


class TestPayloadTypeRobustness:
    def test_unrecognized_payload_shapes_allow_without_raising(self, tmp_path):
        """A fence that raises is a fence disarmed (non-2 hook exits allow).
        Unrecognized input — including unexpected TYPES — must allow."""
        weird = [
            ["not", "a", "dict"],
            {"tool_name": "Write", "tool_input": "not-a-dict"},
            {"tool_name": "Write", "tool_input": {"file_path": 42}},
            {"tool_name": "Bash", "tool_input": {"command": ["ls"]}},
            {"tool_name": "Write", "tool_input": {"file_path": "/tmp_x/f"}, "cwd": 7},
        ]
        for payload_obj in weird:
            allow, _ = hooks.evaluate_pre_tool_use(payload_obj, tmp_path)
            # the last case still carries a recognizable bad write; the rest allow
            if payload_obj is weird[-1]:
                assert not allow
            else:
                assert allow, payload_obj


class TestHookResponse:
    def test_allow_has_no_output(self):
        assert hooks.hook_response(True, None) is None

    def test_deny_shape_matches_hook_protocol(self):
        resp = hooks.hook_response(False, "nope")
        out = resp["hookSpecificOutput"]
        assert out["hookEventName"] == "PreToolUse"
        assert out["permissionDecision"] == "deny"
        assert out["permissionDecisionReason"] == "nope"


class TestProvisioning:
    def test_provision_creates_settings(self, tmp_path):
        assert hooks.provision(tmp_path) is True
        settings = json.loads(hooks.settings_path(tmp_path).read_text())
        entry = settings["hooks"]["PreToolUse"][0]
        assert entry["matcher"] == hooks.HOOK_MATCHER
        assert hooks.HOOK_MARKER in entry["hooks"][0]["command"]

    def test_provision_is_idempotent(self, tmp_path):
        assert hooks.provision(tmp_path) is True
        assert hooks.provision(tmp_path) is False
        settings = json.loads(hooks.settings_path(tmp_path).read_text())
        assert len(settings["hooks"]["PreToolUse"]) == 1

    def test_provision_preserves_existing_settings(self, tmp_path):
        path = hooks.settings_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "model": "opus",
                    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]},
                }
            ),
            encoding="utf-8",
        )
        hooks.provision(tmp_path)
        settings = json.loads(path.read_text())
        assert settings["model"] == "opus"
        assert "Stop" in settings["hooks"]
        assert "PreToolUse" in settings["hooks"]

    def test_verify_detects_missing_and_drifted_hook(self, tmp_path):
        assert hooks.verify(tmp_path)  # nothing provisioned yet
        hooks.provision(tmp_path)
        assert hooks.verify(tmp_path) == []
        # drift: someone narrows the matcher
        path = hooks.settings_path(tmp_path)
        settings = json.loads(path.read_text())
        settings["hooks"]["PreToolUse"][0]["matcher"] = "Write"
        path.write_text(json.dumps(settings), encoding="utf-8")
        problems = hooks.verify(tmp_path)
        assert problems and "drifted" in problems[0]

    def test_provision_repairs_drift(self, tmp_path):
        hooks.provision(tmp_path)
        path = hooks.settings_path(tmp_path)
        settings = json.loads(path.read_text())
        settings["hooks"]["PreToolUse"][0]["matcher"] = "Write"
        path.write_text(json.dumps(settings), encoding="utf-8")
        assert hooks.provision(tmp_path) is True
        assert hooks.verify(tmp_path) == []


class TestInit:
    def test_verify_reports_omater_missing_from_path(self, tmp_path, monkeypatch):
        """Command-not-found is exit 127 = allow: a PATH without omater
        silently disarms the fence, so the run-start drift check must say so."""
        run_init(tmp_path)
        emptybin = tmp_path / "emptybin"
        emptybin.mkdir()
        monkeypatch.setenv("PATH", str(emptybin))
        problems = run_verify(tmp_path)
        assert any("not on PATH" in p for p in problems)

    def test_init_provisions_everything_and_verify_passes(self, tmp_path, omater_on_path):
        actions = run_init(tmp_path)
        assert (tmp_path / ".omater.yaml").exists()
        assert hooks.settings_path(tmp_path).exists()
        assert GITIGNORE_LINE in (tmp_path / ".gitignore").read_text().splitlines()
        assert (tmp_path / hooks.SCRATCH_SUBDIR).is_dir()
        assert any("wrote" in a for a in actions)
        assert run_verify(tmp_path) == []

    def test_init_is_idempotent(self, tmp_path):
        run_init(tmp_path)
        (tmp_path / ".omater.yaml").write_text(
            "project: customized\n", encoding="utf-8"
        )
        actions = run_init(tmp_path)
        # a second init must not clobber the customized config
        assert "customized" in (tmp_path / ".omater.yaml").read_text()
        assert any("kept existing" in a for a in actions)
        gitignore_lines = (tmp_path / ".gitignore").read_text().splitlines()
        assert gitignore_lines.count(GITIGNORE_LINE) == 1

    def test_init_force_overwrites_config(self, tmp_path):
        run_init(tmp_path)
        (tmp_path / ".omater.yaml").write_text("project: customized\n", encoding="utf-8")
        run_init(tmp_path, force=True)
        assert "customized" not in (tmp_path / ".omater.yaml").read_text()

    def test_template_config_parses_and_defaults_to_sandbox(self, tmp_path):
        from claudomater.config import load_project_config

        run_init(tmp_path)
        cfg = load_project_config(tmp_path)
        assert cfg.deployment_type == "sandbox"
        assert cfg.project == tmp_path.name

    def test_verify_reports_all_drift(self, tmp_path):
        problems = run_verify(tmp_path)
        assert len(problems) >= 3  # settings, config, gitignore

    def test_unreadable_settings_reports_not_crashes(self, tmp_path):
        hooks.provision(tmp_path)
        path = hooks.settings_path(tmp_path)
        path.chmod(0o000)
        try:
            problems = hooks.verify(tmp_path)
            assert problems and "cannot read" in problems[0]
        finally:
            path.chmod(0o644)

    def test_unreadable_gitignore_reports_not_crashes(self, tmp_path):
        run_init(tmp_path)
        gitignore = tmp_path / ".gitignore"
        gitignore.chmod(0o000)
        try:
            problems = run_verify(tmp_path)
            assert any("cannot read" in p and ".gitignore" in p for p in problems)
        finally:
            gitignore.chmod(0o644)
