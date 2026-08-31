"""Config loaders: .omater.yaml + ~/.omater/config.yaml."""

from __future__ import annotations

import pytest

from claudomater.config import (
    ConfigError,
    DEPLOYMENT_POLICY,
    MODEL_FABLE,
    MODEL_OPUS,
    MODEL_SONNET,
    PROJECT_CONFIG_NAME,
    SKIP,
    UserConfig,
    load_project_config,
    load_user_config,
)


def write_project(tmp_path, text):
    (tmp_path / PROJECT_CONFIG_NAME).write_text(text, encoding="utf-8")
    return tmp_path


class TestProjectConfig:
    def test_minimal_config_gets_deployment_defaults(self, tmp_path):
        cfg = load_project_config(write_project(tmp_path, "project: demo\n"))
        assert cfg.project == "demo"
        assert cfg.deployment_type == "sandbox"
        assert cfg.model_for("dev") == MODEL_SONNET
        assert cfg.model_for("sr_review") == SKIP
        assert cfg.model_for("escalation") == MODEL_FABLE

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load_project_config(tmp_path)

    def test_unreadable_file_is_a_config_error(self, tmp_path):
        cfg = tmp_path / PROJECT_CONFIG_NAME
        cfg.write_text("project: x\n", encoding="utf-8")
        cfg.chmod(0o000)
        try:
            with pytest.raises(ConfigError, match="cannot read"):
                load_project_config(tmp_path)
        finally:
            cfg.chmod(0o644)

    def test_missing_project_name_raises(self, tmp_path):
        with pytest.raises(ConfigError, match="'project'"):
            load_project_config(write_project(tmp_path, "deployment_type: sandbox\n"))

    def test_invalid_deployment_type_raises(self, tmp_path):
        with pytest.raises(ConfigError, match="deployment_type"):
            load_project_config(
                write_project(tmp_path, "project: x\ndeployment_type: prod\n")
            )

    def test_invalid_forge_raises(self, tmp_path):
        with pytest.raises(ConfigError, match="forge"):
            load_project_config(write_project(tmp_path, "project: x\nforge: gitlab\n"))

    def test_model_override_beats_default(self, tmp_path):
        cfg = load_project_config(
            write_project(
                tmp_path,
                "project: x\ndeployment_type: mission-critical\n"
                f"models:\n  dev: {MODEL_FABLE}\n",
            )
        )
        assert cfg.model_for("dev") == MODEL_FABLE  # override
        assert cfg.model_for("sr_review") == MODEL_FABLE  # default

    def test_unknown_model_role_raises(self, tmp_path):
        with pytest.raises(ConfigError, match="unknown model role"):
            load_project_config(
                write_project(tmp_path, "project: x\nmodels:\n  reviewer: m\n")
            )

    def test_non_string_model_value_raises(self, tmp_path):
        with pytest.raises(ConfigError, match="model name string"):
            load_project_config(
                write_project(tmp_path, "project: x\nmodels:\n  dev: [a, b]\n")
            )

    def test_copilot_reviewer_requires_github(self, tmp_path):
        with pytest.raises(ConfigError, match="GitHub-only"):
            load_project_config(
                write_project(
                    tmp_path,
                    "project: x\nforge: bitbucket\nmerge:\n  reviewer: copilot\n",
                )
            )

    def test_copilot_reviewer_inert_when_converge_off(self, tmp_path):
        """converge: off skips the gate entirely — the reviewer value is
        inert, so a non-GitHub project may keep the template default."""
        cfg = load_project_config(
            write_project(
                tmp_path,
                "project: x\nforge: bitbucket\nmerge:\n  converge: off\n  reviewer: copilot\n",
            )
        )
        assert cfg.merge.converge == "off"

    def test_bitbucket_with_agent_reviewer_ok(self, tmp_path):
        cfg = load_project_config(
            write_project(
                tmp_path,
                "project: x\nforge: bitbucket\nmerge:\n  reviewer: agent\n",
            )
        )
        assert cfg.merge.reviewer == "agent"

    def test_invalid_converge_raises(self, tmp_path):
        with pytest.raises(ConfigError, match="converge"):
            load_project_config(
                write_project(tmp_path, "project: x\nmerge:\n  converge: maybe\n")
            )

    def test_policy_snapshot_records_the_resolved_round_alarm(self, tmp_path):
        """Round-3 finding: the policy event start_run logs must establish
        which review-round limit governed a run — a later config edit has to
        be distinguishable from the original setting. Defaulted and custom
        values both resolve into the snapshot; garbage fails at LOAD like
        every other knob."""
        from claudomater.merge import DEFAULT_REVIEW_ROUND_ALARM

        default = load_project_config(write_project(tmp_path, "project: x\n"))
        assert default.policy()["gates"]["review_round_alarm"] == (
            DEFAULT_REVIEW_ROUND_ALARM
        )
        custom = load_project_config(
            write_project(
                tmp_path, "project: x\ngates:\n  review_round_alarm: 3\n"
            )
        )
        assert custom.policy()["gates"]["review_round_alarm"] == 3
        with pytest.raises(ConfigError, match="review_round_alarm"):
            load_project_config(
                write_project(
                    tmp_path, "project: x\ngates:\n  review_round_alarm: soon\n"
                )
            )

    def test_non_scalar_gate_values_fail_at_load_and_policy_serializes(
        self, tmp_path
    ):
        """Round-4 finding: gates ride verbatim into policy(), which the run
        log json.dumps'es — and yaml.safe_load happily produces a date for
        `board_steps_required: 2026-08-30`, crashing start_run AFTER the run
        directory exists. Non-scalar gate values are ConfigErrors at load,
        and a loaded policy must always serialize."""
        import json as _json

        with pytest.raises(ConfigError, match="gates.board_steps_required"):
            load_project_config(
                write_project(
                    tmp_path,
                    "project: x\ngates:\n  board_steps_required: 2026-08-30\n",
                )
            )
        # YAML's .nan is a float and would sail through a bare scalar check,
        # but json.dumps emits it as NaN — invalid JSON for strict JSONL
        # readers of the run log (round-5 finding).
        with pytest.raises(ConfigError, match="must be finite"):
            load_project_config(
                write_project(tmp_path, "project: x\ngates:\n  weight: .nan\n")
            )
        cfg = load_project_config(
            write_project(
                tmp_path, "project: x\ngates:\n  board_steps_required: false\n"
            )
        )
        _json.dumps(cfg.policy())  # the run-log snapshot must serialize

    def test_policy_changes_visibly_with_deployment_type(self, tmp_path):
        """AC: changing deployment_type visibly changes model chain, review
        floor, and CI tier (this dict is what run start logs)."""
        sandbox = load_project_config(
            write_project(tmp_path, "project: x\ndeployment_type: sandbox\n")
        ).policy()
        mc = load_project_config(
            write_project(tmp_path, "project: x\ndeployment_type: mission-critical\n")
        ).policy()
        assert sandbox["models"] != mc["models"]
        assert (sandbox["review_floor"], mc["review_floor"]) == ("CRITICAL", "NOTE")
        assert (sandbox["ci_on_push"], mc["ci_on_push"]) == ("lint+type", "fast+smoke")
        assert mc["models"]["dev"] == MODEL_OPUS
        assert mc["models"]["sr_review"] == MODEL_FABLE

    def test_all_deployment_types_have_full_policy(self):
        for dt, policy in DEPLOYMENT_POLICY.items():
            for key in ("models", "review_floor", "red_green", "ci_on_push"):
                assert key in policy, f"{dt} missing {key}"

    def test_lessons_close_pass_has_a_model_role(self, tmp_path):
        """Phase 0.5 rough edge #3: the close pass had no knob, so the
        rehearsal driver had to borrow `orchestrator`. Every deployment
        type resolves `lessons` (SKIP at sandbox — run_phase turns a skip
        model into phase-skipped, so drivers call the phase unconditionally),
        and it is overridable like any other role."""
        from claudomater.config import MODEL_ROLES

        assert "lessons" in MODEL_ROLES
        expectations = {
            "sandbox": SKIP,
            "internal": MODEL_OPUS,
            "production": MODEL_OPUS,
            "mission-critical": MODEL_FABLE,
        }
        for dtype, expected in expectations.items():
            cfg = load_project_config(
                write_project(tmp_path, f"project: x\ndeployment_type: {dtype}\n")
            )
            assert cfg.model_for("lessons") == expected, dtype
            assert cfg.policy()["models"]["lessons"] == expected, dtype
        override = load_project_config(
            write_project(
                tmp_path,
                "project: x\ndeployment_type: internal\n"
                "models:\n  lessons: claude-sonnet-5\n",
            )
        )
        assert override.model_for("lessons") == "claude-sonnet-5"

    def test_non_mapping_sections_are_config_errors(self, tmp_path):
        """merge: off (a string) must be a ConfigError at load, never an
        AttributeError traceback — fail loudly at load is the contract."""
        for text in (
            "project: x\nmerge: off\n",
            "project: x\nci: [fast]\n",
            "project: x\nadapters: none\n",
            "project: x\nlearning: 3\n",
            "project: x\ngates: [a]\n",
        ):
            (tmp_path / PROJECT_CONFIG_NAME).write_text(text, encoding="utf-8")
            with pytest.raises(ConfigError, match="must be a mapping"):
                load_project_config(tmp_path)

    def test_scalar_scopes_is_a_config_error_not_a_char_list(self, tmp_path):
        """`scopes: global` must not silently become ['g','l','o','b','a','l']."""
        with pytest.raises(ConfigError, match="learning.scopes"):
            load_project_config(
                write_project(tmp_path, "project: x\nlearning:\n  scopes: global\n")
            )

    def test_secrets_deny_and_scopes(self, tmp_path):
        cfg = load_project_config(
            write_project(
                tmp_path,
                "project: x\nsecrets_deny: [MY_TOKEN]\nlearning:\n  scopes: [global, python]\n",
            )
        )
        assert cfg.secrets_deny == ["MY_TOKEN"]
        assert cfg.learning_scopes == ["global", "python"]


class TestUserConfig:
    def test_missing_file_yields_spec_defaults(self, tmp_path):
        cfg = load_user_config(tmp_path / "nope.yaml")
        assert cfg.usage.pause_at == {"five_hour": 95, "seven_day": 95}
        assert cfg.usage.on_threshold == {"five_hour": "pause", "seven_day": "pause"}
        assert cfg.usage.degrade_scoped_at == 80
        assert cfg.usage.degrade_path == [MODEL_OPUS, "pause"]
        assert cfg.slack_webhook is None
        assert not cfg.notify_enabled

    def test_full_config_parses(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_WEBHOOK", "https://hooks.example/abc")
        path = tmp_path / "config.yaml"
        path.write_text(
            "usage:\n"
            "  pause_at: { five_hour: 90, seven_day: 85 }\n"
            "  on_threshold: { five_hour: pause, seven_day: degrade }\n"
            "  degrade_scoped_at: 75\n"
            "  degrade_path: [claude-opus-5, claude-sonnet-5]\n"
            "notify:\n"
            "  slack_webhook: ${TEST_WEBHOOK}\n"
            "learning:\n"
            "  db_path: /tmp/l.db\n",
            encoding="utf-8",
        )
        cfg = load_user_config(path)
        assert cfg.usage.pause_at == {"five_hour": 90, "seven_day": 85}
        assert cfg.usage.on_threshold["seven_day"] == "degrade"
        assert cfg.usage.degrade_path == ["claude-opus-5", "claude-sonnet-5"]
        assert cfg.slack_webhook == "https://hooks.example/abc"
        assert cfg.notify_enabled

    def test_unset_env_webhook_disables_notify(self, tmp_path, monkeypatch):
        monkeypatch.delenv("UNSET_WEBHOOK_VAR", raising=False)
        path = tmp_path / "config.yaml"
        path.write_text(
            "notify:\n  slack_webhook: ${UNSET_WEBHOOK_VAR}\n", encoding="utf-8"
        )
        cfg = load_user_config(path)
        assert cfg.slack_webhook is None

    def test_invalid_on_threshold_raises(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "usage:\n  on_threshold: { five_hour: stop }\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="on_threshold"):
            load_user_config(path)

    def test_out_of_range_pause_at_raises(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("usage:\n  pause_at: { five_hour: 120 }\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="pause_at"):
            load_user_config(path)

    def test_boolean_pause_at_raises(self, tmp_path):
        # yaml `true` is an int subclass in Python — must not slip through
        path = tmp_path / "config.yaml"
        path.write_text("usage:\n  pause_at: { five_hour: true }\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="pause_at"):
            load_user_config(path)

    def test_empty_degrade_path_raises(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("usage:\n  degrade_path: []\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="degrade_path"):
            load_user_config(path)

    def test_typoed_degrade_path_model_fails_at_load(self, tmp_path):
        """A nonexistent model must fail loudly at config load, not by a live
        run degrading into it and dying at spawn time."""
        path = tmp_path / "config.yaml"
        path.write_text(
            "usage:\n  degrade_path: [claude-opsu-5, pause]\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="unrecognized model"):
            load_user_config(path)

    def test_pause_must_be_last_in_degrade_path(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "usage:\n  degrade_path: [claude-opus-5, pause, claude-sonnet-5]\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="last entry"):
            load_user_config(path)

    def test_pause_only_degrade_path_rejected(self, tmp_path):
        """[pause] can never degrade anything — next_model finds no lower
        tier and keeps every model unchanged, silently disabling scoped
        degrades. Must fail at load."""
        path = tmp_path / "config.yaml"
        path.write_text("usage:\n  degrade_path: [pause]\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="at least one model"):
            load_user_config(path)

    def test_degrade_path_must_step_down(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "usage:\n  degrade_path: [claude-sonnet-5, claude-opus-5]\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="strictly DOWN"):
            load_user_config(path)

    def test_defaults_object_is_valid(self):
        from claudomater.usage import DEFAULT_MAX_STALE_S

        # one default, defined in usage.py next to the TTL>longest-phase
        # invariant — the config layer must not carry its own copy
        assert UserConfig().usage.max_stale_seconds == DEFAULT_MAX_STALE_S

    def test_non_mapping_sections_are_config_errors(self, tmp_path):
        for text in ("usage: [a, b]\n", "notify: just-a-string\n", "learning: 3\n"):
            path = tmp_path / "config.yaml"
            path.write_text(text, encoding="utf-8")
            with pytest.raises(ConfigError, match="must be a mapping"):
                load_user_config(path)

    def test_non_positive_max_stale_is_a_config_error(self, tmp_path):
        """Round-7 finding: 0/negative marks EVERY cache entry stale, and
        the stale carve-out would then proceed on any low reading — config
        garbage silently weakening the guardrail instead of failing at
        load."""
        for bad in (0, -300):
            path = tmp_path / "config.yaml"
            path.write_text(f"usage:\n  max_stale_seconds: {bad}\n", encoding="utf-8")
            with pytest.raises(ConfigError, match="must be >= 1"):
                load_user_config(path)

    def test_non_integer_knobs_are_config_errors(self, tmp_path):
        for text in (
            "usage:\n  degrade_scoped_at: soon\n",
            "usage:\n  max_stale_seconds: [300]\n",
            "usage:\n  degrade_scoped_at: true\n",
        ):
            path = tmp_path / "config.yaml"
            path.write_text(text, encoding="utf-8")
            with pytest.raises(ConfigError, match="must be an integer"):
                load_user_config(path)

    def test_non_string_learning_paths_are_config_errors(self, tmp_path):
        for text in (
            "learning:\n  db_path: 3\n",
            "learning:\n  export_path: [a, b]\n",
        ):
            path = tmp_path / "config.yaml"
            path.write_text(text, encoding="utf-8")
            with pytest.raises(ConfigError, match="path string"):
                load_user_config(path)

    def test_non_list_degrade_path_is_a_config_error(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("usage:\n  degrade_path: claude-opus-5\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="must be a list"):
            load_user_config(path)


class TestArtifactRootsConfig:
    """PR #14 round 3: `data.get("artifact_roots") or []` swallowed falsy
    non-list values (false, '', 0) as "not set" instead of raising - a
    malformed declaration must fail loudly like every other knob."""

    def test_falsy_non_list_values_are_rejected(self, tmp_path):
        for bad in ("artifact_roots: false\n", "artifact_roots: ''\n",
                    "artifact_roots: 0\n"):
            with pytest.raises(ConfigError, match="artifact_roots"):
                load_project_config(write_project(tmp_path, f"project: p\n{bad}"))

    def test_a_string_is_rejected_not_exploded_into_characters(self, tmp_path):
        with pytest.raises(ConfigError, match="artifact_roots"):
            load_project_config(
                write_project(tmp_path, "project: p\nartifact_roots: _bmad-output\n")
            )

    def test_null_and_absent_both_mean_no_declared_roots(self, tmp_path):
        assert load_project_config(
            write_project(tmp_path, "project: p\nartifact_roots:\n")
        ).artifact_roots == []
        assert load_project_config(
            write_project(tmp_path, "project: p\n")
        ).artifact_roots == []

    def test_a_valid_list_loads(self, tmp_path):
        cfg = load_project_config(
            write_project(tmp_path, "project: p\nartifact_roots: [_bmad-output]\n")
        )
        assert cfg.artifact_roots == ["_bmad-output"]


class TestCommitScopeConfig:
    """Phase 3 deliverable 1: `commit_scope` maps a repo ('.' or an
    artifact_roots entry) to the path prefixes a phase agent may commit
    there. Garbage fails at LOAD - a typo'd key or entry discovered at run
    start would arm the guard empty (all gated commits blocked) with the
    declared scope silently attached to a repo the run never arms."""

    def test_absent_means_no_declared_scope(self, tmp_path):
        cfg = load_project_config(write_project(tmp_path, "project: p\n"))
        assert cfg.commit_scope == {}

    def test_a_valid_map_loads(self, tmp_path):
        cfg = load_project_config(
            write_project(
                tmp_path,
                "project: p\n"
                "artifact_roots: [_bmad-output]\n"
                "commit_scope:\n"
                '  ".": [ui/, backend]\n'
                "  _bmad-output: [implementation-artifacts]\n",
            )
        )
        assert cfg.commit_scope == {
            ".": ["ui/", "backend"],
            "_bmad-output": ["implementation-artifacts"],
        }

    def test_a_key_that_names_no_armed_repo_is_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="commit_scope"):
            load_project_config(
                write_project(
                    tmp_path,
                    "project: p\ncommit_scope:\n  _bmad-output: [docs]\n",
                )
            )

    def test_non_list_values_are_rejected_and_the_root_key_reads_cleanly(
        self, tmp_path
    ):
        """Copilot round-2 (suppressed pair): dotted notation rendered the
        root key as `commit_scope..` - bracket-with-repr keeps the message
        addressable for '.' and named keys alike."""
        for bad in ("ui", "null", "3"):
            with pytest.raises(ConfigError, match=r"commit_scope\['\.'\]"):
                load_project_config(
                    write_project(
                        tmp_path, f'project: p\ncommit_scope:\n  ".": {bad}\n'
                    )
                )

    def test_entry_grammar_is_the_guards_own(self, tmp_path):
        """One source of truth: the same normalize_scope that arms the
        guard validates here, so an absolute or traversing entry fails at
        load instead of at run start."""
        for bad in ("/etc", "a/../b", "~/x"):
            with pytest.raises(ConfigError, match=r"commit_scope\['\.'\]"):
                load_project_config(
                    write_project(
                        tmp_path,
                        f'project: p\ncommit_scope:\n  ".": ["{bad}"]\n',
                    )
                )

    def test_an_explicit_empty_list_is_a_legal_declaration(self, tmp_path):
        """[] says 'this repo is read-only for agents' - representable on
        purpose, distinct from an absent declaration only in intent."""
        cfg = load_project_config(
            write_project(tmp_path, 'project: p\ncommit_scope:\n  ".": []\n')
        )
        assert cfg.commit_scope == {".": []}
