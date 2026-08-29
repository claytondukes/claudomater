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
