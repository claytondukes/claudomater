from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from story_automator.commands.doctor import cmd_doctor
from story_automator.core.runtime_policy import resolve_skill_name


# --- resolve_skill_name (the capability-adapter seam) ----------------------

def _skill(root: Path, name: str) -> None:
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")


def test_resolve_prefers_first_present_candidate(tmp_path):
    _skill(tmp_path, "bmad-dev-auto")
    assets = {"skillName": "bmad-dev-story", "skillCandidates": ["bmad-dev-story", "bmad-dev-auto"]}
    chosen, candidates = resolve_skill_name(assets, tmp_path)
    assert chosen == "bmad-dev-auto"
    assert candidates == ["bmad-dev-story", "bmad-dev-auto"]


def test_resolve_falls_back_to_skillname(tmp_path):
    # No candidates present; skillName appended as final fallback for messaging.
    assets = {"skillName": "bmad-dev-story", "skillCandidates": ["ghost-a", "ghost-b"]}
    chosen, candidates = resolve_skill_name(assets, tmp_path)
    assert chosen == "bmad-dev-story"
    assert candidates[-1] == "bmad-dev-story"


def test_resolve_legacy_skillname_only(tmp_path):
    _skill(tmp_path, "bmad-create-story")
    chosen, _ = resolve_skill_name({"skillName": "bmad-create-story"}, tmp_path)
    assert chosen == "bmad-create-story"


# --- doctor ----------------------------------------------------------------

def _project(tmp_path: Path, skills: list[str], *, paths: bool = True) -> Path:
    (tmp_path / "_bmad" / "bmm").mkdir(parents=True)
    (tmp_path / "_bmad" / "bmm" / "config.yaml").write_text(
        "project_name: demo\n# Version: 6.11.0\n", encoding="utf-8"
    )
    for s in skills:
        _skill(tmp_path / ".claude" / "skills", s)
    # Review bridge needs its full asset set (workflow + contract) to resolve.
    bridge = tmp_path / ".claude" / "skills" / "bmad-story-automator-review"
    if bridge.is_dir():
        (bridge / "workflow.yaml").write_text("standalone: true\n", encoding="utf-8")
        (bridge / "contract.json").write_text("{}\n", encoding="utf-8")
    if paths:
        for rel in (
            "_bmad-output/implementation-artifacts",
            "_bmad-output/planning-artifacts",
        ):
            (tmp_path / rel).mkdir(parents=True, exist_ok=True)
        (tmp_path / "_bmad-output/implementation-artifacts/sprint-status.yaml").write_text("", encoding="utf-8")
    return tmp_path


_ALL = [
    "bmad-create-story",
    "bmad-dev-story",
    "bmad-qa-generate-e2e-tests",
    "bmad-retrospective",
    "bmad-story-automator-review",
]


def _run(argv: list[str]) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cmd_doctor(argv)
    return rc, json.loads(buf.getvalue())


def test_doctor_all_green(tmp_path):
    root = _project(tmp_path, _ALL)
    rc, payload = _run([str(root), "--json"])
    assert rc == 0 and payload["ok"] is True
    caps = {c["name"]: c for c in payload["checks"] if c["kind"] == "capability"}
    assert caps["dev-story"]["skill"] == "bmad-dev-story"
    assert all(c["ok"] for c in payload["checks"])
    assert payload["bmadVersion"].startswith("6.11")


def test_doctor_flags_missing_capability_with_hint(tmp_path):
    # dev-story renamed to bmad-dev-auto (shares the 'dev' token).
    skills = [s for s in _ALL if s != "bmad-dev-story"] + ["bmad-dev-auto"]
    root = _project(tmp_path, skills)
    rc, payload = _run([str(root), "--json"])
    assert rc == 1 and payload["ok"] is False
    dev = next(c for c in payload["checks"] if c["name"] == "dev-story")
    assert dev["ok"] is False
    # The rename target is unclaimed and shares a token → suggested.
    assert "bmad-dev-auto" in dev["suggestions"]
    # A claimed skill (create-story) must NOT be suggested.
    assert "bmad-create-story" not in dev["suggestions"]


def test_doctor_no_shared_token_gives_no_false_hint(tmp_path):
    # Renamed to bmad-loop — shares no token; suggestions should be empty.
    skills = [s for s in _ALL if s != "bmad-dev-story"] + ["bmad-loop"]
    root = _project(tmp_path, skills)
    _, payload = _run([str(root), "--json"])
    dev = next(c for c in payload["checks"] if c["name"] == "dev-story")
    assert dev["suggestions"] == []


def test_doctor_flags_missing_paths(tmp_path):
    root = _project(tmp_path, _ALL, paths=False)
    _, payload = _run([str(root), "--json"])
    path_checks = {c["name"]: c["ok"] for c in payload["checks"] if c["kind"] == "path"}
    assert path_checks["sprintStatus"] is False
    assert payload["ok"] is False


def test_doctor_degrades_when_bridge_incomplete(tmp_path):
    # Bridge dir exists but is missing contract.json → the full policy loader
    # would throw; doctor must still enumerate capabilities via the fallback.
    root = _project(tmp_path, _ALL)
    (root / ".claude/skills/bmad-story-automator-review/contract.json").unlink()
    rc, payload = _run([str(root), "--json"])
    # Must not crash; returns a structured report.
    assert "checks" in payload
    caps = {c["name"] for c in payload["checks"] if c["kind"] == "capability"}
    assert "dev-story" in caps


def test_doctor_rejects_non_bmad_dir(tmp_path):
    rc, payload = _run([str(tmp_path), "--json"])
    assert rc == 1 and payload["error"] == "not_a_bmad_project"
