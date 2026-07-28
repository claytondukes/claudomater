from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .common import read_text, trim_lines


def parse_epic_file(epic_file: str | Path, epic_num: str = "") -> dict[str, Any]:
    """Parse an epics file into stories.

    Without `epic_num`, `epicTitle` is the document H1 and `stories`/`count`
    span every epic in the file (back-compat). With `epic_num`, the result is
    scoped to that epic only: `epicTitle` comes from its `## Epic N:` heading
    (H1 fallback for single-epic files without one) and `stories`/`count`
    include only that epic's stories. Multi-epic files MUST be scoped —
    Story 1.1 postmortem (2026-07-14): the unscoped parse fed the document
    title and the all-epics story count into the orchestration state.
    """
    epic_num = str(epic_num).strip()
    content = read_text(epic_file)
    lines = trim_lines(content)
    epic_title = ""
    for line in lines:
        if line.startswith("# "):
            epic_title = line.removeprefix("# ").strip()
            break
    story_re = re.compile(r"^###\s+Story\s+(\d+)([.\-])(\d+)\s*(?::|—|-)\s*(.*)$")
    epic_re = re.compile(r"^##\s+Epic\s+(\d+):\s*(.*)$")
    current_epic_title = ""
    selected_epic_title = ""
    stories: list[dict[str, str]] = []
    for line in lines:
        epic_match = epic_re.match(line)
        if epic_match:
            current_epic_title = epic_match.group(2).strip()
            if epic_num and epic_match.group(1) == epic_num:
                selected_epic_title = current_epic_title
            continue
        story_match = story_re.match(line)
        if story_match:
            story_epic_num, sep, story_num, title = story_match.groups()
            if epic_num and story_epic_num != epic_num:
                continue
            story_id = f"{story_epic_num}{sep}{story_num}"
            stories.append(
                {
                    "epicNum": story_epic_num,
                    "epicTitle": current_epic_title,
                    "storyNum": story_num,
                    "storyId": story_id,
                    "title": title.strip(),
                }
            )
    payload: dict[str, Any] = {"ok": True, "epicTitle": epic_title, "stories": stories, "count": len(stories), "file": str(epic_file)}
    if epic_num:
        payload["epicNum"] = epic_num
        payload["epicTitle"] = selected_epic_title or epic_title
    return payload


def parse_story(epic_file: str | Path, story_id: str, rules_file: str | Path) -> dict[str, Any]:
    content = read_text(epic_file)
    lines = trim_lines(content)
    header_re = re.compile(rf"^###\s+Story\s+{re.escape(story_id)}\s*(?::|—|-)\s*(.*)$")
    start_index = -1
    title = ""
    for index, line in enumerate(lines):
        match = header_re.match(line)
        if match:
            start_index = index
            title = match.group(1).strip()
            break
    if start_index < 0:
        raise ValueError("story_not_found")
    description_lines: list[str] = []
    acceptance_criteria: list[str] = []
    dependencies = ""
    in_ac = False
    for line in lines[start_index + 1 :]:
        if line.startswith("### Story ") or line.startswith("## Epic "):
            break
        if "Acceptance Criteria" in line:
            in_ac = True
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if "Dependencies:" in line or "**Dependencies**:" in line:
            dep = line.replace("**Dependencies**:", "").replace("Dependencies:", "").strip()
            if not dependencies:
                dependencies = dep
        if in_ac:
            acceptance_criteria.append(stripped)
        else:
            description_lines.append(stripped)
    description = " ".join(" ".join(description_lines).split())
    rules = json.loads(read_text(rules_file))
    content_for_score = " ".join(part for part in [title, description, " ".join(acceptance_criteria)] if part).strip()
    score = 0
    reasons: list[str] = []
    for rule in rules.get("rules", []):
        pattern = rule.get("pattern", "")
        if pattern and re.search(pattern, content_for_score, re.IGNORECASE):
            score += int(rule.get("score", 0))
            reasons.append(str(rule.get("label", "")))
    structural = rules.get("structural_rules", {})
    ac_count = len(acceptance_criteria)
    if structural.get("ac_count_high", 0) and ac_count > int(structural["ac_count_high"]):
        score += int(structural.get("ac_count_high_score", 0))
        reasons.append(f"High AC count ({ac_count})")
    elif structural.get("ac_count_medium", 0) and ac_count > int(structural["ac_count_medium"]):
        score += int(structural.get("ac_count_medium_score", 0))
        reasons.append(f"Elevated AC count ({ac_count})")
    if structural.get("dependency_score", 0) and dependencies and dependencies.lower() != "none":
        score += int(structural.get("dependency_score", 0))
        reasons.append("Has explicit dependencies")
    word_threshold = int(structural.get("large_story_word_threshold", 0))
    if word_threshold:
        word_count = len(content_for_score.split())
        if word_count > word_threshold:
            score += int(structural.get("large_story_score", 0))
            reasons.append(f"Large story ({word_count} words)")
    low_max = int(rules.get("thresholds", {}).get("low_max", 0))
    medium_max = int(rules.get("thresholds", {}).get("medium_max", low_max))
    level = "High"
    if score <= low_max:
        level = "Low"
    elif score <= medium_max:
        level = "Medium"
    return {
        "ok": True,
        "storyId": story_id,
        "title": title,
        "description": description,
        "acceptanceCriteria": acceptance_criteria,
        "dependencies": dependencies,
        "complexity": {"score": score, "level": level, "reasons": reasons},
    }


def parse_story_range(user_input: str, total: int, ids_csv: str = "") -> dict[str, Any]:
    if not user_input or total <= 0:
        raise ValueError("missing_input_or_total")
    ids = [part.strip() for part in ids_csv.split(",")] if ids_csv else []
    selected: set[int] = set()
    normalized = user_input.lower().replace(" ", "")
    if normalized == "all":
        selected = set(range(1, total + 1))
    else:
        for part in normalized.split(","):
            if not part:
                continue
            if "-" in part:
                start_raw, end_raw = part.split("-", 1)
                if start_raw.isdigit() and end_raw.isdigit():
                    start = int(start_raw)
                    end = int(end_raw)
                    low, high = sorted((start, end))
                    selected.update(range(low, high + 1))
            elif part.isdigit():
                selected.add(int(part))
    indices = sorted(index for index in selected if 1 <= index <= total)
    story_ids = [ids[index - 1] for index in indices if index - 1 < len(ids)]
    return {"ok": True, "indices": indices, "storyIds": story_ids, "count": len(indices)}


def epic_complete(epic_file: str | Path, range_csv: str, epic_num: str = "") -> dict[str, Any]:
    """Whether the selected range reaches the last story of its epic.

    Scoped to a single epic: `epic_num` if given, else the epic of the
    range's highest story. Comparing the range against the max story of the
    *whole file* is wrong for multi-epic files — Story 1.1 postmortem
    (2026-07-14): epic 1's final story would compare against epic 9's and
    never report complete. Unscoped whole-file behavior is kept only for the
    empty-range/no-epic case (back-compat).
    """
    epic_num = str(epic_num).strip()

    def _key(value: str) -> tuple[int, int]:
        return tuple(int(part) for part in re.split(r"[.\-]", value, maxsplit=1))

    selected = [part.strip() for part in range_csv.split(",") if part.strip()]
    max_range_story = max(selected, key=_key) if selected else "0.0"
    if not epic_num and selected:
        epic_num = re.split(r"[.\-]", max_range_story, maxsplit=1)[0]
    story_ids = [story["storyId"] for story in parse_epic_file(epic_file, epic_num)["stories"]]
    if not story_ids:
        raise ValueError("no_stories_found")
    max_epic_story = max(story_ids, key=_key)
    complete = bool(selected) and _key(max_range_story) == _key(max_epic_story)
    payload: dict[str, Any] = {"ok": True, "epicComplete": complete, "maxEpicStory": max_epic_story}
    if epic_num:
        payload["epicNum"] = epic_num
    return payload
