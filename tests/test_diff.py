"""Multilingual Discord difference formatting tests."""

from __future__ import annotations

import unittest

from noctalia_i18n_core.diff import AnsiStyle, format_ansi_diff

from .fixtures import RECORDED_NEW_VALUE, RECORDED_OLD_VALUE

_RESET = "\x1b[0m"
_REMOVED = "\x1b[1;31m"
_ADDED = "\x1b[1;32m"
_OLD_STYLE = AnsiStyle(color="red", bold=True, underline=False)
_NEW_STYLE = AnsiStyle(color="green", bold=True, underline=False)


def _diff(old: str | None, new: str | None) -> tuple[str, str]:
    return format_ansi_diff(
        old,
        new,
        old_style=_OLD_STYLE,
        new_style=_NEW_STYLE,
    )


class DiffTests(unittest.TestCase):
    def test_dense_script_changes_are_highlighted_at_word_boundaries(self) -> None:
        old, new = _diff(RECORDED_OLD_VALUE, RECORDED_NEW_VALUE)

        self.assertEqual(old, f"在 Toast 通知周围绘制{_REMOVED}边框{_RESET}")
        self.assertEqual(new, f"在 Toast 通知周围绘制{_ADDED}轮廓{_RESET}")

    def test_single_spaces_connect_adjacent_changed_tokens(self) -> None:
        old, new = _diff("value; old phrase", "value: new phrase")

        self.assertEqual(old, f"value{_REMOVED}; old{_RESET} phrase")
        self.assertEqual(new, f"value{_ADDED}: new{_RESET} phrase")

    def test_canonical_unicode_and_emoji_clusters_remain_intact(self) -> None:
        old, new = _diff("cafe\u0301 👩\u200d💻", "café 👩\u200d🔧")

        self.assertEqual(old, f"cafe\u0301 {_REMOVED}👩\u200d💻{_RESET}")
        self.assertEqual(new, f"café {_ADDED}👩\u200d🔧{_RESET}")

    def test_missing_side_marks_the_complete_existing_value(self) -> None:
        added = _diff(None, "Added value")
        deleted = _diff("Deleted value", None)

        self.assertEqual(added, ("", f"{_ADDED}Added value{_RESET}"))
        self.assertEqual(deleted, (f"{_REMOVED}Deleted value{_RESET}", ""))

    def test_upstream_text_cannot_inject_ansi_or_close_the_code_fence(self) -> None:
        _, new = _diff(None, "```ansi \x1b[31m")

        self.assertEqual(
            new,
            f"{_ADDED}``\u200b`ansi \\x1b[31m{_RESET}",
        )

    def test_color_and_emphasis_are_independently_configurable(self) -> None:
        old, new = format_ansi_diff(
            "Old value",
            "New value",
            old_style=AnsiStyle(color="yellow", bold=False, underline=True),
            new_style=AnsiStyle(color=None, bold=True, underline=False),
        )

        self.assertEqual(old, "\x1b[4;33mOld\x1b[0m value")
        self.assertEqual(new, "\x1b[1mNew\x1b[0m value")
