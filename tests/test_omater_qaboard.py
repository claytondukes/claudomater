"""QA-board finish flow (Phase 3 deliverable 3, part 2).

The board is a local STUB server (loopback, per-test), the gate a fake
script whose exit code the test controls - the flow's contract is
judged end to end without a real board. The acceptance replays classify
the REAL merged file sets (guarded, read-only) through the full flow.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from claudomater.qaboard import (
    ProofCheck,
    QaBoardConfig,
    QaBoardError,
    author_step,
    close_epic,
    epic_of,
    finish_story,
    load_spec,
    next_step_key,
    run_gate,
    section_id_for_epic,
    spec_path,
    verify_step_proofs,
)
from claudomater.surface import SurfaceRules

RULES = SurfaceRules(
    surface=("app/src/**",),
    exclude=("app/src/test/**", "docs/**"),
    exclude_root_dotfiles=True,
)

PARITY = Path(os.environ.get("OMATER_PARITY_ROOT") or "/nonexistent")
requires_parity = pytest.mark.skipif(
    not (PARITY / ".omater.yaml").is_file(),
    reason="parity checkout not configured (OMATER_PARITY_ROOT)",
)


class _StubBoard(BaseHTTPRequestHandler):
    sections: list[dict] = []
    steps: dict[int, list[dict]] = {}  # section id -> current steps (GET)
    posted: list[tuple[str, dict]] = []
    fail_next_post = False
    post_body_override: str | None = None

    def log_message(self, *args):  # quiet
        pass

    def do_GET(self):
        if self.path == "/api/sections":
            body = json.dumps(self.sections).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/sections/") and self.path.endswith("/steps"):
            section_id = int(self.path.split("/")[3])
            body = json.dumps(self.steps.get(section_id, [])).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"{}")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).posted.append((self.path, payload))
        if type(self).fail_next_post:
            type(self).fail_next_post = False
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"detail": "simulated board failure"}')
            return
        if type(self).post_body_override is not None:
            body = type(self).post_body_override.encode()
        else:
            body = json.dumps({"id": 555, **payload}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def board():
    _StubBoard.sections = [
        {"id": 7, "epic_id": "34"},
        {"id": 9, "epic_id": "4-5"},
        {"id": 11, "epic_id": "9"},
    ]
    _StubBoard.steps = {}
    _StubBoard.posted = []
    _StubBoard.fail_next_post = False
    server = HTTPServer(("127.0.0.1", 0), _StubBoard)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/api"
    finally:
        # shutdown stops the serve loop; server_close releases the
        # listening socket - skipping it leaks fds across repeated runs
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def cfg(tmp_path, board):
    gate_dir = tmp_path / "server"
    gate_dir.mkdir()
    gate = gate_dir / "gate.sh"
    gate.write_text("#!/bin/sh\necho \"PASS epic $1\"\nexit 0\n", encoding="utf-8")
    gate.chmod(0o755)
    return QaBoardConfig(
        authoring_dir=tmp_path / "authoring",
        board_url=board,
        gate_dir=gate_dir,
        gate=("./gate.sh", "{epic}"),
    )


class _Log:
    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []

    def event(self, phase, event, detail, story_key=None):
        self.events.append((phase, event, detail))


class TestAdapterConfig:
    def test_null_means_no_board(self, tmp_path):
        assert QaBoardConfig.from_adapter(None, tmp_path) is None

    def test_a_full_mapping_loads_with_relative_paths_anchored(self, tmp_path):
        cfg = QaBoardConfig.from_adapter(
            {
                "authoring_dir": "art/authoring",
                "board_url": "http://127.0.0.1:1/api/",
                "gate_dir": "art/server",
                "gate": ["./g.sh", "{epic}"],
            },
            tmp_path,
        )
        assert cfg.authoring_dir == tmp_path / "art/authoring"
        assert cfg.board_url == "http://127.0.0.1:1/api"  # trailing / trimmed

    def test_a_half_declared_adapter_is_refused(self, tmp_path):
        with pytest.raises(QaBoardError, match="missing"):
            QaBoardConfig.from_adapter({"board_url": "http://x/api"}, tmp_path)
        with pytest.raises(QaBoardError, match="mapping"):
            QaBoardConfig.from_adapter("lz-qa-viewer", tmp_path)


class TestSpecAuthoring:
    def test_epic_of_supports_compound_epics(self):
        assert epic_of("43-2") == "43"
        assert epic_of("4-5-1") == "4-5"
        with pytest.raises(QaBoardError, match="malformed"):
            epic_of("epic-43")

    def test_epic_of_accepts_a_split_story_letter_suffix(self):
        # A story split mid-epic into siblings keeps its number with one
        # lowercase letter appended (a consumer's 56-4 -> 56-4a / 56-4b, 2026-09-20);
        # the finish flow must author their board steps like any other story.
        assert epic_of("56-4a") == "56"
        assert epic_of("56-4b") == "56"
        assert epic_of("4-5-1c") == "4-5"
        for bad in ("56-4ab", "56-a", "56-4A", "56a-4", "56-4-", "56-4a-"):
            with pytest.raises(QaBoardError, match="malformed"):
                epic_of(bad)

    def test_absent_spec_starts_a_skeleton_and_malformed_raises(self, cfg):
        path = spec_path(cfg, "34")
        assert load_spec(path, "34")["steps"] == []
        path.parent.mkdir(parents=True)
        path.write_text("not json", encoding="utf-8")
        with pytest.raises(QaBoardError, match="cannot be read"):
            load_spec(path, "34")

    def test_next_step_key_continues_the_story_sequence(self):
        spec = {"steps": [
            {"step_key": "34-36-01"},
            {"step_key": "34-36-02"},
            {"step_key": "34-2-09"},
        ]}
        assert next_step_key(spec, "34-36") == "34-36-03"
        assert next_step_key(spec, "34-9") == "34-9-01"
        assert next_step_key(spec, "56-4a") == "56-4a-01"

    def test_author_step_appends_and_never_rewrites(self, cfg):
        first = author_step(cfg, "34", "34-36", "34-36 click the chart", "ui/x.ts:1")
        assert first["step_key"] == "34-36-01"
        second = author_step(cfg, "34", "34-36", "34-36 check the band", "ui/x.ts:9")
        assert second["step_key"] == "34-36-02"
        spec = json.loads(spec_path(cfg, "34").read_text())
        assert [s["step_key"] for s in spec["steps"]] == ["34-36-01", "34-36-02"]
        assert spec["steps"][0]["label"] == "34-36 click the chart"  # untouched

    def test_label_must_reference_its_story(self, cfg):
        """Coverage is matched on the label's leading story reference -
        a label without it authors a step the gate cannot credit."""
        with pytest.raises(QaBoardError, match="must start with its story id"):
            author_step(cfg, "34", "34-36", "click the chart", "ui/x.ts:1")

    def test_surface_proof_is_required(self, cfg):
        """The board's loader refuses non-waived steps without one - the
        step would never reach the board."""
        with pytest.raises(QaBoardError, match="surface_proof"):
            author_step(cfg, "34", "34-36", "34-36 click", "   ")


class TestBoardCalls:
    def test_section_resolution_by_epic(self, cfg):
        assert section_id_for_epic(cfg, "34") == 7
        assert section_id_for_epic(cfg, "4-5") == 9

    def test_a_missing_section_is_a_loud_stop(self, cfg):
        with pytest.raises(QaBoardError, match="no board section"):
            section_id_for_epic(cfg, "99")

    def test_an_unreachable_board_is_a_loud_stop_not_a_skip(self, tmp_path):
        cfg = QaBoardConfig(
            authoring_dir=tmp_path,
            board_url="http://127.0.0.1:0/api",  # port 0: deterministically unbound
            gate_dir=tmp_path,
            gate=("true",),
        )
        with pytest.raises(QaBoardError, match="unreachable"):
            section_id_for_epic(cfg, "34")


class TestGate:
    def test_exit_zero_passes_and_epic_substitutes(self, cfg):
        run_gate(cfg, "34")  # no raise

    def test_nonzero_fails_with_the_gates_own_output(self, cfg):
        gate = cfg.gate_dir / "gate.sh"
        gate.write_text(
            "#!/bin/sh\necho \"FAIL - (b) R1 coverage: no current step\"\nexit 1\n",
            encoding="utf-8",
        )
        gate.chmod(0o755)
        with pytest.raises(QaBoardError, match="R1 coverage"):
            run_gate(cfg, "34")

    def test_judged_by_exit_code_not_output_text(self, cfg):
        """A gate that PRINTS 'PASS' but exits 1 is a failing gate - text
        parsing is how false passes get minted."""
        gate = cfg.gate_dir / "gate.sh"
        gate.write_text("#!/bin/sh\necho PASS\nexit 1\n", encoding="utf-8")
        gate.chmod(0o755)
        with pytest.raises(QaBoardError, match="exit 1"):
            run_gate(cfg, "34")


class TestFinishFlow:
    def test_no_surface_records_the_waiver_evaluation(self, cfg):
        log = _Log()
        result = finish_story(
            "34-36", ["docs/guide.md", ".gitignore"], RULES, cfg, log
        )
        assert result["ok"] and result["step_required"] is False
        events = [e for e in log.events if e[1] == "qa-board-waiver"]
        assert len(events) == 1
        # the EVALUATION is the artifact: buckets ride into the event
        assert events[0][2]["excluded"] == ["docs/guide.md", ".gitignore"]
        assert _StubBoard.posted == []  # nothing touched the board

    def test_surface_authors_posts_and_gates_before_ok(self, cfg):
        log = _Log()
        result = finish_story(
            "34-36",
            ["app/src/Widget.tsx", "docs/guide.md"],
            RULES,
            cfg,
            log,
            step_label="34-36 click-to-search walkthrough",
            surface_proof="app/src/Widget.tsx:12",
        )
        assert result == {
            "ok": True,
            "step_required": True,
            "step_key": "34-36-01",
            "section_id": 7,
            "surface_touching": True,
            "surface": ["app/src/Widget.tsx"],
            "excluded": ["docs/guide.md"],
            "neutral": [],
        }
        assert [p for p, _ in _StubBoard.posted] == ["/api/sections/7/steps"]
        assert _StubBoard.posted[0][1]["step_key"] == "34-36-01"
        # intent BEFORE each action, completion after (the run log's own
        # write-ahead contract): a crash between any pair shows exactly
        # what was in flight
        assert [e[1] for e in log.events] == [
            "qa-board-proof-check-skipped",
            "qa-board-step",
            "qa-board-post",
            "qa-board-posted",
            "qa-board-gate",
            "qa-board-gate-pass",
        ]
        # events[0] is the logged proof-check skip (no project_root here)
        assert log.events[1][2]["step_label"].startswith("34-36 ")

    def test_a_surface_story_without_step_content_is_a_loud_stop(self, cfg):
        with pytest.raises(QaBoardError, match="no walkthrough step content"):
            finish_story("34-36", ["app/src/Widget.tsx"], RULES, cfg, _Log())

    def test_a_board_500_stops_the_flow_after_authoring(self, cfg):
        """The spec append lands, the POST failure stops the flow, and no
        gate-pass event is minted. Retry semantics live in
        test_a_retry_after_a_mid_flow_crash_converges: identical content
        reuses the appended step instead of minting a sibling."""
        _StubBoard.fail_next_post = True
        log = _Log()
        with pytest.raises(QaBoardError, match="board 500"):
            finish_story(
                "34-36",
                ["app/src/Widget.tsx"],
                RULES,
                cfg,
                log,
                step_label="34-36 walkthrough",
                surface_proof="app/src/Widget.tsx:1",
            )
        names = [e[1] for e in log.events]
        assert "qa-board-post" in names  # the intent that was in flight
        assert "qa-board-posted" not in names
        assert "qa-board-gate-pass" not in names

    def test_a_failing_gate_blocks_the_ok(self, cfg):
        gate = cfg.gate_dir / "gate.sh"
        gate.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
        gate.chmod(0o755)
        with pytest.raises(QaBoardError, match="exit 3"):
            finish_story(
                "34-36",
                ["app/src/Widget.tsx"],
                RULES,
                cfg,
                _Log(),
                step_label="34-36 walkthrough",
                surface_proof="app/src/Widget.tsx:1",
            )


def _merged_files(sha: str) -> list[str]:
    out = subprocess.run(
        ["git", "show", "--name-only", "--format=", sha],
        cwd=PARITY, capture_output=True, text=True, check=True,
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


@requires_parity
class TestParityFinishReplays:
    """The named acceptance proofs: the real merged file sets, the real
    committed rules, the full flow against the stub board + fake gate.
    Story 34-36's merge must drive the SURFACE path (it shipped without a
    board step and the gap was repaired by hand - this flow exists so
    that never recurs); story 46-7's must drive the WAIVER path."""

    def _rules(self):
        from claudomater.config import load_project_config

        rules = load_project_config(PARITY).surface_rules
        assert rules is not None
        return rules

    def test_34_36_drives_the_surface_path_end_to_end(self, cfg):
        log = _Log()
        result = finish_story(
            "34-36",
            _merged_files("a5105e31"),
            self._rules(),
            cfg,
            log,
            step_label="34-36 time-series click-to-search walkthrough",
            surface_proof="ui/src/components/charts/bandClick.ts:1",
        )
        assert result["ok"] and result["step_required"] is True
        assert result["step_key"] == "34-36-01"
        assert [e[1] for e in log.events][-1] == "qa-board-gate-pass"

    def test_46_7_drives_the_waiver_path_with_the_evaluation_recorded(self, cfg):
        log = _Log()
        result = finish_story(
            "46-7", _merged_files("5b26c746"), self._rules(), cfg, log
        )
        assert result["ok"] and result["step_required"] is False
        (event,) = [e for e in log.events if e[1] == "qa-board-waiver"]
        assert "CLAUDE.md" in event[2]["neutral"]
        assert _StubBoard.posted == []


class TestRoundTwoHardening:
    """Copilot round-2 suppressed findings, all three real."""

    def test_a_sibling_story_prefix_cannot_credit_the_label(self, cfg):
        """The digit-boundary lesson from the coverage regex, resurfacing:
        startswith('34-3') matches a '34-36 ...' label, so a step authored
        for one story could credit its sibling. The id must end at a
        non-digit boundary."""
        with pytest.raises(QaBoardError, match="must start with its story id"):
            author_step(cfg, "34", "34-3", "34-36 walkthrough", "x.ts:1")
        # legit punctuation after the id stays legal
        step = author_step(cfg, "34", "34-3", "34-3: walkthrough", "x.ts:1")
        assert step["step_key"] == "34-3-01"
        # the split-story letter is a boundary too: a "56-4a ..." label must
        # not credit story 56-4, and 56-4a's own label authors under its key
        with pytest.raises(QaBoardError, match="must start with its story id"):
            author_step(cfg, "56", "56-4", "56-4a One deployer walkthrough", "x.ts:1")
        step = author_step(cfg, "56", "56-4a", "56-4a One deployer walkthrough", "x.ts:1")
        assert step["step_key"] == "56-4a-01"

    def test_malformed_section_objects_are_a_typed_stop(self, cfg):
        _StubBoard.sections = [{"epic_id": "34"}]  # no id field
        with pytest.raises(QaBoardError, match="section"):
            section_id_for_epic(cfg, "34")
        _StubBoard.sections = [{"epic_id": "34", "id": "seven"}]
        with pytest.raises(QaBoardError, match="section"):
            section_id_for_epic(cfg, "34")

    def test_a_board_response_without_an_id_is_a_loud_stop(self, cfg):
        """posted.get('id') on a malformed payload either crashed untyped
        (non-dict) or silently recorded board_step_id=None - the flow must
        refuse a response it cannot anchor an audit trail to."""
        from claudomater.qaboard import post_step

        step = {
            "step_key": "34-3-01", "label": "34-3 x", "surface_proof": "x.ts:1",
        }
        _StubBoard.post_body_override = "[]"
        try:
            with pytest.raises(QaBoardError, match="board response"):
                post_step(cfg, 7, step)
            for bad in ('{"ok": true}', '{"id": null}', '{"id": "abc"}'):
                _StubBoard.post_body_override = bad
                with pytest.raises(QaBoardError, match="board response"):
                    post_step(cfg, 7, step)
        finally:
            _StubBoard.post_body_override = None

    def test_a_sub_story_id_cannot_credit_the_parent_either(self, cfg):
        """Round-3: '34-3' with a '34-3-1 ...' label passed the bare digit
        guard (the next char is '-'). Compound ids are real in this grammar,
        so the boundary must refuse a continuing '-<digit>' segment too."""
        with pytest.raises(QaBoardError, match="must start with its story id"):
            author_step(cfg, "34", "34-3", "34-3-1 nested walkthrough", "x.ts:1")

    def test_non_object_section_elements_are_a_typed_stop(self, cfg):
        _StubBoard.sections = ["oops", {"epic_id": "34", "id": 7}]
        with pytest.raises(QaBoardError, match="section"):
            section_id_for_epic(cfg, "34")

    def test_a_retry_after_a_mid_flow_crash_converges(self, cfg):
        """Round-5 (the docstring's claim was wrong, and the truth was
        worse): a naive retry re-ran author_step, computed the NEXT
        sequence number, and appended a SECOND spec entry for the same
        story. Identical content now reuses the existing step, so
        retry-after-crash converges: spec unchanged, POST idempotent
        server-side, gate re-run harmless."""
        log = _Log()
        _StubBoard.fail_next_post = True
        with pytest.raises(QaBoardError, match="board 500"):
            finish_story(
                "34-36", ["app/src/W.tsx"], RULES, cfg, log,
                step_label="34-36 walkthrough", surface_proof="app/src/W.tsx:1",
            )
        result = finish_story(
            "34-36", ["app/src/W.tsx"], RULES, cfg, log,
            step_label="34-36 walkthrough", surface_proof="app/src/W.tsx:1",
        )
        assert result["ok"] and result["step_key"] == "34-36-01"
        spec = json.loads(spec_path(cfg, "34").read_text())
        assert [s["step_key"] for s in spec["steps"]] == ["34-36-01"]  # ONE entry

    def test_the_intent_event_records_what_gets_persisted(self, cfg):
        """Round-5: the intent event logged raw values while author_step
        stripped them - the audit trail must match the persisted bytes."""
        log = _Log()
        finish_story(
            "34-36", ["app/src/W.tsx"], RULES, cfg, log,
            step_label="  34-36 walkthrough  ", surface_proof=" app/src/W.tsx:1 ",
        )
        intent = [e for e in log.events if e[1] == "qa-board-step"][0][2]
        assert intent["step_label"] == "34-36 walkthrough"
        assert intent["surface_proof"] == "app/src/W.tsx:1"

    def test_whitespace_only_adapter_fields_fail_at_load(self, tmp_path):
        """Round-10 pins: blank-after-strip values passed the truthiness
        check and failed later as confusing runtime errors."""
        base = {
            "authoring_dir": "a",
            "board_url": "http://x/api",
            "gate_dir": "s",
            "gate": ["./g.sh"],
        }
        for key, bad in (
            ("board_url", "   "),
            ("authoring_dir", " "),
            ("gate_dir", "\t"),
            ("gate", ["./g.sh", "   "]),
        ):
            raw = {**base, key: bad}
            with pytest.raises(QaBoardError, match="qa_board"):
                QaBoardConfig.from_adapter(raw, tmp_path)

    def test_a_spec_with_duplicate_step_keys_is_refused(self, cfg):
        path = spec_path(cfg, "34")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"epic_id": "34", "steps": [
                {"step_key": "34-1-01", "label": "34-1 a"},
                {"step_key": "34-1-01", "label": "34-1 b"},
            ]}),
            encoding="utf-8",
        )
        with pytest.raises(QaBoardError, match="duplicate step_key"):
            load_spec(path, "34")

    def test_post_step_refuses_a_proofless_step(self, cfg):
        """Round-12: a caller bypassing author_step could POST null
        surface_proof; the board strands non-waived steps without one."""
        from claudomater.qaboard import post_step

        with pytest.raises(QaBoardError, match="surface_proof"):
            post_step(cfg, 7, {"step_key": "34-3-01", "label": "34-3 x"})
        with pytest.raises(QaBoardError, match="surface_proof"):
            post_step(
                cfg, 7,
                {"step_key": "34-3-01", "label": "34-3 x", "surface_proof": "  "},
            )


class TestCloseEpic:
    """Retirement condition 2 (epic-47 retro F4): artifacts pushed before
    the gate, and the regenerated matrix's audited count vs the epic's
    story count - a mismatch FAILS, it is not a warning."""

    SPRINT = (
        "# preamble\n"
        "development_status:\n"
        "  epic-9: done\n"
        "  9-1-first-thing: done\n"
        "  9-2-second-thing: done\n"
        "  9-3-abandoned: superseded\n"
        "  epic-9-retrospective: fable-review-required\n"
    )

    def _arrange(self, tmp_path, audited=2, dirty=False, unpushed=False,
                 board_url="http://board.invalid/api"):
        import subprocess as sp

        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_CONFIG_SYSTEM": os.devnull}

        def git(cwd, *args):
            sp.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)

        upstream = tmp_path / "upstream.git"
        upstream.mkdir()
        git(upstream, "init", "-q", "--bare")
        artifacts = tmp_path / "artifacts"
        git(tmp_path, "clone", "-q", str(upstream), str(artifacts))
        git(artifacts, "config", "user.email", "t@example.invalid")
        git(artifacts, "config", "user.name", "T")
        (artifacts / "qa-viewer" / "authoring").mkdir(parents=True)
        (artifacts / "qa-viewer" / "coverage").mkdir(parents=True)
        (artifacts / "qa-viewer" / "coverage" / "epic-9-coverage.md").write_text(
            "# Epic 9 coverage\n\n- **Story files audited:** 1\n"
        )
        git(artifacts, "add", "-A")
        git(artifacts, "commit", "-qm", "seed")
        git(artifacts, "push", "-q", "-u", "origin", "HEAD")
        if unpushed:
            (artifacts / "late-story.md").write_text("late\n")
            git(artifacts, "add", "-A")
            git(artifacts, "commit", "-qm", "late artifacts, not pushed")
        if dirty:
            (artifacts / "wip.md").write_text("wip\n")
        sprint = tmp_path / "sprint-status.yaml"
        sprint.write_text(self.SPRINT)
        # the fake lab gate: rewrites the matrix (like --write-coverage),
        # commits and pushes it - the real gate's observable contract
        gate = tmp_path / "gate.sh"
        gate.write_text(
            "#!/bin/sh\nset -e\n"
            f"cd {artifacts}\n"
            f"printf '# Epic 9 coverage\\n\\n- **Story files audited:** {audited}\\n'"
            " > qa-viewer/coverage/epic-9-coverage.md\n"
            "if [ -n \"$(git status --porcelain)\" ]; then\n"
            "  git add -A && git commit -qm 'matrix regen' && git push -q\n"
            "fi\n"
        )
        gate.chmod(0o755)
        cfg = QaBoardConfig(
            authoring_dir=artifacts / "qa-viewer" / "authoring",
            board_url=board_url,
            gate_dir=tmp_path,
            gate=("./gate.sh", "{epic}"),
        )
        return cfg, sprint

    def test_happy_path_logs_the_matching_count(self, tmp_path, board):
        cfg, sprint = self._arrange(tmp_path, audited=2, board_url=board)
        log = _Log()
        result = close_epic(tmp_path, cfg, "9", sprint, log)
        assert result == {"epic": "9", "gate": "PASS", "audited": 2, "expected": 2}
        kinds = [e[1] for e in log.events]
        assert kinds == [
            "close-gate-precheck", "close-proof-check", "close-gate", "close-gate-count",
        ]
        count_detail = log.events[-1][2]
        assert (count_detail["audited"], count_detail["expected"]) == (2, 2)
        pre = log.events[0][2]
        assert pre["story_keys"] == ["9-1-first-thing", "9-2-second-thing"]

    def test_red_the_47_4_shape_unpushed_artifacts_stop_the_close(self, tmp_path):
        """One story's artifacts sit local-only at gate time - the exact
        F4 incident. The precheck stops BEFORE the gate runs."""
        cfg, sprint = self._arrange(tmp_path, unpushed=True)
        log = _Log()
        with pytest.raises(QaBoardError, match="unpushed commit"):
            close_epic(tmp_path, cfg, "9", sprint, log)
        assert log.events == []  # write-ahead: nothing passed the precheck

    def test_uncommitted_artifacts_stop_the_close(self, tmp_path):
        cfg, sprint = self._arrange(tmp_path, dirty=True)
        with pytest.raises(QaBoardError, match="uncommitted"):
            close_epic(tmp_path, cfg, "9", sprint, _Log())

    def test_a_count_mismatch_fails_loudly_not_a_warning(self, tmp_path, board):
        cfg, sprint = self._arrange(tmp_path, audited=1, board_url=board)
        log = _Log()
        with pytest.raises(QaBoardError, match="audited 1 story file"):
            close_epic(tmp_path, cfg, "9", sprint, log)
        count_detail = log.events[-1][2]
        assert count_detail["ok"] is False

    def test_superseded_stories_do_not_count(self, tmp_path, board):
        """9-3 is superseded: expected is 2, so a matrix auditing 2 passes
        and one auditing 3 would fail - superseded stories own no
        artifacts and no audit row."""
        cfg, sprint = self._arrange(tmp_path, audited=3, board_url=board)
        with pytest.raises(QaBoardError, match="audited 3"):
            close_epic(tmp_path, cfg, "9", sprint, _Log())

    def test_a_drifted_board_stops_the_close_before_the_gate(self, tmp_path, board):
        """Epic-61 retro A2: the range-only gate passed three drifted boards;
        the close now re-greps every current step first and stops loudly."""
        cfg, sprint = self._arrange(tmp_path, audited=2, board_url=board)
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "main.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
        _StubBoard.steps[11] = [
            {"step_key": "9-1-01", "retired": False,
             "surface_proof": 'grep -nF "y = 2" app/main.py (moved by a sibling, app/main.py:1)'},
        ]
        log = _Log()
        with pytest.raises(QaBoardError, match="board proof drift on section 11"):
            close_epic(tmp_path, cfg, "9", sprint, log)
        names = [e[1] for e in log.events]
        assert "close-proof-check" in names and "close-gate" not in names

    def test_the_regex_reads_the_real_gen_coverage_header(self):
        """The epic-48 live run: gen_coverage.py writes markdown bold, and
        the plain-form-only regex refused the real matrix. Verbatim
        production header block, both forms pinned."""
        from claudomater.qaboard import _AUDITED_RE

        production = (
            "# Coverage matrix - Epic 48: Epic 48 - Notification Deep-Link "
            "Filter Contract\n\n"
            "- **Epic:** 48\n"
            "- **Current (non-retired) steps in DB:** 1\n"
            "- **Story files audited:** 1\n"
        )
        m = _AUDITED_RE.search(production)
        assert m and m.group(1) == "1"
        plain = _AUDITED_RE.search("Story files audited: 4")
        assert plain and plain.group(1) == "4"

    def test_a_matrix_without_the_count_line_fails(self, tmp_path, board):
        cfg, sprint = self._arrange(tmp_path, audited=2, board_url=board)
        gate = tmp_path / "gate.sh"
        gate.write_text(
            "#!/bin/sh\nset -e\n"
            f"cd {cfg.authoring_dir.parent.parent}\n"
            "printf 'no count here\\n' > qa-viewer/coverage/epic-9-coverage.md\n"
            "git add -A && git commit -qm regen && git push -q\n"
        )
        with pytest.raises(QaBoardError, match="no 'Story files audited"):
            close_epic(tmp_path, cfg, "9", sprint, _Log())

    def test_an_unknown_epic_id_raises_from_the_sprint_file(self, tmp_path):
        from claudomater.sprint import SprintError

        cfg, sprint = self._arrange(tmp_path)
        with pytest.raises(SprintError, match="no epic-77 line"):
            close_epic(tmp_path, cfg, "77", sprint, _Log())


class TestFinishStoryPersistsMetrics:
    def test_a_waiver_story_writes_its_row_in_the_flow(self, tmp_path):
        from claudomater.metrics import load_rows

        facts = {
            "pr": 389, "merge_sha": "f" * 40, "converge_rounds": 2,
            "threads_fixed": 0, "threads_dismissed": 0,
            "suppressed_fixed": 0, "suppressed_dismissed": 0,
            "wall_minutes": 100, "cost_usd": 20.23, "parks": 1,
            "merge_bypass": True,
        }
        cfg = QaBoardConfig(
            authoring_dir=tmp_path / "authoring",
            board_url="http://board.invalid/api",
            gate_dir=tmp_path,
            gate=("true",),
        )
        mpath = tmp_path / "run-metrics" / "stories.jsonl"
        log = _Log()
        result = finish_story(
            "9-1", ["docs/x.md"], RULES, cfg, log,
            metrics_facts=facts, metrics_path=mpath,
        )
        assert result["metrics_row"]["outcome"] == {"kind": "waiver"}
        assert load_rows(mpath)[0]["story_id"] == "9-1"
        assert any(e[1] == "metrics-row" for e in log.events)

    def test_half_wired_metrics_refuse(self, tmp_path):
        cfg = QaBoardConfig(
            authoring_dir=tmp_path / "authoring",
            board_url="http://board.invalid/api",
            gate_dir=tmp_path,
            gate=("true",),
        )
        with pytest.raises(QaBoardError, match="come together"):
            finish_story(
                "9-1", ["docs/x.md"], RULES, cfg, _Log(),
                metrics_path=tmp_path / "m.jsonl",
            )

    def test_the_intent_event_survives_a_failed_append(self, tmp_path):
        """Write-ahead discipline: the metrics-row event lands BEFORE the
        append, so a refusal (or crash) mid-write leaves the run log
        showing what was in flight - and the event claims no outcome."""
        from claudomater.metrics import append_row, compose_row

        facts = {
            "pr": 389, "merge_sha": "f" * 40, "converge_rounds": 2,
            "threads_fixed": 0, "threads_dismissed": 0,
            "suppressed_fixed": 0, "suppressed_dismissed": 0,
            "wall_minutes": 100, "cost_usd": 20.23, "parks": 1,
            "merge_bypass": True,
        }
        cfg = QaBoardConfig(
            authoring_dir=tmp_path / "authoring",
            board_url="http://board.invalid/api",
            gate_dir=tmp_path,
            gate=("true",),
        )
        mpath = tmp_path / "run-metrics" / "stories.jsonl"
        append_row(mpath, compose_row(
            "9-1", "9", {"surface_touching": False},
            {**facts, "cost_usd": 1.0},
        ))
        log = _Log()
        with pytest.raises(QaBoardError, match="DIFFERENT row"):
            finish_story(
                "9-1", ["docs/x.md"], RULES, cfg, log,
                metrics_facts=facts, metrics_path=mpath,
            )
        intents = [e for e in log.events if e[1] == "metrics-row"]
        assert intents, "write-ahead intent event missing after failed append"
        assert intents[0][2]["row"]["story_id"] == "9-1"
        assert "written" not in intents[0][2]


class TestProofContentCheck:
    """Epic-61 retro A2: the range-only gate read PASS over drifted boards in
    three consecutive consumer epics; the finish and the close now re-run
    every current step's proof greps against the project tree."""

    @staticmethod
    def _tree(tmp_path):
        root = tmp_path / "project"
        (root / "app" / "src").mkdir(parents=True)
        (root / "app" / "src" / "Widget.tsx").write_text(
            "line one\nexport function Widget() {\n  return null;\n}\n",
            encoding="utf-8",
        )
        return root

    @staticmethod
    def _proof(*entries):
        return "; ".join(
            f'grep -nF "{needle}" {path} ({note}, {path}:{line})'
            for needle, path, note, line in entries
        )

    def test_every_anchor_lands_on_its_cited_line(self, cfg, tmp_path):
        root = self._tree(tmp_path)
        _StubBoard.steps[7] = [
            {"step_key": "34-1-01", "retired": False, "surface_proof": self._proof(
                ("export function Widget() {", "app/src/Widget.tsx", "the component", 2),
                ("return null;", "app/src/Widget.tsx", "the render", 3),
            )},
        ]
        check = verify_step_proofs(cfg, 7, root)
        assert check == ProofCheck(section_id=7, steps=1, anchors=2, unparsed=0, drift=())

    def test_a_drifted_anchor_names_the_step_the_line_and_the_hits(self, cfg, tmp_path):
        root = self._tree(tmp_path)
        _StubBoard.steps[7] = [
            {"step_key": "34-1-01", "retired": False, "surface_proof": self._proof(
                ("return null;", "app/src/Widget.tsx", "cited one line too early", 2),
            )},
        ]
        check = verify_step_proofs(cfg, 7, root)
        assert check.anchors == 1 and len(check.drift) == 1
        assert check.drift[0].startswith("34-1-01: app/src/Widget.tsx:2 -> hits [3]")

    def test_a_needle_that_hits_nothing_is_drift(self, cfg, tmp_path):
        root = self._tree(tmp_path)
        _StubBoard.steps[7] = [
            {"step_key": "34-1-01", "retired": False, "surface_proof": self._proof(
                ("text a sibling merge rewrote", "app/src/Widget.tsx", "gone", 2),
            )},
        ]
        line = verify_step_proofs(cfg, 7, root).drift[0]
        assert line == "34-1-01: app/src/Widget.tsx:2 -> hits []"
        assert "rewrote" not in line  # never the needle text: the run log has no scrubber

    def test_retired_and_waived_steps_are_skipped_and_range_only_proofs_are_counted(
        self, cfg, tmp_path
    ):
        root = self._tree(tmp_path)
        _StubBoard.steps[7] = [
            {"step_key": "34-1-01", "retired": True, "surface_proof": self._proof(
                ("nowhere", "app/src/Widget.tsx", "retired, never judged", 9),
            )},
            {"step_key": "34-PRE-01", "retired": False, "surface_proof": ""},
            {"step_key": "34-2-01", "retired": False,
             "surface_proof": "app/src/Widget.tsx:2 the component (range-only proof)"},
        ]
        check = verify_step_proofs(cfg, 7, root)
        assert (check.steps, check.anchors, check.unparsed, check.drift) == (1, 0, 1, ())
        assert check.unparsed_steps == ("34-2-01",)

    def test_a_range_only_proof_fails_the_gate_not_just_the_count(self, cfg, tmp_path):
        root = self._tree(tmp_path)
        _StubBoard.steps[7] = [
            {"step_key": "34-2-01", "retired": False,
             "surface_proof": "app/src/Widget.tsx:2 the component (range-only proof)"},
        ]
        with pytest.raises(QaBoardError, match="no grep entry \\(34-2-01\\)"):
            finish_story(
                "34-36", ["app/src/Widget.tsx"], RULES, cfg, _Log(),
                step_label="34-36 walkthrough", surface_proof="app/src/Widget.tsx:3",
                project_root=root,
            )
        assert _StubBoard.posted == []

    def test_a_malformed_trailing_fragment_is_drift_not_ignored(self, cfg, tmp_path):
        root = self._tree(tmp_path)
        _StubBoard.steps[7] = [
            {"step_key": "34-1-01", "retired": False, "surface_proof": self._proof(
                ("return null;", "app/src/Widget.tsx", "the render", 3),
            ) + '; grep -nF "unparseable" app/src/Widget.tsx'},
        ]
        check = verify_step_proofs(cfg, 7, root)
        assert check.anchors == 1
        assert check.drift == (
            "34-1-01: 1 grep fragment(s) could not be parsed (2 present, 1 parsed) - "
            'every entry must be `grep -nF "<needle>" <path> (... <path>:<line>)`',
        )

    @pytest.mark.parametrize("value", ["false", 0, 1, None, "yes"])
    def test_a_non_boolean_retired_flag_is_a_loud_stop(self, cfg, tmp_path, value):
        _StubBoard.steps[7] = [
            {"step_key": "34-1-01", "retired": value,
             "surface_proof": self._proof(("return null;", "app/src/Widget.tsx", "the render", 3))},
        ]
        with pytest.raises(QaBoardError, match="non-boolean retired flag"):
            verify_step_proofs(cfg, 7, self._tree(tmp_path))

    @pytest.mark.parametrize("sep", [";grep", ";  grep", "; grep"])
    def test_every_delimiter_spelling_is_seen_by_parser_and_counter_alike(self, cfg, tmp_path, sep):
        root = self._tree(tmp_path)
        _StubBoard.steps[7] = [
            {"step_key": "34-1-01", "retired": False, "surface_proof": self._proof(
                ("return null;", "app/src/Widget.tsx", "the render", 3),
            ) + f'{sep} -nF "unparseable" app/src/Widget.tsx'},
        ]
        check = verify_step_proofs(cfg, 7, root)
        assert check.anchors == 1 and len(check.drift) == 1
        assert check.drift[0].startswith("34-1-01: 1 grep fragment(s) could not be parsed (2 present, 1 parsed)")

    def test_two_valid_entries_joined_without_a_space_both_run(self, cfg, tmp_path):
        root = self._tree(tmp_path)
        _StubBoard.steps[7] = [
            {"step_key": "34-1-01", "retired": False, "surface_proof":
             'grep -nF "line one" app/src/Widget.tsx (first, app/src/Widget.tsx:1);grep -nF "return null;" app/src/Widget.tsx (second, app/src/Widget.tsx:2)'},
        ]
        check = verify_step_proofs(cfg, 7, root)
        assert check.anchors == 2
        assert check.drift == ("34-1-01: app/src/Widget.tsx:2 -> hits [3]",)

    @pytest.mark.parametrize("key", [None, "", "   ", 7])
    def test_a_row_without_a_step_key_is_a_loud_stop(self, cfg, tmp_path, key):
        row = {"retired": False, "surface_proof": self._proof(("return null;", "app/src/Widget.tsx", "the render", 3))}
        if key is not None:
            row["step_key"] = key
        _StubBoard.steps[7] = [row]
        with pytest.raises(QaBoardError, match="carries no step_key"):
            verify_step_proofs(cfg, 7, self._tree(tmp_path))

    @pytest.mark.parametrize("needle", ["", "   "])
    def test_an_empty_needle_is_drift_not_a_match_all(self, cfg, tmp_path, needle):
        root = self._tree(tmp_path)
        _StubBoard.steps[7] = [
            {"step_key": "34-1-01", "retired": False,
             "surface_proof": f'grep -nF "{needle}" app/src/Widget.tsx (would match every line, app/src/Widget.tsx:2)'},
        ]
        check = verify_step_proofs(cfg, 7, root)
        assert check.drift == ("34-1-01: app/src/Widget.tsx:2 has an empty needle - not an anchor",)

    @pytest.mark.parametrize("path", ["-", "app/src", "app/src/Missing.tsx"])
    def test_stdin_directories_and_missing_files_are_not_anchors(self, cfg, tmp_path, path):
        root = self._tree(tmp_path)
        _StubBoard.steps[7] = [
            {"step_key": "34-1-01", "retired": False,
             "surface_proof": f'grep -nF "line one" {path} (not a project file, {path}:1)'},
        ]
        check = verify_step_proofs(cfg, 7, root)
        assert check.drift == (f"34-1-01: {path}:1 is not a file inside the project root",)

    def test_a_timed_out_grep_never_puts_the_needle_in_the_drift_line(self, cfg, tmp_path, monkeypatch):
        import subprocess as sp

        from claudomater import qaboard as qb

        root = self._tree(tmp_path)
        secret = "hunter2-token-value"
        _StubBoard.steps[7] = [
            {"step_key": "34-1-01", "retired": False, "surface_proof": self._proof(
                (secret, "app/src/Widget.tsx", "a needle that must never be logged", 2),
            )},
        ]

        def slow(argv, **kwargs):
            raise sp.TimeoutExpired(cmd=argv, timeout=30)

        monkeypatch.setattr(qb.subprocess, "run", slow)
        check = verify_step_proofs(cfg, 7, root)
        assert check.drift == ("34-1-01: app/src/Widget.tsx:2 grep timed out after 30 s",)
        assert secret not in json.dumps(check.as_dict())

    def test_a_nul_byte_in_a_needle_is_drift_with_the_class_name_only(self, cfg, tmp_path):
        root = self._tree(tmp_path)
        _StubBoard.steps[7] = [
            {"step_key": "34-1-01", "retired": False, "surface_proof": self._proof(
                ("secret\x00value", "app/src/Widget.tsx", "an argument grep cannot take", 2),
            )},
        ]
        check = verify_step_proofs(cfg, 7, root)
        assert check.drift == ("34-1-01: app/src/Widget.tsx:2 grep could not run (ValueError)",)

    def test_an_unsupported_grep_spelling_is_a_counted_fragment(self, cfg, tmp_path):
        root = self._tree(tmp_path)
        _StubBoard.steps[7] = [
            {"step_key": "34-1-01", "retired": False, "surface_proof": self._proof(
                ("return null;", "app/src/Widget.tsx", "the render", 3),
            ) + '; grep -F "return null;" app/src/Widget.tsx (no -n, app/src/Widget.tsx:3)'},
        ]
        check = verify_step_proofs(cfg, 7, root)
        assert check.anchors == 1
        assert check.drift[0].startswith("34-1-01: 1 grep fragment(s) could not be parsed (2 present, 1 parsed)")

    def test_a_path_the_os_cannot_resolve_is_drift(self, cfg, tmp_path):
        root = self._tree(tmp_path)
        _StubBoard.steps[7] = [
            {"step_key": "34-1-01", "retired": False,
             "surface_proof": 'grep -nF "line one" app/src/Wid\u0000get.tsx (a NUL in the path, app/src/Wid\u0000get.tsx:1)'},
        ]
        check = verify_step_proofs(cfg, 7, root)
        assert len(check.drift) == 1 and "cannot be resolved" in check.drift[0]

    def test_the_new_steps_proof_must_be_the_grep_form_when_a_root_is_given(self, cfg, tmp_path):
        root = self._tree(tmp_path)
        _StubBoard.steps[7] = []
        with pytest.raises(QaBoardError, match="carries no parseable grep entry"):
            finish_story(
                "34-36", ["app/src/Widget.tsx"], RULES, cfg, _Log(),
                step_label="34-36 walkthrough", surface_proof="app/src/Widget.tsx:3",
                project_root=root,
            )
        assert _StubBoard.posted == []
        assert not (cfg.authoring_dir / "epic-34-steps.json").exists()

    def test_the_new_steps_proof_must_land_when_a_root_is_given(self, cfg, tmp_path):
        root = self._tree(tmp_path)
        _StubBoard.steps[7] = []
        with pytest.raises(QaBoardError, match="does not land on the merged tree"):
            finish_story(
                "34-36", ["app/src/Widget.tsx"], RULES, cfg, _Log(),
                step_label="34-36 walkthrough",
                surface_proof=self._proof(("return null;", "app/src/Widget.tsx", "cited too early", 2)),
                project_root=root,
            )
        assert _StubBoard.posted == []

    def test_the_error_names_every_drifted_anchor_not_a_sample(self, cfg, tmp_path):
        root = self._tree(tmp_path)
        entries = [("return null;", "app/src/Widget.tsx", f"anchor {i}", 1) for i in range(25)]
        _StubBoard.steps[7] = [{"step_key": "34-1-01", "retired": False, "surface_proof": self._proof(*entries)}]
        with pytest.raises(QaBoardError) as exc:
            finish_story(
                "34-36", ["app/src/Widget.tsx"], RULES, cfg, _Log(),
                step_label="34-36 walkthrough",
                surface_proof=self._proof(("return null;", "app/src/Widget.tsx", "the render", 3)),
                project_root=root,
            )
        assert str(exc.value).count("34-1-01: app/src/Widget.tsx:1 -> hits [3]") == 25
        assert "more)" not in str(exc.value)

    def test_a_symlink_loop_in_the_path_is_drift_never_a_raw_exception(self, cfg, tmp_path):
        root = self._tree(tmp_path)
        (root / "app" / "loop").symlink_to(root / "app" / "loop")
        _StubBoard.steps[7] = [
            {"step_key": "34-1-01", "retired": False,
             "surface_proof": 'grep -nF "line one" app/loop/x.tsx (through a symlink loop, app/loop/x.tsx:1)'},
        ]
        check = verify_step_proofs(cfg, 7, root)
        # newer Pythons resolve a loop without raising and the target is then
        # not a file; older ones raise and the path cannot be resolved -
        # fail-closed drift either way, never a raw exception
        assert len(check.drift) == 1
        assert ("cannot be resolved" in check.drift[0]) or ("is not a file" in check.drift[0])

    def test_a_resolution_runtime_error_is_drift(self, cfg, tmp_path, monkeypatch):
        from claudomater import qaboard as qb

        root = self._tree(tmp_path)
        _StubBoard.steps[7] = [
            {"step_key": "34-1-01", "retired": False, "surface_proof": self._proof(
                ("line one", "app/src/Widget.tsx", "resolution explodes", 1),
            )},
        ]

        def boom(self, *a, **k):
            raise RuntimeError("Symlink loop from 'x'")

        monkeypatch.setattr(qb.Path, "resolve", boom)
        check = verify_step_proofs(cfg, 7, root)
        assert len(check.drift) == 1 and "cannot be resolved" in check.drift[0]

    def test_a_non_object_row_is_a_loud_stop(self, cfg, tmp_path):
        _StubBoard.steps[7] = [["not", "a", "step"]]
        with pytest.raises(QaBoardError, match="a row is not a JSON object"):
            verify_step_proofs(cfg, 7, self._tree(tmp_path))

    def test_the_cited_path_must_be_the_grepped_path(self, cfg, tmp_path):
        root = self._tree(tmp_path)
        (root / "app" / "src" / "Other.tsx").write_text("export function Widget() {\n")
        _StubBoard.steps[7] = [
            {"step_key": "34-1-01", "retired": False,
             "surface_proof": 'grep -nF "export function Widget() {" app/src/Other.tsx (claims the widget, app/src/Widget.tsx:2)'},
        ]
        check = verify_step_proofs(cfg, 7, root)
        assert check.drift == (
            "34-1-01: entry greps app/src/Other.tsx but cites app/src/Widget.tsx:2 - "
            "the cited anchor must be the grepped file",
        )

    @pytest.mark.parametrize("path", ["/etc/hosts", "../outside.txt", "app/../../outside.txt"])
    def test_a_path_outside_the_project_root_is_drift_never_read(self, cfg, tmp_path, path):
        root = self._tree(tmp_path)
        (tmp_path / "outside.txt").write_text("secret needle\n", encoding="utf-8")
        _StubBoard.steps[7] = [
            {"step_key": "34-1-01", "retired": False,
             "surface_proof": f'grep -nF "secret needle" {path} (escapes the tree, {path}:1)'},
        ]
        check = verify_step_proofs(cfg, 7, root)
        assert len(check.drift) == 1 and "outside the project root" in check.drift[0]

    def test_a_regex_proof_keeps_its_flag(self, cfg, tmp_path):
        root = self._tree(tmp_path)
        _StubBoard.steps[7] = [
            {"step_key": "34-1-01", "retired": False,
             "surface_proof": 'grep -n "export function W.*t()" app/src/Widget.tsx (regex needle, app/src/Widget.tsx:2)'},
        ]
        assert verify_step_proofs(cfg, 7, root).drift == ()

    def test_a_non_list_steps_body_is_a_loud_stop(self, cfg, tmp_path):
        _StubBoard.steps[7] = {"steps": []}  # type: ignore[assignment]
        with pytest.raises(QaBoardError, match="expected a JSON list"):
            verify_step_proofs(cfg, 7, self._tree(tmp_path))

    def test_the_finish_stops_on_a_siblings_drift_before_authoring(self, cfg, tmp_path):
        root = self._tree(tmp_path)
        _StubBoard.steps[7] = [
            {"step_key": "34-1-01", "retired": False, "surface_proof": self._proof(
                ("return null;", "app/src/Widget.tsx", "drifted by a sibling", 2),
            )},
        ]
        log = _Log()
        with pytest.raises(QaBoardError, match="board proof drift on section 7"):
            finish_story(
                "34-36", ["app/src/Widget.tsx"], RULES, cfg, log,
                step_label="34-36 walkthrough", surface_proof="app/src/Widget.tsx:3",
                project_root=root,
            )
        assert _StubBoard.posted == []  # nothing authored, nothing posted
        assert not (cfg.authoring_dir / "epic-34-steps.json").exists()
        checks = [e for e in log.events if e[1] == "qa-board-proof-check"]
        assert len(checks) == 1 and checks[0][2]["drift"] == [
            "34-1-01: app/src/Widget.tsx:2 -> hits [3]"
        ]

    def test_the_finish_records_a_clean_check_and_proceeds(self, cfg, tmp_path):
        root = self._tree(tmp_path)
        _StubBoard.steps[7] = [
            {"step_key": "34-1-01", "retired": False, "surface_proof": self._proof(
                ("return null;", "app/src/Widget.tsx", "the render", 3),
            )},
        ]
        log = _Log()
        result = finish_story(
            "34-36", ["app/src/Widget.tsx"], RULES, cfg, log,
            step_label="34-36 walkthrough",
            surface_proof=self._proof(("return null;", "app/src/Widget.tsx", "the render", 3)),
            project_root=root,
        )
        assert result["ok"] and result["step_key"] == "34-36-01"
        names = [e[1] for e in log.events]
        assert names.index("qa-board-proof-check") < names.index("qa-board-step")
        check = next(e[2] for e in log.events if e[1] == "qa-board-proof-check")
        assert (check["anchors"], check["drift"]) == (1, [])

    def test_no_project_root_logs_the_skip_never_silently(self, cfg):
        log = _Log()
        finish_story(
            "34-36", ["app/src/Widget.tsx"], RULES, cfg, log,
            step_label="34-36 walkthrough", surface_proof="app/src/Widget.tsx:3",
        )
        skipped = [e for e in log.events if e[1] == "qa-board-proof-check-skipped"]
        assert len(skipped) == 1 and "not re-verified" in skipped[0][2]["reason"]
