"""QA-board finish flow: after a story's PR merges, before its done-flip.
(Phase 3 deliverable 3, part 2 - the gap that let a surface story ship
with no walkthrough step, no matrix regen, and no gate re-run, all fixed
by hand after the fact.)

The flow answers ONE question and acts on it, fail-closed at every step:

  classify the MERGED changeset (claudomater.surface, rules from the
  project's committed config), then
  - surface: author the walkthrough step (insert-only append to the
    epic's authoring spec AND a POST to the live board), regenerate the
    coverage matrix and run the epic gate in one shot, and only report ok
    when the gate command exits 0;
  - no surface: record the waiver EVALUATION - verdict buckets and all -
    in the run log, so "no step needed" is a decision with evidence, not
    a silence.

The caller (a run driver, the merge phase) flips the story done only on
an ok result. Board unreachable, spec malformed, duplicate step_key,
gate nonzero: each is a loud error, never a skip - a silent pass here is
the exact failure mode this module exists to remove.

Adapter config (`.omater.yaml` `adapters.qa_board`, a mapping - the slot
also accepts null for "no board"):

    adapters:
      qa_board:
        authoring_dir: _bmad-output/qa-viewer/authoring
        board_url: http://localhost:8090/api
        gate_dir: _bmad-output/qa-viewer/server
        gate: [./.venv/bin/python, gate_check.py, "{epic}", --db, /data/qa.db, --write-coverage]

`{epic}` in the gate argv is substituted with the epic id. The gate is
judged by EXIT CODE, never by parsing its output text. The board URL is
plain HTTP on a loopback/trusted bind (the board's documented security
boundary is its bind address; it has no auth by design), and the POST is
idempotent server-side on UNIQUE(section_id, step_key).
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claudomater.surface import SurfaceRules, classify_changed_files

_STORY_ID_RE = re.compile(r"^\d+(?:-\d+)+$")


class QaBoardError(Exception):
    """The finish flow cannot proceed honestly. Never swallowed."""


@dataclass(frozen=True)
class QaBoardConfig:
    authoring_dir: Path
    board_url: str
    gate_dir: Path
    gate: tuple[str, ...]

    @classmethod
    def from_adapter(cls, raw: Any, root: Path) -> "QaBoardConfig | None":
        """The adapters.qa_board value -> config; None when the slot is
        null (no board wired). Garbage fails loudly - a half-declared
        adapter must not read as 'no board'."""
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise QaBoardError(
                f"adapters.qa_board must be a mapping (or null), got {raw!r}"
            )
        missing = [
            k for k in ("authoring_dir", "board_url", "gate_dir", "gate") if not raw.get(k)
        ]
        if missing:
            raise QaBoardError(
                f"adapters.qa_board is missing {', '.join(missing)} - a "
                "half-declared board adapter must not read as 'no board'"
            )
        # every field TYPED here: a non-string path would escape as a raw
        # TypeError from Path(), past load_project_config's typed catch
        for key in ("authoring_dir", "board_url", "gate_dir"):
            if not isinstance(raw[key], str):
                raise QaBoardError(
                    f"adapters.qa_board.{key} must be a string, got {raw[key]!r}"
                )
        gate = raw["gate"]
        if (
            not isinstance(gate, list)
            or not gate
            or not all(isinstance(a, str) and a for a in gate)
        ):
            raise QaBoardError(
                "adapters.qa_board.gate must be a non-empty list of "
                f"non-empty argv strings, got {gate!r}"
            )
        root = Path(root)
        authoring = Path(raw["authoring_dir"])
        gate_dir = Path(raw["gate_dir"])
        return cls(
            authoring_dir=authoring if authoring.is_absolute() else root / authoring,
            board_url=str(raw["board_url"]).rstrip("/"),
            gate_dir=gate_dir if gate_dir.is_absolute() else root / gate_dir,
            gate=tuple(gate),
        )


def epic_of(story_id: str) -> str:
    """'43-2' -> '43'; compound epic ids ('4-5-1' -> '4-5') supported."""
    if not _STORY_ID_RE.match(story_id):
        raise QaBoardError(f"malformed story id: {story_id!r}")
    return story_id.rsplit("-", 1)[0]


def spec_path(cfg: QaBoardConfig, epic_id: str) -> Path:
    return cfg.authoring_dir / f"epic-{epic_id}-steps.json"


def load_spec(path: Path, epic_id: str) -> dict:
    """The epic's authoring spec, or a fresh skeleton when absent (the
    first surface story of a new epic has nothing to append to). A spec
    that exists but cannot be parsed raises: treating it as 'no steps'
    would author a duplicate story step into a file a human broke."""
    if not path.exists():
        return {"epic_id": epic_id, "title": f"Epic {epic_id}", "steps": []}
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise QaBoardError(f"{path} exists but cannot be read: {exc}") from exc
    if not isinstance(spec, dict) or not isinstance(spec.get("steps"), list):
        raise QaBoardError(
            f"{path} is not an authoring spec (need an object with a 'steps' list)"
        )
    for i, step in enumerate(spec["steps"]):
        if not isinstance(step, dict) or not str(step.get("step_key", "")).strip():
            raise QaBoardError(f"{path}: steps[{i}] has no usable step_key")
    return spec


def next_step_key(spec: dict, story_id: str) -> str:
    """`{story}-NN`, one past the story's highest existing sequence -
    the convention every hand-authored spec already follows."""
    prefix = f"{story_id}-"
    highest = 0
    for step in spec["steps"]:
        key = str(step["step_key"])
        if key.startswith(prefix) and key[len(prefix):].isdigit():
            highest = max(highest, int(key[len(prefix):]))
    return f"{story_id}-{highest + 1:02d}"


def author_step(
    cfg: QaBoardConfig, epic_id: str, story_id: str, label: str, surface_proof: str
) -> dict:
    """Insert-only append to the epic's authoring spec. Returns the step.

    The label must reference its story (the coverage convention: steps
    reference stories by leading id in the LABEL), and the surface_proof
    must be non-empty - the board's own loader refuses non-waived steps
    without one, so authoring such a step here would strand it."""
    if not label.strip():
        raise QaBoardError("a walkthrough step needs a non-empty label")
    if not label.strip().startswith(story_id):
        raise QaBoardError(
            f"the step label must start with its story id {story_id!r} - "
            "coverage is matched on the label's story reference"
        )
    if not surface_proof.strip():
        raise QaBoardError(
            "a non-waived step needs a non-empty surface_proof (a file:line "
            "claim a reviewer can check) - the board's loader refuses it "
            "otherwise and the step would never reach the board"
        )
    path = spec_path(cfg, epic_id)
    spec = load_spec(path, epic_id)
    step = {
        "step_key": next_step_key(spec, story_id),
        "label": label.strip(),
        "surface_proof": surface_proof.strip(),
    }
    if any(s.get("step_key") == step["step_key"] for s in spec["steps"]):
        raise QaBoardError(f"step_key collision: {step['step_key']}")
    # append ONLY - existing steps are other stories' records and a
    # rewrite here could clobber hand edits (the board tool's own default
    # is insert-only for the same reason)
    spec["steps"].append(step)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        raise QaBoardError(f"cannot write {path}: {exc}") from exc
    return step


def _http_json(url: str, payload: dict | None = None, timeout: float = 10.0) -> Any:
    req = urllib.request.Request(
        url,
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise QaBoardError(f"board {exc.code} on {url}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise QaBoardError(
            f"board unreachable or unreadable at {url}: {exc} - the finish "
            "flow does not skip an unreachable board"
        ) from exc


def section_id_for_epic(cfg: QaBoardConfig, epic_id: str) -> int:
    sections = _http_json(f"{cfg.board_url}/sections")
    if not isinstance(sections, list):
        raise QaBoardError(f"board /sections returned {type(sections).__name__}")
    matches = [s for s in sections if str(s.get("epic_id")) == str(epic_id)]
    if not matches:
        raise QaBoardError(
            f"no board section for epic {epic_id!r} - create the section "
            "first; authoring into nowhere is not a pass"
        )
    if len(matches) > 1:
        raise QaBoardError(f"multiple board sections claim epic {epic_id!r}")
    return int(matches[0]["id"])


def post_step(cfg: QaBoardConfig, section_id: int, step: dict) -> dict:
    return _http_json(
        f"{cfg.board_url}/sections/{section_id}/steps",
        {
            "step_key": step["step_key"],
            "label": step["label"],
            "surface_proof": step.get("surface_proof"),
        },
    )


def run_gate(cfg: QaBoardConfig, epic_id: str) -> None:
    """The epic gate, judged by EXIT CODE only. Output rides into the
    error on failure so the operator sees the gate's own verdict text."""
    argv = [a.replace("{epic}", str(epic_id)) for a in cfg.gate]
    try:
        proc = subprocess.run(
            argv, cwd=cfg.gate_dir, capture_output=True, text=True, timeout=300
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QaBoardError(f"gate command failed to run ({argv}): {exc}") from exc
    if proc.returncode != 0:
        raise QaBoardError(
            f"epic gate FAILED (exit {proc.returncode}) for epic {epic_id}:\n"
            f"{(proc.stdout + proc.stderr).strip()[-2000:]}"
        )


def finish_story(
    story_id: str,
    merged_files: list[str],
    rules: SurfaceRules,
    cfg: QaBoardConfig,
    runlog: Any,
    step_label: str | None = None,
    surface_proof: str | None = None,
) -> dict:
    """The full flow. Returns a JSON-able result; raises rather than
    guessing. Events are written BEFORE each action (run-log discipline).

    For a surface verdict the caller must supply `step_label` and
    `surface_proof`: the walkthrough instruction is authored content the
    flow cannot invent, and arriving here without them means the merge
    phase never wrote them - a loud stop, not a waiver."""
    epic_id = epic_of(story_id)
    verdict = classify_changed_files(merged_files, rules)
    if not verdict.surface_touching:
        # the waiver EVALUATION is the artifact: buckets and all, so "no
        # step needed" is a recorded decision, not a silence
        runlog.event(
            "merge",
            "qa-board-waiver",
            {"story": story_id, "epic": epic_id, **verdict.as_dict()},
            story_key=story_id,
        )
        return {"ok": True, "step_required": False, **verdict.as_dict()}
    if not step_label or not surface_proof:
        raise QaBoardError(
            f"story {story_id} touched {len(verdict.surface)} surface "
            "path(s) but no walkthrough step content was supplied - the "
            "merge phase must author step_label + surface_proof for a "
            "surface story"
        )
    # run-log discipline: an INTENT event lands before each action (the
    # log's own contract) and a completion event after it, so a crash
    # between the two shows exactly what was being attempted - content
    # included, because a retry needs the label/proof that were in flight
    runlog.event(
        "merge",
        "qa-board-step",
        {
            "story": story_id,
            "epic": epic_id,
            "surface": verdict.surface,
            "step_label": step_label,
            "surface_proof": surface_proof,
        },
        story_key=story_id,
    )
    step = author_step(cfg, epic_id, story_id, step_label, surface_proof)
    section_id = section_id_for_epic(cfg, epic_id)
    runlog.event(
        "merge",
        "qa-board-post",
        {"story": story_id, "step_key": step["step_key"], "section_id": section_id},
        story_key=story_id,
    )
    posted = post_step(cfg, section_id, step)
    runlog.event(
        "merge",
        "qa-board-posted",
        {
            "story": story_id,
            "step_key": step["step_key"],
            "section_id": section_id,
            "board_step_id": posted.get("id"),
        },
        story_key=story_id,
    )
    runlog.event(
        "merge",
        "qa-board-gate",
        {"story": story_id, "epic": epic_id},
        story_key=story_id,
    )
    run_gate(cfg, epic_id)
    runlog.event(
        "merge",
        "qa-board-gate-pass",
        {"story": story_id, "epic": epic_id},
        story_key=story_id,
    )
    return {
        "ok": True,
        "step_required": True,
        "step_key": step["step_key"],
        "section_id": section_id,
        **verdict.as_dict(),
    }
