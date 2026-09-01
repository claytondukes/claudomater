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

from claudomater.sprint import _write_atomically
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
            if not isinstance(raw[key], str) or not raw[key].strip():
                # whitespace-only survives the truthiness check above and
                # becomes an empty value after normalization - a confusing
                # runtime failure instead of a load-time one
                raise QaBoardError(
                    f"adapters.qa_board.{key} must be a non-blank string, "
                    f"got {raw[key]!r}"
                )
        gate = raw["gate"]
        if (
            not isinstance(gate, list)
            or not gate
            or not all(isinstance(a, str) and a.strip() for a in gate)
        ):
            raise QaBoardError(
                "adapters.qa_board.gate must be a non-empty list of "
                f"non-blank argv strings, got {gate!r}"
            )
        root = Path(root)
        authoring = Path(raw["authoring_dir"])
        gate_dir = Path(raw["gate_dir"])
        return cls(
            authoring_dir=authoring if authoring.is_absolute() else root / authoring,
            # strip whitespace BEFORE the slash trim: a pasted URL with a
            # stray space otherwise fails later as a confusing "unreachable"
            board_url=raw["board_url"].strip().rstrip("/"),
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
    seen: set[str] = set()
    for i, step in enumerate(spec["steps"]):
        if not isinstance(step, dict) or not str(step.get("step_key", "")).strip():
            raise QaBoardError(f"{path}: steps[{i}] has no usable step_key")
        key = str(step["step_key"]).strip()
        if key in seen:
            # step_key uniquely identifies a step everywhere downstream
            # (the board's unique constraint, the retry reuse, coverage) -
            # a spec already carrying a duplicate is malformed, and
            # authoring on top of it would compound the corruption
            raise QaBoardError(f"{path}: duplicate step_key {key!r}")
        seen.add(key)
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
    if not re.match(rf"{re.escape(story_id)}(?!\d|-\d)", label.strip()):
        # the digit boundary is load-bearing (the coverage regex's own
        # lesson): a bare startswith('34-3') matches a '34-36 ...' label,
        # crediting a step to its sibling story
        raise QaBoardError(
            f"the step label must start with its story id {story_id!r} "
            "(digit-bounded) - coverage is matched on the label's story "
            "reference"
        )
    if not surface_proof.strip():
        raise QaBoardError(
            "a non-waived step needs a non-empty surface_proof (a file:line "
            "claim a reviewer can check) - the board's loader refuses it "
            "otherwise and the step would never reach the board"
        )
    path = spec_path(cfg, epic_id)
    spec = load_spec(path, epic_id)
    label = label.strip()
    surface_proof = surface_proof.strip()
    # IDEMPOTENT on identical content: a retry after a mid-flow crash
    # (board 500, gate failure) re-runs this whole flow, and computing
    # the next sequence number again would append a SECOND spec entry for
    # the same story. Same story + same label + same proof = the same
    # step, reused; the board POST is idempotent server-side on its key.
    for existing in spec["steps"]:
        if (
            existing.get("label") == label
            and existing.get("surface_proof") == surface_proof
        ):
            return dict(existing)
    step = {
        "step_key": next_step_key(spec, story_id),
        "label": label,
        "surface_proof": surface_proof,
    }
    if any(s.get("step_key") == step["step_key"] for s in spec["steps"]):
        raise QaBoardError(f"step_key collision: {step['step_key']}")
    # append ONLY - existing steps are other stories' records and a
    # rewrite here could clobber hand edits (the board tool's own default
    # is insert-only for the same reason)
    spec["steps"].append(step)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            # _write_atomically preserves the target's mode, so the target
            # must exist; a fresh spec starts with a normal file
            path.touch()
        # atomic replace (sprint.py's writer): the spec is a curated
        # artifact hand edits share - a truncating write interrupted by a
        # crash or a full disk would corrupt it and strand every future
        # run behind a parse error
        _write_atomically(
            path, json.dumps(spec, indent=2, ensure_ascii=False) + "\n"
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
    except ValueError as exc:
        # the board RESPONDED, with something that is not JSON - a
        # different failure from unreachable, and triaged differently
        raise QaBoardError(
            f"board response at {url} is not JSON: {exc}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise QaBoardError(
            f"board unreachable at {url}: {exc} - the finish flow does "
            "not skip an unreachable board"
        ) from exc


def section_id_for_epic(cfg: QaBoardConfig, epic_id: str) -> int:
    sections = _http_json(f"{cfg.board_url}/sections")
    if not isinstance(sections, list):
        raise QaBoardError(f"board /sections returned {type(sections).__name__}")
    for entry in sections:
        if not isinstance(entry, dict):
            # a corrupt listing must stop loudly, not AttributeError - and
            # not silently skip past an element that might BE our section
            raise QaBoardError(
                f"board /sections carries a non-object section entry "
                f"({str(entry)[:80]!r})"
            )
    matches = [s for s in sections if str(s.get("epic_id")) == str(epic_id)]
    if not matches:
        raise QaBoardError(
            f"no board section for epic {epic_id!r} - create the section "
            "first; authoring into nowhere is not a pass"
        )
    if len(matches) > 1:
        raise QaBoardError(f"multiple board sections claim epic {epic_id!r}")
    raw_id = matches[0].get("id")
    try:
        return int(raw_id)
    except (TypeError, ValueError) as exc:
        raise QaBoardError(
            f"board section for epic {epic_id!r} has no usable id "
            f"({raw_id!r}) - a malformed section is a stop, not a crash"
        ) from exc


def post_step(cfg: QaBoardConfig, section_id: int, step: dict) -> dict:
    # public function, so the invariant is enforced HERE too: every step
    # this flow posts is non-waived, and the board strands a non-waived
    # step without a proof - a caller bypassing author_step must not be
    # able to POST null
    if not str(step.get("surface_proof") or "").strip():
        raise QaBoardError(
            f"step {step.get('step_key')!r} has no surface_proof - "
            "non-waived steps must carry one before reaching the board"
        )
    posted = _http_json(
        f"{cfg.board_url}/sections/{section_id}/steps",
        {
            "step_key": step["step_key"],
            "label": step["label"],
            "surface_proof": step.get("surface_proof"),
        },
    )
    # the returned id anchors the audit trail (the run log records it);
    # a response without one either crashed untyped (non-dict) or
    # silently recorded None - refuse it instead
    try:
        posted_id = int(posted["id"])  # type: ignore[index]
    except (TypeError, KeyError, ValueError):
        raise QaBoardError(
            f"board response to the step POST has no usable 'id' "
            f"({str(posted)[:200]!r}) - cannot anchor the audit trail"
        ) from None
    posted["id"] = posted_id
    return posted


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


_AUDITED_RE = re.compile(r"Story files audited:\s*(\d+)")


def _git_out(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # same single user-facing error type as run_gate: a hung or absent
        # git must not crash the close as a raw traceback
        raise QaBoardError(f"git {' '.join(args)} failed to run in {repo}: {exc}") from exc
    if proc.returncode != 0:
        raise QaBoardError(
            f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()[:300]}"
        )
    return proc.stdout


def close_epic(
    project_root: Path | str,
    cfg: QaBoardConfig,
    epic_id: str,
    sprint_path: Path | str,
    runlog: Any,
) -> dict:
    """The epic-close gate with its ordering and count checks (epic-47
    retro F4; retirement condition 2). Three stages, each logged, each a
    loud stop on failure:

    1. PRECHECK - the artifact repo holding the authoring/coverage tree is
       fully committed AND fully pushed. The 47-4 shape (story artifacts
       still local when the lab gate regenerated the matrix) becomes
       impossible instead of invisible.
    2. THE GATE - `run_gate` (exit code only), which commits the
       regenerated matrix lab-side.
    3. COUNT - pull the artifact repo and validate the regenerated
       matrix's "Story files audited: N" against the epic's story count
       from the sprint file (positional membership, superseded excluded).
       A matrix that silently audits 3 of 4 is the silent-pass shape, so
       a mismatch FAILS - it is not a warning.
    """
    from claudomater import sprint as sprint_mod

    root = Path(project_root)
    sprint_file = Path(sprint_path)
    if not sprint_file.is_absolute():
        # anchored to the PROJECT, not the caller's cwd - a gate invoked
        # from outside the repo must still judge the repo's sprint file
        sprint_file = root / sprint_file
    artifact_repo = Path(
        _git_out(cfg.authoring_dir, "rev-parse", "--show-toplevel").strip()
    )
    dirty = _git_out(artifact_repo, "status", "--porcelain").strip()
    if dirty:
        raise QaBoardError(
            f"epic-close precheck FAILED: {artifact_repo} has uncommitted "
            f"changes - story artifacts must be committed and pushed before "
            f"the gate:\n{dirty[:500]}"
        )
    unpushed = _git_out(artifact_repo, "rev-list", "@{u}..HEAD").strip()
    if unpushed:
        raise QaBoardError(
            f"epic-close precheck FAILED: {artifact_repo} has "
            f"{len(unpushed.splitlines())} unpushed commit(s) - the lab gate "
            "audits the PUSHED tree, so local-only artifacts are invisible "
            "to it (epic-47 retro F4)"
        )
    stories = sprint_mod.epic_story_entries(sprint_file, epic_id)
    expected = len(stories)
    runlog.event(
        "close",
        "close-gate-precheck",
        {
            "epic": epic_id,
            "artifact_repo": str(artifact_repo),
            "clean": True,
            "pushed": True,
            "expected_stories": expected,
            "story_keys": [e.key for e in stories],
        },
    )
    # Write-ahead: intent BEFORE the action, and no outcome claim - the
    # count stage completing is what implies the gate passed.
    runlog.event("close", "close-gate", {"epic": epic_id})
    run_gate(cfg, epic_id)
    # The gate commits the regenerated matrix lab-side; read it back
    # through git, not trust.
    _git_out(artifact_repo, "pull", "--rebase", "-q")
    matrix = cfg.authoring_dir.parent / "coverage" / f"epic-{epic_id}-coverage.md"
    try:
        matrix_text = matrix.read_text(encoding="utf-8")
    except OSError as exc:
        raise QaBoardError(
            f"epic-close count check FAILED: cannot read the regenerated "
            f"matrix {matrix}: {exc}"
        ) from exc
    m = _AUDITED_RE.search(matrix_text)
    if m is None:
        raise QaBoardError(
            f"epic-close count check FAILED: {matrix} carries no 'Story "
            "files audited: N' line - a count that cannot be read must "
            "not read as matching"
        )
    audited = int(m.group(1))
    runlog.event(
        "close",
        "close-gate-count",
        {"epic": epic_id, "audited": audited, "expected": expected,
         "ok": audited == expected},
    )
    if audited != expected:
        raise QaBoardError(
            f"epic-close count check FAILED for epic {epic_id}: the "
            f"regenerated matrix audited {audited} story file(s), the "
            f"sprint file carries {expected} non-superseded stories - a "
            "matrix missing part of the epic must never pass its close"
        )
    return {"epic": epic_id, "gate": "PASS", "audited": audited, "expected": expected}


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
    # normalize ONCE here so the intent event, the spec, and the board all
    # carry the same bytes - logging raw values while author_step stripped
    # them let the audit trail disagree with what was persisted
    step_label = (step_label or "").strip()
    surface_proof = (surface_proof or "").strip()
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
