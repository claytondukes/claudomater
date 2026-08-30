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

    def test_relative_target_honors_in_command_cd(self, tmp_path):
        """The Phase 0.5 measured false deny: `cd <root>/server && cat >
        ../.omater/scratch/x` resolved the redirect against the SESSION cwd
        (repo root) instead of server/, landing one level ABOVE the repo —
        denied, though the real target was in-root scratch."""
        (tmp_path / "server").mkdir()
        cmd = f"cd {tmp_path}/server && cat > ../.omater/scratch/probe.py"
        p = payload("Bash", command=cmd)
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_cd_tracking_also_catches_true_out_of_tree_writes(self, tmp_path):
        """The same tracking that fixes the false deny makes the deny
        smarter: a relative write after cd-ing out of the tree is now a
        RECOGNIZED out-of-tree write."""
        cmd = "cd /etc && cat > passwd"
        p = payload("Bash", command=cmd)
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_untrackable_cwd_fails_open_never_falsely_denies(self, tmp_path):
        """Deny-on-recognized: when the effective cwd is unknowable, a
        RELATIVE target is not a recognized out-of-tree write — allow."""
        for cmd in (
            "cd $BUILD_DIR && cat > out.txt",  # variable cd target
            'cd "some dir" && cat > out.txt',  # quoted cd target (placeholder)
            "cd - && cat > out.txt",  # OLDPWD
            "pushd /tmp && popd && cat > out.txt",  # popd
            "(cd /tmp && echo hi) && cat > out.txt",  # subshell scoping
            "cd $(mktemp -d) && cat > out.txt",  # command substitution
            "cd /etc | cat > out.txt",  # pipeline segment = subshell
            "cd /etc & cat > out.txt",  # backgrounded cd = subshell
            "false || cd /etc; cat > out.txt",  # ||-guarded: conditional
            "cd /definitely-missing || cat > out.txt",  # || RHS = cd FAILED
            "cd /etc >/dev/null & cat > out.txt",  # redirect hid the `&`
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert allow, (cmd, reason)

    def test_absolute_targets_deny_regardless_of_cwd_tracking(self, tmp_path):
        """Losing the cwd must not disarm the fence for absolute targets."""
        cmd = "cd $BUILD_DIR && cat > /tmp_probe/evil.txt"
        p = payload("Bash", command=cmd)
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_relative_cd_chain_tracks_and_write_inside_subshell_denies(self, tmp_path):
        (tmp_path / "a" / "b").mkdir(parents=True)
        # chained relative cds stay in-tree -> allowed
        p = payload("Bash", command="cd a && cd b && cat > out.txt")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason
        # a write INSIDE `(cd /tmp && ...)` is linearly tracked -> denied
        p = payload("Bash", command="(cd /tmp_probe && cat > evil.txt)")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_redirect_on_the_cd_command_itself_is_pre_cd(self, tmp_path):
        """Bash opens redirections BEFORE running the builtin: `cd /etc >
        cd.log` creates ./cd.log in the pre-cd cwd. Resolving it post-cd
        falsely denied an in-tree write."""
        p = payload("Bash", command="cd /etc > cd.log")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason
        # ...while a target in the NEXT segment is post-cd and still denies
        p = payload("Bash", command="cd /etc > cd.log && cat > shadow")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_pushd_n_does_not_move_the_cwd(self, tmp_path):
        """pushd -n updates the directory stack only — the cwd stays put, so
        a following relative write is still in-tree (was a false deny)."""
        p = payload("Bash", command="pushd -n /etc && cat > out.txt")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason
        # plain pushd still tracks
        p = payload("Bash", command="pushd /etc && cat > out.txt")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_redirect_does_not_stop_cd_from_applying_across_and_and(self, tmp_path):
        """`cd /etc >/dev/null && cat > x`: the redirect belongs to the cd,
        but the && boundary still applies the cd for the next segment."""
        p = payload("Bash", command="cd /etc >/dev/null && cat > x")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_comment_text_is_not_scanned_as_commands(self, tmp_path):
        """`cd <root>/server # note; cd /etc` — bash ignores everything from
        the unquoted `#`; scanning it applied the /etc cd and falsely denied
        the in-root scratch write."""
        (tmp_path / "server").mkdir()
        cmd = (
            f"cd {tmp_path}/server # note; cd /etc\n"
            "cat > ../.omater/scratch/x"
        )
        p = payload("Bash", command=cmd)
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason
        # a hash inside a word is NOT a comment: the redirect still denies
        p = payload("Bash", command="cat > /tmp_probe/file#1")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_comment_after_a_metacharacter_is_a_comment_too(self, tmp_path):
        """`echo ok;# ignored > /etc/x` — the # after `;` starts a comment;
        scanning its text falsely denied a redirect bash never executes."""
        p = payload("Bash", command="echo ok;# ignored > /etc/x\ncat > out.txt")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_cdpath_makes_bare_relative_cd_targets_untrackable(self, tmp_path):
        """CDPATH rewires where `cd target` lands (search path, not cwd) —
        one-shot prefix or inherited env. Bare relative cd targets go
        unknown (fail open); ./-anchored and absolute targets bypass CDPATH
        per bash and keep tracking (and denying)."""
        (tmp_path / "server").mkdir()
        # one-shot prefix: tracker must NOT assume <root>/target
        p = payload("Bash", command="CDPATH=/tmp_probe cd target && cat > out.txt")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason
        # inherited CDPATH: same rule via env
        env = {"CDPATH": "/tmp_probe"}
        p = payload("Bash", command="cd server && cat > out.txt")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path, env=env)
        assert allow, reason
        # absolute cd is CDPATH-immune: still tracked, still denies
        p = payload("Bash", command="cd /etc && cat > passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path, env=env)
        assert not allow
        # ./-anchored target bypasses CDPATH: tracked, in-root scratch allows
        p = payload(
            "Bash", command="cd ./server && cat > ../.omater/scratch/x"
        )
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path, env=env)
        assert allow, reason

    def test_cd_needs_a_token_boundary(self, tmp_path):
        """`cd/etc; cat > out.txt` runs a command NAMED cd/etc — bash stays
        in the project root and out.txt is in-tree; parsing it as `cd /etc`
        falsely denied the write."""
        p = payload("Bash", command="cd/etc; cat > out.txt")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_assignment_prefixed_cd_is_tracked(self, tmp_path):
        """`MODE=x cd dir` legally prefixes the builtin — missing it would
        re-open the original stale-cwd false deny with an env prefix."""
        (tmp_path / "server").mkdir()
        cmd = f"MODE=x cd {tmp_path}/server && cat > ../.omater/scratch/x"
        p = payload("Bash", command=cmd)
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason
        p = payload("Bash", command="MODE=x cd /etc && cat > passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_unrecognized_cd_forms_void_tracking_not_silently_ignored(self, tmp_path):
        """Invariant: every cd-ish token either tracks correctly or voids
        the cwd. An unmodeled form (quoted assignment value, `command cd`)
        left silently untracked would resolve later relative targets against
        a STALE cwd — the original false-deny shape."""
        (tmp_path / "server").mkdir()
        for cmd in (
            f'MODE="a b" cd {tmp_path}/server && cat > ../.omater/scratch/x',
            f"command cd {tmp_path}/server && cat > ../.omater/scratch/x",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert allow, (cmd, reason)

    def test_heredoc_introducer_inside_data_is_not_a_heredoc(self, tmp_path):
        """A << inside a comment or quoted span starts no heredoc — eating
        the following lines as a body hid a real out-of-tree write."""
        for cmd in (
            "echo ok # <<EOF\ncat > /tmp_probe/real\nEOF",
            "echo '<<EOF'\ncat > /tmp_probe/real\nEOF",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert not allow, cmd

    def test_escaped_metachars_are_word_data_everywhere(self, tmp_path):
        """Escaped separators/operators are arguments, not syntax:
        `echo \\; cd /etc` never runs cd (false deny closed), and `echo \\&`
        / `echo \\\\`` must not void a correctly tracked cwd (miss closed)."""
        p = payload("Bash", command="echo \\; cd /etc; cat > out.txt")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason
        for cmd in (
            "cd /etc; echo \\&; cat > passwd",
            "cd /etc; echo \\`; cat > passwd",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert not allow, cmd

    def test_quote_inside_comment_cannot_swallow_executable_lines(self, tmp_path):
        """Quotes and comments share ONE lexical state: a quote that is
        comment text opens no span. Masking quotes before blanking comments
        let `echo ok # "` swallow the following lines up to the next quote,
        hiding a real out-of-tree write (false ALLOW) — top-level, after a
        group-closing `)`, inside backticks, and via a `\\`-continuation
        (a comment ends at its newline; the backslash is comment text)."""
        for cmd in (
            'echo ok # "\ncat > /tmp_probe/real\necho "done"',
            '(echo ok)# "\ncat > /tmp_probe/real\necho "done"',
            'echo `true # "`\ncat > /tmp_probe/real\necho "done"',
            "echo ok # note \\\ncat > /tmp_probe/real",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert not allow, cmd
        # and the inverse stays fixed: a # inside quotes is data, so the
        # redirect after the closing quote is still recognized
        p = payload("Bash", command='echo "a # b" > /tmp_probe/real')
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_arithmetic_shift_and_here_string_are_not_heredocs(self, tmp_path):
        """`$((1 << EOF))` is a shift and `<<<` a here-string — parsing
        either as a heredoc introducer ate the following executable lines
        as body (false ALLOW), and a shift raising the residual-<< wall
        hid a real redirect on its own line."""
        for cmd in (
            "echo $((1 << EOF))\ncat > /tmp_probe/real\nEOF",
            "cat <<<EOF\ncat > /tmp_probe/real\nEOF",
            "echo $((1<<2)) > /tmp_probe/real",
            "(( x << 2 ))\ncat > /tmp_probe/real\n2",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert not allow, cmd

    def test_heredoc_prepass_shares_the_lexers_data_rules(self, tmp_path):
        """The heredoc pre-pass and the main lexer must agree on what is
        data: an ANSI-C escaped quote ($'fake \\' <<EOF') and a comment
        after a group-closing paren ((echo ok)# <<EOF) both make the
        <<EOF data — a pre-pass missing either rule took it as a live
        introducer and ate the next line's real write as heredoc body."""
        for cmd in (
            "echo $'fake \\' <<EOF'\ncat > /tmp_probe/real\nEOF",
            "(echo ok)# <<EOF\ncat > /tmp_probe/real\nEOF",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert not allow, cmd

    def test_pure_arithmetic_parens_do_not_void_the_tracked_cwd(self, tmp_path):
        """Arithmetic evaluation cannot move the shell's cwd: treating
        `((x++))` / `$((x+1))` parens as opacity let `cd /etc; ((x++));
        cat > passwd` resolve its relative write against a 'lost' cwd
        (fail open = ALLOW). A nested command substitution inside the
        arithmetic keeps the span's full opacity."""
        for cmd in (
            "cd /etc; ((x++)); cat > passwd",
            "cd /etc; echo $((x+1)); cat > passwd",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert not allow, cmd
        p = payload("Bash", command="cd /etc; ((x = $(id -u) )); cat > passwd")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason  # nested substitution: conservative fail open

    def test_glob_write_targets_fail_open_never_falsely_deny(self, tmp_path):
        """An unquoted glob/brace in a write target expands at RUNTIME:
        from the parent dir, `> <roo>?/out.txt` can uniquely expand back
        INTO the root while the literal text resolves out-of-tree — a
        false deny. Same rule as cd targets: not a literal path, fail
        open (absolute or relative)."""
        glob_name = tmp_path.name[:-1] + "?"
        for cmd in (
            f"cd {tmp_path.parent} && echo x > {glob_name}/out.txt",
            "echo x > /tmp_probe/rea?/out.txt",
            "echo x > /tmp_probe/{a,b}/out.txt",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert allow, (cmd, reason)

    def test_create_option_semantics_are_per_verb(self, tmp_path):
        """touch -m is a FLAG (set mtime, no argument) — sharing mkdir's
        -m exemption let `touch -m /etc/passwd` through unrecognized.
        mkdir -m still takes an argument and fails open."""
        p = payload("Bash", command="touch -m /etc/passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow
        p = payload("Bash", command=f"cd /etc; mkdir -m 755 {tmp_path}/safe")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_copy_verb_is_the_matched_command_not_prefix_text(self, tmp_path):
        """`X=cp rsync -t src /tmp_probe/out`: the verb is rsync (-t =
        --times) — finding 'cp' in the assignment prefix applied the
        target-directory exemption and let the real destination
        through."""
        p = payload("Bash", command="X=cp rsync -t src /tmp_probe/out")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_escaped_backslash_newline_is_a_real_boundary(self, tmp_path):
        """`echo \\\\<nl>cd /etc; cat > passwd`: the first backslash
        escapes the second, so the newline is a REAL command boundary and
        the cd runs — joining unconditionally glued the next line into
        echo's argument and missed the out-of-tree write."""
        p = payload("Bash", command="echo \\\\\ncd /etc; cat > passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_dangling_substitution_openers_reject_the_input(self, tmp_path):
        """An unclosed backtick or ( hits EOF: bash rejects the whole
        input and executes NOTHING — the still-scannable absolute
        redirect was a false deny (same rule as unterminated quotes and
        unbalanced arithmetic)."""
        for cmd in (
            "echo `ls; printf x > /tmp_probe/out",
            "(echo x; printf x > /tmp_probe/out",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert allow, (cmd, reason)

    def test_rsync_dash_t_is_times_not_target_directory(self, tmp_path):
        """rsync -t preserves TIMES — the last operand is still the
        destination and must keep denying (the cp/mv/install -t
        exemption over-applied and let the out-of-tree dest through)."""
        p = payload("Bash", command="rsync -t src /tmp_probe/out")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_option_arguments_are_not_operands(self, tmp_path):
        """`mkdir -m 755 <root>/safe` consumes 755 as -m's argument — the
        only write is in-root, and resolving 755 against /etc falsely
        denied it. Arg-taking option shapes fail open; plain flags keep
        denying."""
        for cmd in (
            f"cd /etc; mkdir -m 755 {tmp_path}/safe",
            f"cd /etc; touch -r ref {tmp_path}/safe",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert allow, (cmd, reason)
        p = payload("Bash", command="cd /etc; mkdir -p passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_multi_operand_writers_check_every_operand(self, tmp_path):
        """tee/mkdir/touch take multiple operands — checking only the
        first let `cd /etc; tee <root>/ok passwd` open /etc/passwd
        unrecognized. All-in-root operand lists still pass."""
        for cmd in (
            f"cd /etc; tee {tmp_path}/ok passwd",
            f"cd /etc; touch {tmp_path}/ok passwd",
            f"cd /etc; mkdir {tmp_path}/ok passwd",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert not allow, cmd
        p = payload("Bash", command=f"tee {tmp_path}/a {tmp_path}/b")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_redirect_ampersand_does_not_anchor_command_words(self, tmp_path):
        """`echo hi >& tee /tmp_probe/x` redirects to a FILE named tee —
        /tmp_probe/x is just an echo argument; matching _TEE at the
        redirect's & falsely denied it."""
        p = payload("Bash", command="echo hi >& tee /tmp_probe/x")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_target_directory_copies_fail_open(self, tmp_path):
        """`cp -t <root>/dest hosts` writes INTO dest — the last operand
        is a SOURCE, and resolving it as the destination falsely denied
        it as /etc/hosts. Unrecognized, fail open; plain copies keep
        denying."""
        (tmp_path / "dest").mkdir()
        p = payload("Bash", command=f"cd /etc; cp -t {tmp_path}/dest hosts")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason
        p = payload("Bash", command="cd /etc; cp x passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_unterminated_quote_is_a_syntax_error_not_a_write(self, tmp_path):
        """`echo " > /tmp_probe/out` hits EOF inside the quote: bash
        rejects the whole input and writes NOTHING — the exposed redirect
        was a false deny. The unterminated span is data to EOF (same rule
        as unbalanced arithmetic)."""
        for cmd in ('echo " > /tmp_probe/out', "echo ' > /tmp_probe/out"):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert allow, (cmd, reason)

    def test_brace_group_anchor_requires_trailing_whitespace(self, tmp_path):
        """`echo foo{[[ x > /tmp_probe/out ]]` is a plain word plus a
        REAL redirect — bash's brace-group reserved word needs whitespace
        after {, and anchoring a comparison span on the mid-word { masked
        the write. A real group-open `{ [[ ... ]]` still masks its
        comparison (no false deny)."""
        p = payload("Bash", command="echo foo{[[ x > /tmp_probe/out ]]")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow
        p = payload("Bash", command="cd /etc; { [[ x > passwd ]]; }; echo done")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_command_word_patterns_require_command_position(self, tmp_path):
        """`echo mkdir passwd` PRINTS words — matching the argument
        resolved a phantom target against the tracked /etc (false deny).
        Real command-position forms, wrapper- and pipe-prefixed included,
        keep denying."""
        for cmd in (
            "cd /etc; echo mkdir passwd",
            "cd /etc; echo cp x passwd",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert allow, (cmd, reason)
        for cmd in (
            "cd /etc; mkdir passwd",
            "cd /etc; sudo mkdir passwd",
            "echo x | tee /tmp_probe/t",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert not allow, cmd

    def test_escaped_redirect_operator_is_an_argument(self, tmp_path):
        """`test x \\> passwd` passes a LITERAL > — reporting passwd as a
        write falsely denied it against the tracked /etc; the unescaped
        form is a real redirect and keeps denying."""
        p = payload("Bash", command="cd /etc; test x \\> passwd")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason
        p = payload("Bash", command="cd /etc; test x > passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_negated_comparison_contexts_are_still_comparisons(self, tmp_path):
        """`! [[ x > passwd ]]` and `! (( x > passwd ))` negate a
        COMPARISON — nothing is written; the `!` reserved word keeps
        command position and the phantom redirects were false denies."""
        for cmd in (
            "cd /etc; ! [[ x > passwd ]]; echo done",
            "cd /etc; ! (( x > passwd )); echo done",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert allow, (cmd, reason)

    def test_concatenated_quoted_delimiter_reaches_the_wall(self, tmp_path):
        """bash's delimiter for <<'EOF'x is the CONCATENATED word EOFx —
        matching the quoted prefix let an in-body EOF line terminate the
        heredoc early and falsely denied the never-executed redirect.
        Unsupported concatenations reach the residual-<< wall."""
        cmd = "cat <<'EOF'x\nEOF\ncat > /tmp_probe/not-executed\nEOFx"
        p = payload("Bash", command=cmd)
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_cond_open_requires_a_live_anchor(self, tmp_path):
        """`echo \\; [[ x > /tmp_probe/out ]]` anchors on word data: the
        [[ is an ARGUMENT and the > a REAL out-of-tree redirect — masking
        it as a comparison hid the write."""
        p = payload("Bash", command="echo \\; [[ x > /tmp_probe/out ]]")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_escaped_pipe_never_forms_a_two_char_operator(self, tmp_path):
        """`\\|&` is word data + a real background `&` (not |&), and
        `\\||` is word data + a real pipe (not ||): each misread either
        kept a stale cwd (false deny) or voided a known one (miss).
        Prev-char exemptions apply only when the prev char is itself
        unescaped."""
        # the & backgrounds the list; cat writes in the ORIGINAL cwd
        p = payload("Bash", command="cd /etc && echo \\|& cat > out.txt")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason
        for cmd in (
            # real pipeline RHS: source is subshell-scoped, /etc keeps denying
            "cd /etc; echo \\|| source env.sh; cat > passwd",
            # foreground cd after the backgrounded echo: /etc applies
            "echo \\|& cd /etc; cat > passwd",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert not allow, cmd

    def test_dashdash_makes_target_slot_dash_n_an_operand(self, tmp_path):
        """`pushd -- -n` enters a directory NAMED -n (the -- terminator
        ends options) — reading it as the no-chdir option kept a stale
        cwd and falsely denied the in-root scratch write; the dash-target
        branch fails open instead. A real `pushd -n` (option) still
        keeps the cwd tracked (and denying)."""
        p = payload("Bash", command="pushd -- -n; cat > ../.omater/scratch/x")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason
        p = payload("Bash", command="cd /etc; pushd -n; cat > passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_span_containment_lookups_stay_subquadratic(self, tmp_path):
        """Target filtering, the residual-<< wall skip, and the
        arithmetic cd checks each rescanned the full span list per item —
        O(N^2) in the synchronous hook for generated scripts mixing
        arithmetic, redirects, cds, and unsupported heredoc markers. The
        bisect span lookup keeps them logarithmic."""
        import time

        cmd = "".join(
            f"echo $(({i}+1)) > f{i}; cd .; echo <<@{i}\n" for i in range(5000)
        )
        p = payload("Bash", command=cmd)
        p["cwd"] = str(tmp_path)
        started = time.monotonic()
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert time.monotonic() - started < 1.5
        assert allow, reason  # in-root targets, then walls (fail open)

    def test_unbalanced_arithmetic_is_a_syntax_error_not_a_write(self, tmp_path):
        """`(( x > /tmp_probe/out` never closes: bash reads to EOF hunting
        for )) and rejects the whole input — nothing executes, so the
        exposed `>` was a false deny. The unmatched remainder is data."""
        for cmd in (
            "(( x > /tmp_probe/out",
            "echo $(( x > /tmp_probe/out",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert allow, (cmd, reason)

    def test_conditional_comparison_is_not_a_redirect(self, tmp_path):
        """`[[ x > passwd ]]` compares strings — nothing is written, and
        the phantom target was falsely denied against the kept /etc cwd.
        A real redirect after ]] still denies."""
        p = payload("Bash", command="cd /etc; [[ x > passwd ]]; echo done")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason
        p = payload("Bash", command="cd /etc; [[ x > y ]] > passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_redirect_operand_cd_keeps_tracking(self, tmp_path):
        """`< cd` reads FROM a file named cd — no chdir runs, so the
        known /etc must stay tracked and the recognized write denied
        (voiding on the operand token hid it)."""
        p = payload("Bash", command="cd /etc; < cd; cat > passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_pipeline_scoped_state_changes_keep_the_parent_cwd(self, tmp_path):
        """source/eval, an expanded command word, and set -P inside a
        pipeline run in the pipeline's subshell — the parent's cwd and
        options are untouched (same rule as pipeline cds). Voiding them
        hid the recognized /etc write."""
        for cmd in (
            "cd /etc; true | source env.sh; cat > passwd",
            "cd /etc; source env.sh | true; cat > passwd",
            "cd /etc; true | $CMD; cat > passwd",
            "cd /etc; true | set -P; cd ..; cat > passwd",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert not allow, cmd

    def test_prefix_of_unsupported_delimiter_is_not_the_delimiter(self, tmp_path):
        """bash's delimiter for <<END@MARK is the WHOLE word — capturing
        the END prefix let an in-body `END` line terminate the heredoc
        early and exposed the never-executed redirect (false deny).
        Unsupported delimiter words must reach the residual-<< wall."""
        cmd = "cat <<END@MARK\nEND\ncat > /tmp_probe/not-executed\nEND@MARK"
        p = payload("Bash", command=cmd)
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_subshell_function_definition_body_never_executes(self, tmp_path):
        """`deploy() (cd /etc; cat > shadow)` only DEFINES deploy —
        applying the body's cd falsely denied shadow under a cwd the
        shell never entered. Same hard opacity as the `name() { ... }`
        brace form. An INVOKED subshell still executes: its write keeps
        denying."""
        p = payload(
            "Bash", command="deploy() (cd /etc; cat > shadow); cat > out.txt"
        )
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason
        p = payload("Bash", command="(cd /etc; cat > shadow)")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_heredoc_data_filter_is_linear_in_span_count(self, tmp_path):
        """The heredoc pre-pass rescanned every data span per match —
        quadratic on generated scripts full of quoted lines and heredocs
        in the synchronous hook. Sorted spans + a cursor keep it linear."""
        import time

        cmd = "".join(
            f'cat <<EOF > f{i}\n"line a" x\n"line b" y\nEOF\n' for i in range(4000)
        )
        p = payload("Bash", command=cmd)
        p["cwd"] = str(tmp_path)
        started = time.monotonic()
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert time.monotonic() - started < 1.5
        assert allow, reason  # every f{i} is a relative in-root write

    def test_cd_fallback_scan_is_linear_in_cd_count(self, tmp_path):
        """The _CD_WORD fallback rescanned every matched verb span per
        token — O(N^2) in the cd count inside the synchronous hook. A
        start-position set keeps it linear."""
        import time

        cmd = "; ".join(["cd ."] * 10000) + "; cat > out.txt"
        p = payload("Bash", command=cmd)
        p["cwd"] = str(tmp_path)
        started = time.monotonic()
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert time.monotonic() - started < 1.5
        assert allow, reason  # 'cd .' chains stay in-root

    def test_arithmetic_comparison_is_not_a_redirect(self, tmp_path):
        """`(( x > passwd ))` compares — nothing is written, and the
        phantom target was falsely denied against the (correctly) kept
        /etc cwd. A real redirect AFTER the closing )) still denies."""
        p = payload("Bash", command="cd /etc; (( x > passwd )); echo done")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason
        p = payload("Bash", command="cd /etc; (( x > 2 )) > passwd; echo done")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_escaped_whitespace_in_prefix_words_stays_one_word(self, tmp_path):
        """`MODE=a\\ b cd <dir>` is one assignment word and the cd RUNS —
        str.split() shattered it, the stray fragment read as a command
        word, and the un-voided stale /etc falsely denied the in-root
        scratch write. An escaped BACKSLASH before the blank
        (`MODE=a\\\\ b`) really ends the word: b is the command and its
        argument cd must leave tracking alone."""
        (tmp_path / "server").mkdir()
        cmd = f"cd /etc; MODE=a\\ b cd {tmp_path}/server; cat > ../.omater/scratch/x"
        p = payload("Bash", command=cmd)
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason
        p = payload("Bash", command="cd /etc; MODE=a\\\\ b cd; cat > passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_spaced_redirect_prefix_keeps_cd_at_command_position(self, tmp_path):
        """`> /dev/null cd <dir>` redirects and then RUNS cd — checking
        the spaced operand as an independent command word rejected the
        prefix, kept the stale /etc cwd, and falsely denied the in-root
        write. The unmatched cd now voids (fail open), never goes stale;
        a cd as a plain argument (`echo x 2>&1 cd`) still leaves tracking
        alone."""
        p = payload(
            "Bash", command=f"cd /etc; > /dev/null cd {tmp_path}; cat > out.txt"
        )
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason
        p = payload("Bash", command="cd /etc; echo x 2>&1 cd; cat > passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_redirect_ampersand_is_not_a_command_anchor(self, tmp_path):
        """`echo hi >& cd /etc` redirects to a FILE named cd — no chdir.
        Anchoring the chdir match on the redirect's & applied /etc and
        falsely denied the in-root write; the unmatched-cd back-scan
        honors the same rule so a tracked cwd is not voided either."""
        p = payload("Bash", command="echo hi >& cd /etc; cat > out.txt")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason
        p = payload("Bash", command="cd /etc; echo hi >& cd; cat > passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_tilde_after_home_assignment_fails_open(self, tmp_path):
        """An in-command HOME assignment changes what `~` means; the
        hook's expanduser reads its own stale HOME. `HOME=<root>;
        echo > ~/f` and `HOME=<root>; cd ~` land IN-root — resolving
        through the old HOME falsely denied both. Without a HOME
        assignment, `~` still resolves (and denies)."""
        for cmd in (
            f"HOME={tmp_path}; echo hi > ~/out.txt",
            f"HOME={tmp_path}; cd ~; cat > out.txt",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert allow, (cmd, reason)
        p = payload("Bash", command="echo hi > ~/omater-tilde-probe2.txt")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_identifiers_inside_arithmetic_are_not_builtins(self, tmp_path):
        """`((cd /etc))` evaluates the arithmetic expression `cd / etc` —
        no chdir happens, and applying the target falsely denied the
        in-root write. A cd token inside arithmetic must not void
        tracking either: `cd /etc; ((cd /tmp))` keeps /etc and the later
        relative write stays recognized."""
        p = payload("Bash", command="((cd /etc)); cat > passwd")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason
        p = payload("Bash", command="cd /etc; ((cd /tmp)); cat > passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_escaped_separators_are_not_command_anchors_anywhere(self, tmp_path):
        """The parity rule applies at EVERY command-position scan: an
        escaped `;`/`|` is word data, so the word after it is an ARGUMENT
        (`echo \\; source x` runs no source) — anchoring opacity there
        cleared a known /etc and hid the recognized write. One shape per
        affected scanner: shell-exec, compound, glued, ||, &&-list-end,
        set -P."""
        for cmd in (
            "cd /etc && echo \\; source x && cat > passwd",
            "cd /etc && echo \\; if x && cat > passwd",
            "cd /etc && echo \\; $CMD && cat > passwd",
            "cd /etc && echo \\|| true && cat > passwd",
            "true && cd /etc && echo \\; && cat > passwd",
            "cd /etc && echo \\; set -P && cd .. && cat > passwd",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert not allow, cmd

    def test_quotes_inside_nested_substitution_never_falsely_deny(self, tmp_path):
        """Bash gives $(...) its own quote context inside double quotes:
        in `echo "$(echo "> /tmp_probe/x")"` the `>` is quoted argument
        data — pairing the outer quote with the nested one left it
        scannable and falsely denied the command. Recursive parsing is
        frozen out, so the misparse SHAPE (unclosed opener or backtick in
        the scanned content) classifies the remainder as unrecognized:
        fail open. Balanced substitutions keep exact pairing and their
        real redirects keep denying."""
        for cmd in (
            'echo "$(echo "> /tmp_probe/x")"',
            # the space keeps the exposed target un-glued from the next
            # placeholder — the target-grammar backstop alone missed this
            'echo "$(echo "> /tmp_probe/x ")"',
            'echo "${X:-"> /tmp_probe/x "}"',
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert allow, (cmd, reason)
        p = payload("Bash", command='echo "$(hostname)" > /tmp_probe/x')
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_negated_cd_voids_tracking_never_goes_stale(self, tmp_path):
        """`! cd server` runs cd in the CURRENT shell (status negation) —
        rejecting the `!` prefix kept the stale pre-cd cwd and falsely
        denied the in-root scratch write. Untrackable (fail open), not
        stale."""
        (tmp_path / "server").mkdir()
        cmd = f"! cd {tmp_path}/server; cat > ../.omater/scratch/x"
        p = payload("Bash", command=cmd)
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_unresolved_target_constructs_fail_open_by_grammar(self, tmp_path):
        """THE conservative target grammar (ratified fence contract:
        seatbelt, not a security boundary): any construct the resolver has
        not FULLY resolved classifies the write as UNRECOGNIZED -> fail
        open. One grammar, one rule per construct CLASS — never new
        per-construct bash semantics."""
        for cmd in (
            'echo x > "$OUT"/f',  # quoted span (placeholder)
            "echo x > $OUT/f",  # parameter expansion
            "echo x > $(dirname a)/f",  # command substitution
            "echo x > `dirname a`/f",  # backtick substitution
            "echo x > f\\ g.txt",  # escape
            "echo x > proj?/f",  # glob
            "echo x > proj[12]/f",  # glob class
            "echo x > {a,b}/f",  # brace expansion
            "echo x > ~-/f",  # directory-stack tilde
            "echo x > ~nosuchuser8_/f",  # unresolvable user tilde
        ):
            resolutions = hooks.resolved_bash_targets(cmd, tmp_path)
            assert resolutions, cmd  # the shape IS seen — and fails open
            for _, resolved in resolutions:
                assert resolved is None, (cmd, resolved)

    def test_invalid_builtin_flags_never_apply_the_target(self, tmp_path):
        """bash rejects `pushd -P /etc` and `cd -Z /etc` (invalid option)
        WITHOUT moving — applying the target tracked a cwd the shell never
        entered and falsely denied the in-root write. Valid flags still
        track (and deny)."""
        for cmd in (
            "pushd -P /etc; cat > out.txt",
            "cd -Z /etc; cat > out.txt",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert allow, (cmd, reason)
        p = payload("Bash", command="cd -L /etc; cat > passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_comment_after_line_continuation_is_a_comment(self, tmp_path):
        """bash removes `\\<newline>` BEFORE recognizing comments:
        `echo ok \\<nl># ignored > /tmp_probe/x` joins to a comment whose
        redirect never runs (scanning it falsely denied), while
        `echo x\\<nl>#y` joins into the WORD `x#y` (not a comment — its
        real redirect must stay recognized)."""
        p = payload("Bash", command="echo ok \\\n# ignored > /tmp_probe/x")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason
        p = payload("Bash", command="cd /etc; echo x\\\n#y > passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_directory_stack_tilde_write_targets_fail_open(self, tmp_path):
        """`cd /etc && echo x > ~-/out.txt` writes under $OLDPWD (in-root
        here) — resolving the literal `~-` against /etc falsely denied it.
        Same rule as cd targets: a tilde surviving expanduser is runtime
        state, fail open. A plain `~/` target still resolves (and
        denies)."""
        p = payload("Bash", command="cd /etc && echo x > ~-/out.txt")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason
        [(_, resolved)] = hooks.resolved_bash_targets(
            "cd /etc && echo x > ~-/out.txt", tmp_path
        )
        assert resolved is None
        p = payload("Bash", command="echo x > ~/omater-tilde-probe.txt")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_cd_as_an_argument_does_not_void_tracking(self, tmp_path):
        """`echo cd` is an argument, not a command — the fallback voiding
        a known /etc there hid the recognized write behind the fail-open
        path. Current-shell wrappers (`command cd`) still void."""
        p = payload("Bash", command="cd /etc; echo cd; cat > passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow
        # `command cd` DOES move the current shell's cwd: still unmodeled,
        # still voids (fail open)
        p = payload("Bash", command=f"cd /etc; command cd {tmp_path}; cat > passwd")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_assignment_only_segment_keeps_the_tracked_cwd(self, tmp_path):
        """`HOME=$HOME` runs no command word — the cwd genuinely stays
        put, and reading it as an unnameable command hid the recognized
        /etc write. `HOME=x $CMD` still voids: $CMD can expand to `cd`,
        which runs in the CURRENT shell."""
        p = payload("Bash", command="cd /etc; HOME=$HOME; cat > passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow
        p = payload("Bash", command="cd /etc; HOME=x $CMD; cat > passwd")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_bitwise_and_in_arithmetic_is_not_backgrounding(self, tmp_path):
        """`$((x & 1))` is a bitwise AND — reading it as a background `&`
        voided the tracked /etc and hid the recognized write."""
        p = payload("Bash", command="cd /etc; echo $((x & 1)); cat > passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_pipeline_cd_keeps_the_parent_cwd(self, tmp_path):
        """A cd in a pipeline (either side) runs in a subshell that moves
        NOTHING: the parent's tracked cwd stays valid — voiding it turned
        the known /etc into unknown and hid the recognized write."""
        for cmd in (
            "cd /etc; true | cd /tmp; cat > passwd",
            "cd /etc; cd /tmp | true; cat > passwd",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert not allow, cmd

    def test_set_physical_makes_flagless_cd_mode_unknowable(self, tmp_path):
        """`set -P` flips every later flagless cd to physical `..`
        resolution: through an in-root symlink to an outside dir,
        `cd link && cd ..` physically lands OUTSIDE while logical stepping
        lands back in-root — tracking the logical answer misresolved the
        write. After a physical-option toggle the mode is unknowable:
        relative hops fail open. P-irrelevant `set` flags must not
        disturb tracking."""
        outside = tmp_path.parent / (tmp_path.name + "_target")
        outside.mkdir()
        (tmp_path / "link").symlink_to(outside)
        cmd = "set -P; cd link && cd .. && cat > out.txt"
        [(_, resolved)] = hooks.resolved_bash_targets(cmd, tmp_path)
        assert resolved is None
        p = payload("Bash", command="set -x; cd /etc; cat > passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_arith_scan_is_linear_in_command_size(self, tmp_path):
        """The fence runs synchronously in PreToolUse — rescanning the
        data-span list per character made the arithmetic scan quadratic
        (spans x chars), so a generated heredoc full of quoted lines
        stalled the hook for tens of seconds."""
        import time

        body = "\n".join(f'echo "line {i}" >> /tmp_probe/x' for i in range(2000))
        cmd = f"cat <<EOF > out.txt\n{body}\nEOF"
        p = payload("Bash", command=cmd)
        p["cwd"] = str(tmp_path)
        started = time.monotonic()
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert time.monotonic() - started < 2.0
        assert allow, reason  # the body is heredoc DATA, never executed

    def test_escaped_separator_in_cd_segment_never_falsely_denies(self, tmp_path):
        """`cd /etc \\; cat > out.txt` is ONE command: bash opens the
        redirect against the PRE-cd cwd (in-root) and then rejects the
        multi-arg cd ("too many arguments") without moving. A boundary at
        the escaped `;` applied /etc and denied ./out.txt as /etc/out.txt
        — and the never-run cd must not poison later segments either."""
        p = payload("Bash", command="cd /etc \\; cat > out.txt; cat > two.txt")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason
        # a REAL separator there still tracks the cd and denies
        p = payload("Bash", command="cd /etc ; cat > out.txt")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_ansi_c_quoting_allows_escaped_quotes(self, tmp_path):
        """$'text \\' more' is ONE argument (ANSI-C quoting) — ending the
        span at the escaped quote exposed its text as a redirect target."""
        cmd = "printf '%s' $'text \\' > /tmp_probe/data' > out.txt"
        p = payload("Bash", command=cmd)
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_unsupported_heredoc_shapes_are_a_wall(self, tmp_path):
        """Delimiters beyond the supported grammar (END@MARK, <<\\EOF) leave
        their bodies scannable — but body text is DATA: neither its cds nor
        its redirects (absolute included) execute. Everything after the
        introducer line fails open."""
        for cmd in (
            "cat <<END@MARK\ncd /etc\nEND@MARK\ncat > out.txt",
            "cat <<\\EOF\ncd /etc\nEOF\ncat > out.txt",
            "cat <<END@MARK\necho > /tmp_probe/not-executed\nEND@MARK",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert allow, (cmd, reason)

    def test_line_continuation_joins_token_fragments(self, tmp_path):
        """`c\\<newline>d <dir>` is the word `cd` — spacing the fragments
        apart left the tracked cwd stale and falsely denied the write."""
        (tmp_path / "server").mkdir()
        cmd = f"cd /etc; c\\\nd {tmp_path}/server && cat > ../.omater/scratch/x"
        p = payload("Bash", command=cmd)
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_indented_terminator_does_not_end_a_heredoc(self, tmp_path):
        """Bash requires an exact column-zero terminator for << — an
        indented ` EOF` body line must not end the heredoc and expose the
        rest of the body (its absolute redirect never executes)."""
        cmd = "cat <<EOF\n EOF\ncat > /tmp_probe/not-executed\nEOF\necho done"
        p = payload("Bash", command=cmd)
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_backtick_escaping_is_parity_based(self, tmp_path):
        """`\\\\\\`` after an even backslash run OPENS a substitution — the
        single-char check missed it, and a backtick inside a later real
        comment then ended the phantom span early, exposing a redirect bash
        ignores."""
        cmd = "echo \\\\`true` # tail with ` and > /tmp_probe/x"
        p = payload("Bash", command=cmd)
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_escaped_paren_is_word_data_not_group_close(self, tmp_path):
        """`echo \\)#suffix > /tmp_probe/out`: the escaped paren keeps
        #suffix in the word, so the redirect EXECUTES — classifying it as a
        group close comment-stripped the redirect away (a miss)."""
        p = payload("Bash", command="echo \\)#suffix > /tmp_probe/out")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_quoted_heredoc_delimiter_with_spaces(self, tmp_path):
        """`<<'END MARK'` is a valid heredoc — its body (containing a cd) is
        data, and scanning it falsely denied the later in-root write."""
        cmd = "cat <<'END MARK'\ncd /etc\nEND MARK\ncat > out.txt"
        p = payload("Bash", command=cmd)
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_even_backslash_run_before_quote_still_opens_the_span(self, tmp_path):
        """Escapes are parity-based: `\\\\\"` is an escaped backslash + a REAL
        quote — the span must mask, or a genuinely quoted `>` gets falsely
        denied."""
        p = payload("Bash", command='echo \\\\"quoted > /tmp_probe/out"')
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_heredoc_delimiters_beyond_word_chars(self, tmp_path):
        """`<<END-MARK` is a valid heredoc — an unrecognized delimiter left
        the body scannable and its cd falsely denied the later write."""
        cmd = "cat <<END-MARK\ncd /etc\nEND-MARK\ncat > out.txt"
        p = payload("Bash", command=cmd)
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_runtime_tilde_and_brace_cd_targets_are_untrackable(self, tmp_path):
        """`cd ~-` returns to $OLDPWD (runtime state), `~nobody`-style names
        stay literal to expanduser, and one-value brace expansions resolve
        away from their literal braces — all unknown, fail open."""
        for cmd in (
            "cd /etc; cd ~-; cat > out.txt",
            "cd ~no_such_user_xyz/dir && cat > out.txt",
            "cd {a,b}/dir && cat > out.txt",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert allow, (cmd, reason)

    def test_pipe_ampersand_is_a_pipeline_not_background(self, tmp_path):
        """`|&` pipes stderr. Its `&` must not read as backgrounding (that
        spuriously cleared a tracked /etc and missed the later write), and a
        cd on its right side runs in the pipeline subshell (applying it
        falsely denied an in-root write)."""
        p = payload("Bash", command="cd /etc; echo x |& cat; cat > passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow
        p = payload("Bash", command="true |& cd /etc; cat > out.txt")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_backslash_and_expansion_command_words_void_tracking(self, tmp_path):
        """Bash removes backslashes and expands parameters in command words:
        `c\\d server` RUNS cd — missing it kept a stale cwd that falsely
        denied the in-root scratch write. `$CMD ...` is equally unnameable."""
        (tmp_path / "server").mkdir()
        for cmd in (
            f"c\\d {tmp_path}/server && cat > ../.omater/scratch/x",
            f"$GOTO {tmp_path}/server && cat > ../.omater/scratch/x",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert allow, (cmd, reason)

    def test_glob_cd_targets_are_untrackable(self, tmp_path):
        """`cd ../proj*` expands at runtime and can land right back in-root
        — tracking the literal nonexistent `../proj*` falsely denied the
        write."""
        p = payload("Bash", command="cd ../proj* && cat > out.txt")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_escaped_double_quote_does_not_end_the_quoted_span(self, tmp_path):
        """`echo "x\\" # still quoted" > /tmp_probe/out`: the \\" stays
        inside the argument, so the absolute redirect EXECUTES — ending the
        mask at the escape turned the rest into a comment and hid it."""
        p = payload("Bash", command='echo "x\\" # still quoted" > /tmp_probe/out')
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_escaped_space_before_hash_is_not_a_comment_boundary(self, tmp_path):
        """`echo foo\\ #bar > /tmp_probe/out`: the escaped space keeps #bar
        inside the word, so the absolute redirect EXECUTES — blanking from
        the # hid a recognizable out-of-tree write."""
        p = payload("Bash", command="echo foo\\ #bar > /tmp_probe/out")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow
        # an unescaped space before # is still a comment
        p = payload("Bash", command="echo foo #bar > /tmp_probe/out")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_non_literal_targets_fail_open_even_under_a_tracked_cwd(self, tmp_path):
        """A quoted or expansion-bearing target is not a literal filename:
        resolving the placeholder against a tracked /etc cwd falsely denied
        `cd /etc && echo x > "<root>/out.txt"` (an in-root write)."""
        for cmd in (
            f'cd /etc && echo x > "{tmp_path}/out.txt"',
            "cd /etc && echo x > $PROJECT_ROOT/out.txt",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert allow, (cmd, reason)
        # literal targets under the tracked cwd keep full deny power
        p = payload("Bash", command="cd /etc && echo x > out.txt")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_dash_p_realpaths_before_lexical_normalization(self, tmp_path):
        """`cd link && cd -P ..` lands in the link TARGET's parent —
        normalizing `..` away first erased the symlink hop and let the
        out-of-tree write pass."""
        outside = tmp_path / "outside-tree"
        (outside / "child").mkdir(parents=True)
        root = tmp_path / "project"
        root.mkdir()
        (root / "link").symlink_to(outside / "child")
        p = payload("Bash", command="cd link && cd -P .. && cat > out.txt")
        p["cwd"] = str(root)
        allow, _ = hooks.evaluate_pre_tool_use(p, root)
        assert not allow

    def test_popd_n_does_not_move_the_cwd(self, tmp_path):
        """popd -n edits the stack only — discarding a known cwd there made
        the fence miss a still-in-/etc relative write."""
        p = payload("Bash", command="cd /etc; popd -n; cat > passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_assignment_prefixed_source_voids_tracking(self, tmp_path):
        """`MODE=x source env.sh` is valid bash and the sourced code can cd
        anywhere — missing the prefix form kept a stale cwd that falsely
        denied a legitimate relative write."""
        (tmp_path / "server").mkdir()
        cmd = (
            f"cd {tmp_path}/server && MODE=x source setup.sh && "
            "cat > ../../might-be-fine.txt"
        )
        p = payload("Bash", command=cmd)
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_command_opacity_lands_after_its_own_redirects(self, tmp_path):
        """Redirects attached to a command open BEFORE it runs: after
        `cd /etc`, `source env.sh > audit.log` writes /etc/audit.log — the
        source's opacity must not clear tracking before that target
        resolves (and denies). Same for an unmatched-cd command's redirect
        resolving against the pre-command cwd."""
        p = payload("Bash", command="cd /etc && source env.sh > audit.log")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow
        p = payload("Bash", command="command cd /etc > ../escape.txt")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow  # ../escape.txt opens against the PRE-command cwd

    def test_comments_inside_backtick_substitutions_are_blanked(self, tmp_path):
        """A comment inside a substitution is a real comment — its text
        (`# > /tmp_probe/never`) must not stay scannable as a write."""
        cmd = "echo `printf x # > /tmp_probe/never\n` > out.txt"
        p = payload("Bash", command=cmd)
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_prefix_whitespace_does_not_swallow_newlines(self, tmp_path):
        """`false && MODE=x\\ncd /etc; cat > passwd`: two separate commands —
        the unconditional cd DOES run, so the /etc/passwd write must stay a
        recognized deny (merging the lines guarded the wrong cd and let it
        pass). And `cd -P\\ncat > out` must not parse `cat` as the directory."""
        p = payload("Bash", command="false && MODE=x\ncd /etc; cat > passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow
        p = payload("Bash", command="cd -P\ncat > out.txt")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason  # leftover -P target -> unknown, fail open

    def test_expansion_braces_are_not_compound_bodies(self, tmp_path):
        """`${HOME}` and `file{1,2}` are expansions — treating their braces
        as hard opacity killed tracking and let a real /etc write pass."""
        for cmd in (
            "echo ${HOME}; cd /etc && cat > passwd",
            "echo file{1,2}; cd /etc && cat > passwd",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert not allow, cmd

    def test_line_continuation_is_not_a_boundary(self, tmp_path):
        """`false && \\<newline> cd /etc; cat > out.txt` is ONE guarded list
        — the cd never runs; parsing the newline as a boundary made it an
        unconditional cd and falsely denied out.txt."""
        p = payload("Bash", command="false && \\\n cd /etc; cat > out.txt")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason
        # joined lists keep deny power: the continuation glues one && list
        p = payload("Bash", command="cd /etc && \\\n cat > passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_cdpath_searches_dot_hidden_names_too(self, tmp_path):
        """Bash bypasses CDPATH only for ./.. and ./-anchored paths —
        `.hidden` IS CDPATH-searched and can land anywhere on the search
        path, so it must be untrackable when a CDPATH is active."""
        (tmp_path / ".hidden").mkdir()
        env = {"CDPATH": "/tmp_probe"}
        p = payload("Bash", command="cd .hidden && cat > ../../evil.txt")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path, env=env)
        assert allow, reason  # untrackable -> fail open, never guess-deny

    def test_quote_spliced_cd_voids_tracking(self, tmp_path):
        """Bash concatenates quoted spans: `c""d server` and `"cd" server`
        both RUN cd, invisibly to the scanner — a stale cwd then falsely
        denied the in-root scratch write. An unnameable command-position
        word voids tracking instead."""
        (tmp_path / "server").mkdir()
        for cmd in (
            'c""d server && cat > ../.omater/scratch/x',
            '"cd" server && cat > out.txt',
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert allow, (cmd, reason)

    def test_no_cd_recovery_inside_compound_bodies(self, tmp_path):
        """`if false; then cd /etc; cat > shadow; fi` executes neither the
        cd nor the write — an absolute cd inside an unmodeled compound body
        must not recover tracking and falsely deny the redirect."""
        p = payload("Bash", command="if false; then cd /etc; cat > shadow; fi")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_quote_placeholder_keeps_word_adjacency(self, tmp_path):
        """`echo "x"#suffix > /tmp_probe/out`: bash executes the redirect —
        the space-padded placeholder invented a word boundary that turned
        #suffix into a comment and ERASED the recognizable absolute write."""
        p = payload("Bash", command='echo "x"#suffix > /tmp_probe/out')
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_comment_after_a_grouping_paren_is_a_comment(self, tmp_path):
        """`(echo ok)# ignored > /tmp_probe/x`: after a GROUP-closing `)` the
        `#` starts a comment — bash never executes that redirect, so keeping
        it scannable falsely denied the command."""
        p = payload("Bash", command="(echo ok)# ignored > /tmp_probe/x")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_hash_after_command_substitution_is_not_a_comment(self, tmp_path):
        """`$(printf x)#suffix` continues the word — a `)` ends a
        substitution whose result can be word-glued, so it must not count
        as a comment boundary (that erased the absolute write after it)."""
        p = payload("Bash", command="echo $(printf x)#suffix > /tmp_probe/out")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_or_after_an_applied_cd_voids_the_tracked_cwd(self, tmp_path):
        """`cd /definitely-missing && true || cat > out.txt`: the || branch
        runs because the cd FAILED, so cat writes in the original cwd — the
        success-assumed /definitely-missing must not deny it."""
        p = payload(
            "Bash", command="cd /definitely-missing && true || cat > out.txt"
        )
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason
        # a || in a LATER list says nothing about an earlier list's cd:
        # deny power is kept across the `;`
        p = payload("Bash", command="cd /etc; true || false; cat > passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_eval_source_and_dot_void_tracking(self, tmp_path):
        """eval/source/. execute current-shell code the scanner cannot see
        (`eval 'cd <root>/server'` really moves the cwd) — after them the
        cwd is unknowable, so relative targets fail open. An absolute cd
        afterwards recovers tracking (and denying)."""
        (tmp_path / "server").mkdir()
        for cmd in (
            f"eval 'cd {tmp_path}/server' && cat > ../.omater/scratch/x",
            "source env.sh && cat > out.txt",
            ". ./env.sh && cat > out.txt",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert allow, (cmd, reason)
        p = payload("Bash", command="eval 'x'; cd /etc && cat > passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_cd_is_logical_by_default(self, tmp_path):
        """Bash cds logically (-L): `cd link && cd ..` returns to the LINK's
        parent, not the symlink target's parent. Realpathing every hop
        falsely denied the in-root write; -P opts into physical semantics."""
        outside = tmp_path / "outside-tree"
        (outside / "child").mkdir(parents=True)
        root = tmp_path / "project"
        root.mkdir()
        (root / "link").symlink_to(outside / "child")
        p = payload("Bash", command="cd link && cd .. && cat > out.txt")
        p["cwd"] = str(root)
        allow, reason = hooks.evaluate_pre_tool_use(p, root)
        assert allow, reason  # logical: back in <root>, write is in-tree
        # writes THROUGH the link still canonicalize at resolution: denied
        p = payload("Bash", command="cd link && cat > x")
        p["cwd"] = str(root)
        allow, _ = hooks.evaluate_pre_tool_use(p, root)
        assert not allow
        # -P selects physical semantics: `cd ..` lands in the OUTSIDE parent
        p = payload("Bash", command="cd -P link && cd .. && cat > out.txt")
        p["cwd"] = str(root)
        allow, _ = hooks.evaluate_pre_tool_use(p, root)
        assert not allow

    def test_cd_inside_compound_control_flow_fails_open(self, tmp_path):
        """A cd inside `if false; then … fi`, a function body, or a
        zero-iteration loop may never execute — applying it guess-denied a
        write that lands in the original cwd. Compound keywords make the
        tracked cwd opaque; absolute targets still enforce."""
        for cmd in (
            "if false; then\n cd /etc\nfi\ncat > out.txt",
            "while false; do cd /etc; done; cat > out.txt",
            "deploy() { cd /etc; }; cat > out.txt",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert allow, (cmd, reason)
        p = payload("Bash", command="if true; then cat > /tmp_probe/x; fi")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_guarded_cd_applies_within_its_list_and_voids_after_it(self, tmp_path):
        """`A && cd /x` is conditional: within the same && list every later
        member ran only if the cd succeeded (apply), but past the `;` the
        guard's outcome is unknowable — `false && cd /etc; cat > out.txt`
        writes in the ORIGINAL cwd and was falsely denied."""
        p = payload("Bash", command="false && cd /etc; cat > out.txt")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason
        # within the guarded list the cd is sound and still denies
        p = payload("Bash", command="true && cd /etc && cat > shadow")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_non_literal_cd_targets_fail_open(self, tmp_path):
        """Backslash-escaped whitespace truncates the scanned token (`cd
        a\\ b/c` reads as `a\\`), and pushd +N/-N rotates the directory
        stack — neither names a knowable path, so relative targets after
        them must fail open, never resolve against a wrong guess."""
        for cmd in (
            "cd a\\ b/c && cat > ../../.omater/scratch/x",
            "pushd +1 && cat > out.txt",
            "pushd -2 && cat > out.txt",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert allow, (cmd, reason)

    def test_fd_duplication_ampersand_is_not_a_control_operator(self, tmp_path):
        """`cd /etc >/dev/null 2>&1 && cat > passwd`: the `&` in `2>&1` is
        fd duplication, not backgrounding — the cd still applies at the &&
        and the /etc write is a recognized deny. Same for the `&>` shorthand."""
        for cmd in (
            "cd /etc >/dev/null 2>&1 && cat > passwd",
            "cd /etc &> cd.log && cat > passwd",
        ):
            p = payload("Bash", command=cmd)
            p["cwd"] = str(tmp_path)
            allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
            assert not allow, cmd

    def test_bare_ampersand_backgrounds_the_whole_list(self, tmp_path):
        """`cd /etc && true & cat > out.txt`: the trailing `&` backgrounds
        the ENTIRE `cd && true` list in a subshell, so cat writes under the
        original cwd — was falsely denied as /etc/out.txt."""
        p = payload("Bash", command="cd /etc && true & cat > out.txt")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_double_dash_option_terminator_is_not_the_directory(self, tmp_path):
        """`cd -- /etc && cat > passwd`: parsing `--` as the target pinned
        the cwd to <root>/-- and let the /etc/passwd write through."""
        p = payload("Bash", command="cd -- /etc && cat > passwd")
        p["cwd"] = str(tmp_path)
        allow, _ = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert not allow

    def test_cd_mentioned_in_prose_does_not_move_the_cwd(self, tmp_path):
        """Only cd at a command position counts: `echo cd /etc` is prose."""
        p = payload("Bash", command="echo cd /etc; cat > out.txt")
        p["cwd"] = str(tmp_path)
        allow, reason = hooks.evaluate_pre_tool_use(p, tmp_path)
        assert allow, reason

    def test_writes_inside_quoted_interpreter_code_pass_by_design(self, tmp_path):
        """CHARACTERIZATION, not a bug (report rough edge #7, measured in the
        sandbox proof): the fence is a redirector for tool-shaped writes, not
        a jail. A write constructed inside quoted interpreter code is data to
        the bash scan — quoted strings must stay data or every commit message
        mentioning a path would stall a phase. The measured backstop is the
        CLI's permission_denials capture plus verifier discipline. If this
        test ever FAILS, the fence has started parsing quoted code: check the
        false-DENY cost before celebrating."""
        for cmd in (
            "python3 -c \"open('/private/tmp/omlog-poke.txt', 'w').write('x')\"",
            'python3 -c "import tempfile; tempfile.NamedTemporaryFile(delete=False)"',
        ):
            allow, reason = hooks.evaluate_pre_tool_use(
                payload("Bash", command=cmd), tmp_path
            )
            assert allow, (cmd, reason)


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

    def test_init_gitignores_pytest_cache_too(self, tmp_path):
        """Report rough edge #9: command_ok pytest gauntlets leave a
        .pytest_cache/ at the consumer repo's root — ignore it up front. It
        is a convenience, not drift: verify must not fail on its absence in
        repos initialized before this line existed."""
        run_init(tmp_path)
        lines = (tmp_path / ".gitignore").read_text().splitlines()
        assert ".pytest_cache/" in lines
        # idempotent alongside the required line
        run_init(tmp_path)
        lines = (tmp_path / ".gitignore").read_text().splitlines()
        assert lines.count(".pytest_cache/") == 1
        # pre-existing repos without it stay verify-clean
        (tmp_path / ".gitignore").write_text(GITIGNORE_LINE + "\n", encoding="utf-8")
        assert not any(".pytest_cache" in p for p in run_verify(tmp_path))

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
