"""Regression tests for interactive-Claude completion detection.

2026-07-03 field failure (acme Epic 36 first ladder run): the create
session finished ("Crunched for 4m 56s", idle at the input box) but
check-session/monitor-session reported it running forever. Two gates were
broken:

1. `_claude_completion_marker_present` whitelisted only Baked|Done|Finished,
   but Claude Code rotates through arbitrary whimsical verbs (Crunched,
   Cogitated, Simmered, ...) and sub-minute turns render seconds-only
   ("Cogitated for 19s").
2. `_check_prompt_visible` only recognized shell prompts (❯ $ # %) at
   end-of-line; the modern interactive Claude TUI never shows one — idle is
   "no 'esc to interrupt' hint while the footer chrome is rendered".
"""

from story_automator.core import tmux_runtime as tr


class TestCompletionMarker:
    def test_matches_arbitrary_whimsical_verbs(self):
        for line in (
            "✳ Crunched for 4m 56s",
            "* Cogitated for 19s",
            "Baked for 5m",
            "  ✻ Simmered for 12m 3s",
            "Done for 3m",
            "✻ Sautéed for 14m 4s",
            "· Flambéed for 45s",
        ):
            assert tr._claude_completion_marker_present(line), line

    def test_rejects_non_timer_lines(self):
        for line in (
            "waiting for 10 seconds",
            "polling for 3 more results",
            "scheduled for 3pm",
            "GET /api for 200ms",
            "",
        ):
            assert not tr._claude_completion_marker_present(line), line


IDLE_TUI = """\
Advanced elicitation caught and fixed 3 gaps.

✳ Crunched for 4m 56s

> dev this story _bmad-output/implementation-artifacts/36-1-de-perish-report-p
rompts.md
──────────────
  \U0001f916 Sonnet 4.6 | ⏳ 5h 33% (resets in 3h49m)
  \U0001f33f main · ⏱ session 6m · ✎ diff +199/-31
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents
"""

WORKING_TUI = """\
✳ Crunching… (32s · ⚒ 1.2k tokens · esc to interrupt)

> █
──────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""

SHELL_PROMPT = "some output\nuser@host dir %\n"


class TestPromptVisible:
    def _visible(self, monkeypatch, capture: str) -> str:
        monkeypatch.setattr(tr, "_capture_text", lambda session, start: capture)
        return tr._check_prompt_visible("any-session")

    def test_idle_interactive_tui_is_visible(self, monkeypatch):
        assert self._visible(monkeypatch, IDLE_TUI) == "true"

    def test_working_interactive_tui_is_not_visible(self, monkeypatch):
        assert self._visible(monkeypatch, WORKING_TUI) == "false"

    def test_shell_prompt_still_visible(self, monkeypatch):
        assert self._visible(monkeypatch, SHELL_PROMPT) == "true"

    def test_empty_capture_not_visible(self, monkeypatch):
        assert self._visible(monkeypatch, "") == "false"


class TestStableIdleLines:
    def test_strips_ticking_footer_lines(self):
        capture = (
            "Real output line\n"
            "✻ Sautéed for 14m 4s\n"
            "> dev this story 36-3\n"
            "──────────────\n"
            "  \U0001f916 Sonnet 4.6 | ⏳ 5h 53% (resets in 2h15m) · 7d 17% | 76% ctx\n"
            "  \U0001f33f main · ⏱ session 14m · ✎ diff +556/-10\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
            "9% until auto-compact\n"
        )
        stable = tr._stable_idle_lines(capture)
        assert "Sautéed for 14m 4s" in stable
        assert "Real output line" in stable
        assert "session 14m" not in stable
        assert "resets in" not in stable
        assert "auto-compact" not in stable
        assert "shift+tab" not in stable

    def test_identical_after_footer_tick(self):
        base = "output\n✻ Sautéed for 14m 4s\n> \n  ⏱ session {}m · diff\n"
        assert tr._stable_idle_lines(base.format(14)) == tr._stable_idle_lines(base.format(15))
