"""`story-automator doctor` — verify a project's BMAD wiring before a run.

BMAD churns: releases rename or replace skills and occasionally move paths. This
command resolves every capability the automator needs against the *current*
project and reports precisely what's missing, so a broken upgrade surfaces as a
5-second actionable diagnostic instead of a cryptic mid-run PolicyError.

It shares the policy's capability-resolution seam (`resolve_skill_name`) and the
declared `bmadPaths`, so what doctor checks is exactly what the orchestrator
resolves — no drift. Exit 0 when healthy, 1 otherwise; `--json` for machines.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..core.project_config import reviewer_bridge
from ..core.runtime_policy import (
    PolicyError,
    bmad_paths,
    bundled_skill_root,
    load_effective_policy,
    resolve_skill_name,
)
from ..core.utils import read_text, run_cmd

# Step -> human capability label for the report.
_CAPABILITY = {
    "create": "create-story",
    "dev": "dev-story",
    "auto": "generate-e2e-tests",
    "review": "senior-review",
    "retro": "retrospective",
}


def _resolve_project_root(args: list[str]) -> Path:
    for idx, arg in enumerate(args):
        if arg in ("--project-root", "-C") and idx + 1 < len(args):
            return Path(args[idx + 1]).expanduser().resolve()
    positional = [a for a in args if not a.startswith("-")]
    if positional:
        return Path(positional[0]).expanduser().resolve()
    result = run_cmd("git", "-C", str(Path.cwd()), "rev-parse", "--show-toplevel")
    if result.exit_code == 0 and result.output.strip():
        return Path(result.output.strip()).resolve()
    return Path.cwd().resolve()


def _bmad_version(root: Path) -> str:
    for rel in ("_bmad/bmm/config.yaml", "_bmad/automator/config.yaml", "_bmad/core/config.yaml"):
        path = root / rel
        if not path.is_file():
            continue
        try:
            for line in read_text(path).splitlines():
                low = line.lower()
                if "version" in low and ":" in line:
                    return line.split(":", 1)[1].strip()
        except OSError:
            continue
    return ""


def _installed_skills(skills_root: Path) -> list[str]:
    if not skills_root.is_dir():
        return []
    return sorted(p.name for p in skills_root.iterdir() if (p / "SKILL.md").is_file())


def _rename_hints(candidates: list[str], unclaimed: list[str]) -> list[str]:
    """Best-effort rename suggestions from the pool of installed-but-unclaimed
    skills, ranked by token overlap with the expected candidates. Empty when
    nothing shares a token — a silent-but-honest "we can't guess this rename"."""
    tokens = {t for cand in candidates for t in cand.replace("bmad-", "").split("-") if len(t) > 2}
    scored = []
    for name in unclaimed:
        overlap = sum(1 for tok in tokens if tok in name)
        if overlap:
            scored.append((overlap, name))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [name for _score, name in scored[:5]]


def _check_capabilities(root: Path, policy: dict) -> list[dict]:
    skills_root = root / ".claude" / "skills"
    steps = policy.get("steps") or {}
    # First pass: resolve every capability so we know which skills are "claimed".
    resolved: dict[str, tuple[str, str, list[str], bool]] = {}
    for step, capability in _CAPABILITY.items():
        assets = (steps.get(step) or {}).get("assets") or {}
        chosen, candidates = resolve_skill_name(assets, skills_root)
        ok = bool(chosen) and (skills_root / chosen / "SKILL.md").is_file()
        resolved[step] = (capability, chosen, candidates, ok)
    claimed = {chosen for (_c, chosen, _cand, ok) in resolved.values() if ok}
    unclaimed = [name for name in _installed_skills(skills_root) if name not in claimed]
    # Second pass: emit checks, drawing rename hints only from unclaimed skills.
    checks: list[dict] = []
    for step, (capability, chosen, candidates, ok) in resolved.items():
        entry = {"kind": "capability", "name": capability, "step": step, "ok": ok, "skill": chosen if ok else ""}
        if not ok:
            entry["candidates"] = candidates
            entry["suggestions"] = _rename_hints(candidates, unclaimed)
        checks.append(entry)
    return checks


def _load_policy_tolerant(root: Path) -> dict:
    """Merged policy structure for doctor's checks, robust to a broken project.

    Prefers the fully-merged effective policy (honours per-project overrides).
    `resolve_assets=False` still resolves the review-bridge success contract, so a
    broken/incomplete bridge would make it throw — in that case fall back to the
    bundled policy structure (with the project's reviewer bridge applied) so
    doctor can still enumerate every capability/path rather than dying.
    """
    try:
        return load_effective_policy(str(root), resolve_assets=False)
    except PolicyError:
        raw = json.loads(read_text(bundled_skill_root(str(root)) / "data" / "orchestration-policy.json"))
        review_assets = raw.setdefault("steps", {}).setdefault("review", {}).setdefault("assets", {})
        review_assets["skillName"] = reviewer_bridge(str(root))
        return raw


def _check_paths(root: Path, policy: dict) -> list[dict]:
    checks: list[dict] = []
    for key, rel in bmad_paths(policy).items():
        exists = (root / rel).exists()
        checks.append({"kind": "path", "name": key, "ok": exists, "path": rel})
    return checks


def cmd_doctor(args: list[str]) -> int:
    if args and args[0] in ("--help", "-h"):
        print("Usage: story-automator doctor [PROJECT_ROOT] [--json]")
        print("  Verifies BMAD capabilities + path conventions resolve for the project.")
        return 0

    as_json = "--json" in args
    root = _resolve_project_root([a for a in args if a != "--json"])

    if not (root / "_bmad").is_dir():
        payload = {"ok": False, "error": "not_a_bmad_project", "projectRoot": str(root)}
        print(json.dumps(payload, indent=2) if as_json else f"✗ {root} has no _bmad/ — not a BMAD project.")
        return 1

    policy = _load_policy_tolerant(root)
    checks = _check_capabilities(root, policy) + _check_paths(root, policy)
    problems = [c for c in checks if not c["ok"]]
    ok = not problems
    version = _bmad_version(root)
    payload = {"ok": ok, "projectRoot": str(root), "bmadVersion": version, "checks": checks}

    if as_json:
        print(json.dumps(payload, indent=2))
        return 0 if ok else 1

    print(f"story-automator doctor — {root}" + (f" (BMAD {version})" if version else ""))
    print("Capabilities:")
    for c in [c for c in checks if c["kind"] == "capability"]:
        if c["ok"]:
            print(f"  ✓ {c['name']:<20} → {c['skill']}")
        else:
            print(f"  ✗ {c['name']:<20} none of {c.get('candidates') or []} present")
            hints = c.get("suggestions") or []
            if hints:
                print(f"       ↳ similar installed skills: {', '.join(hints)}")
            else:
                print("       ↳ no obvious rename match — check .claude/skills/ for the replacement")
            print(f"         (add the right skill to steps.{c['step']}.skillCandidates in orchestration-policy.json)")
    print("Paths:")
    for c in [c for c in checks if c["kind"] == "path"]:
        mark = "✓" if c["ok"] else "✗"
        print(f"  {mark} {c['name']:<22} {c['path']}")
    if ok:
        print("Result: OK — ready to run.")
    else:
        print(f"Result: {len(problems)} problem(s) — fix before running (see ↳ hints).")
    return 0 if ok else 1
