from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from story_automator.commands.setup import cmd_setup


def _bmad_project(tmp_path: Path, *, name: str = "demo", node: bool = False) -> Path:
    (tmp_path / "_bmad" / "bmm").mkdir(parents=True)
    (tmp_path / "_bmad" / "bmm" / "config.yaml").write_text(
        f"user_name: Tester\nproject_name: {name}\n", encoding="utf-8"
    )
    if node:
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "d", "scripts": {"lint": "x", "test": "y", "build": "z"}}),
            encoding="utf-8",
        )
    return tmp_path


def _run(argv: list[str]) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cmd_setup(argv)
    return rc, json.loads(buf.getvalue())


def test_setup_creates_everything(tmp_path):
    root = _bmad_project(tmp_path, name="demo", node=True)
    rc, payload = _run([str(root), "--json"])
    assert rc == 0 and payload["ok"] is True
    # Review bridge copied with its key files.
    bridge = root / ".claude" / "skills" / "bmad-story-automator-review"
    assert (bridge / "SKILL.md").is_file()
    assert (bridge / "workflow.yaml").is_file()
    assert (bridge / "contract.json").is_file()
    # yaml written with detected name + node gauntlet.
    yaml = (root / "_bmad" / "automator" / "story-automator.yaml").read_text()
    assert "project_name: demo" in yaml
    assert "npm test" in yaml and "npm run build" in yaml
    assert "bridge: bmad-story-automator-review" in yaml
    # Stop hook points at the global skill script; marker gitignored.
    settings = json.loads((root / ".claude" / "settings.json").read_text())
    cmd = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert "story-automator" in cmd and "stop-hook" in cmd
    assert ".claude/.story-automator-active" in (root / ".gitignore").read_text()


def test_setup_json_output_is_clean(tmp_path):
    # The reused ensure_* helpers must not leak their own JSON into stdout.
    root = _bmad_project(tmp_path)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cmd_setup([str(root), "--json"])
    # Entire stdout must parse as a single JSON document.
    parsed = json.loads(buf.getvalue())
    assert parsed["ok"] is True


def test_setup_idempotent_skips_existing(tmp_path):
    root = _bmad_project(tmp_path)
    _run([str(root), "--json"])
    _, payload = _run([str(root), "--json"])
    statuses = {a["item"]: a["status"] for a in payload["actions"]}
    assert statuses["review-bridge"] == "skipped"
    assert statuses["story-automator.yaml"] == "skipped"


def test_setup_force_overwrites(tmp_path):
    root = _bmad_project(tmp_path)
    _run([str(root), "--json"])
    # Corrupt the yaml, then --force should regenerate it.
    yaml_path = root / "_bmad" / "automator" / "story-automator.yaml"
    yaml_path.write_text("garbage\n", encoding="utf-8")
    _, payload = _run([str(root), "--force", "--json"])
    statuses = {a["item"]: a["status"] for a in payload["actions"]}
    assert statuses["story-automator.yaml"] == "forced"
    assert "project_name:" in yaml_path.read_text()


def test_setup_empty_gauntlet_for_unknown_stack(tmp_path):
    root = _bmad_project(tmp_path, node=False)
    _run([str(root), "--json"])
    yaml = (root / "_bmad" / "automator" / "story-automator.yaml").read_text()
    assert "test_gauntlet: []" in yaml


def test_setup_rejects_non_bmad_dir(tmp_path):
    rc, payload = _run([str(tmp_path), "--json"])
    assert rc == 1 and payload["ok"] is False
    assert payload["error"] == "not_a_bmad_project"
