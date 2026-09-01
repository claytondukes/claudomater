"""Run-metrics store: one structured JSONL row per finished story,
committed to the artifact repo beside the story's other artifacts
(Clay, epic-47 close follow-up).

The finish flow already computes the per-story report row at flip time;
this module persists it so per-epic tables and cross-epic trends render
from data instead of being reassembled from run logs after the fact.

The row is DRIVER facts (PR, merge sha, converge ledger, wall, cost,
parks, bypass) merged with FINISH-FLOW outcome (surface verdict, the
step-and-gate or waiver result). Appends are idempotent by story_id:
retrying a crashed finish with the identical row converges; a DIFFERENT
row for an already-recorded story is a loud stop, never a silent second
line (the author_step discipline)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# Driver-supplied facts, all REQUIRED - a metrics row with holes reads
# exactly like a complete one in a trend, so holes are refused at write.
DRIVER_FIELDS = (
    "pr",
    "merge_sha",
    "converge_rounds",
    "threads_fixed",
    "threads_dismissed",
    "suppressed_fixed",
    "suppressed_dismissed",
    "wall_minutes",
    "cost_usd",
    "parks",
    "merge_bypass",
)


class MetricsError(Exception):
    pass


def compose_row(
    story_id: str, epic_id: str, finish_result: Mapping[str, Any],
    driver_facts: Mapping[str, Any],
) -> dict[str, Any]:
    missing = [k for k in DRIVER_FIELDS if k not in driver_facts]
    if missing:
        raise MetricsError(
            f"metrics row for {story_id} is missing driver fact(s): "
            f"{', '.join(missing)} - a row with holes must not be written"
        )
    unknown = set(driver_facts) - set(DRIVER_FIELDS)
    if unknown:
        raise MetricsError(
            f"metrics row for {story_id} has unknown driver fact(s): "
            f"{sorted(unknown)}"
        )
    if finish_result.get("surface_touching"):
        step_key = finish_result.get("step_key")
        section_id = finish_result.get("section_id")
        if not (isinstance(step_key, str) and step_key.strip()) or section_id is None:
            # a step+gate row full of None reads as complete downstream
            raise MetricsError(
                f"metrics row for {story_id}: a surface outcome needs its "
                f"step_key and section_id, got step_key={step_key!r}, "
                f"section_id={section_id!r}"
            )
        outcome = {
            "kind": "step+gate",
            "step_key": step_key,
            "section_id": section_id,
        }
    else:
        outcome = {"kind": "waiver"}
    return {
        "story_id": story_id,
        "epic": epic_id,
        **{k: driver_facts[k] for k in DRIVER_FIELDS},
        "outcome": outcome,
    }


# every key a compose_row-written row carries; only compose_row writes
# this file, so a deviation at load means something else did
_ROW_KEYS = frozenset(("story_id", "epic", "outcome", *DRIVER_FIELDS))


def _validate_row(row: Any, where: str) -> None:
    """Full row schema at LOAD: a row missing a field crashed the
    renderers with a raw KeyError instead of the CLI's error: contract."""
    if not isinstance(row, dict) or not row.get("story_id"):
        raise MetricsError(f"{where} is not a metrics row (no story_id)")
    missing = sorted(_ROW_KEYS - set(row))
    if missing:
        raise MetricsError(f"{where} is missing field(s): {', '.join(missing)}")
    unknown = sorted(set(row) - _ROW_KEYS)
    if unknown:
        raise MetricsError(f"{where} has unknown field(s): {', '.join(unknown)}")
    outcome = row["outcome"]
    if not isinstance(outcome, dict) or outcome.get("kind") not in (
        "step+gate",
        "waiver",
    ):
        raise MetricsError(f"{where} has a malformed outcome: {outcome!r}")


def load_rows(path: Path | str) -> list[dict[str, Any]]:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise MetricsError(f"cannot read {p}: {exc}") from exc
    rows: list[dict[str, Any]] = []
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            # a malformed line poisons every aggregate silently - stop
            raise MetricsError(f"{p}:{i} is not valid JSON: {exc}") from exc
        _validate_row(row, f"{p}:{i}")
        rows.append(row)
    return rows


def append_row(path: Path | str, row: Mapping[str, Any]) -> bool:
    """Append one row; idempotent on story_id. Returns True if written,
    False when the identical row already exists. A DIFFERENT row for the
    same story refuses - two conflicting records of one story is worse
    than one wrong one, because nothing downstream can tell which lies."""
    p = Path(path)
    existing = load_rows(p)
    canon = json.dumps(dict(row), sort_keys=True)
    for prior in existing:
        if prior.get("story_id") == row.get("story_id"):
            if json.dumps(prior, sort_keys=True) == canon:
                return False
            raise MetricsError(
                f"{p} already carries a DIFFERENT row for "
                f"{row.get('story_id')!r} - refusing a conflicting second "
                "record; correct the existing row deliberately instead"
            )
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(dict(row), sort_keys=True) + "\n")
    return True


def epic_rows(rows: Iterable[Mapping[str, Any]], epic_id: str) -> list[dict]:
    out = [dict(r) for r in rows if r.get("epic") == epic_id]
    if not out:
        raise MetricsError(f"no metrics rows for epic {epic_id!r}")
    return out


def render_epic_table(rows: Sequence[Mapping[str, Any]]) -> str:
    """A fixed-column table of every row's cell VALUES - the acceptance
    is value fidelity, not layout."""
    header = (
        "story | pr | merge_sha | rounds | fixed(thr/sup) | dismissed(thr/sup)"
        " | wall_min | cost_usd | parks | outcome | bypass"
    )
    lines = [header, "-" * len(header)]
    for r in sorted(rows, key=lambda x: x["story_id"]):
        o = r.get("outcome") or {}
        outcome = (
            f"step {o.get('step_key')}+gate" if o.get("kind") == "step+gate"
            else "waiver"
        )
        lines.append(
            f"{r['story_id']} | #{r['pr']} | {str(r['merge_sha'])[:8]} | "
            f"{r['converge_rounds']} | "
            f"{r['threads_fixed']}/{r['suppressed_fixed']} | "
            f"{r['threads_dismissed']}/{r['suppressed_dismissed']} | "
            f"{r['wall_minutes']} | {r['cost_usd']:.2f} | {r['parks']} | "
            f"{outcome} | {'yes' if r['merge_bypass'] else 'no'}"
        )
    total_cost = sum(r["cost_usd"] for r in rows)
    lines.append(
        f"TOTAL: {len(rows)} stories, {sum(r['wall_minutes'] for r in rows)} "
        f"wall_min, ${total_cost:.2f}"
    )
    return "\n".join(lines)


def render_trends(rows: Sequence[Mapping[str, Any]]) -> str:
    """Cross-epic trends: rounds-to-converge, cost per story, dismissal
    rate - per epic, in epic order."""
    if not rows:
        raise MetricsError("no metrics rows to render trends from")
    by_epic: dict[str, list[Mapping[str, Any]]] = {}
    for r in rows:
        by_epic.setdefault(str(r["epic"]), []).append(r)
    lines = ["epic | stories | avg_rounds | cost/story | dismissal_rate"]
    for epic in sorted(by_epic, key=lambda e: [int(x) for x in e.split("-")]):
        er = by_epic[epic]
        n = len(er)
        avg_rounds = sum(r["converge_rounds"] for r in er) / n
        cost = sum(r["cost_usd"] for r in er) / n
        found = sum(
            r["threads_fixed"] + r["threads_dismissed"]
            + r["suppressed_fixed"] + r["suppressed_dismissed"]
            for r in er
        )
        dismissed = sum(
            r["threads_dismissed"] + r["suppressed_dismissed"] for r in er
        )
        rate = f"{dismissed}/{found}" if found else "0/0"
        lines.append(
            f"{epic} | {n} | {avg_rounds:.1f} | ${cost:.2f} | {rate}"
        )
    return "\n".join(lines)
