"""Message loading and rendering tests with synthetic templates."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from noctalia_i18n_core.messages import (
    MessageValues,
    load_merge,
    load_message,
)
from noctalia_i18n_core.models import Action

from .fixtures import (
    FIXTURE_ACTOR,
    FIXTURE_NEW_VALUE,
    FIXTURE_OLD_VALUE,
    FIXTURE_URL,
    fixture_change,
)


def _write(directory: Path, name: str, content: str) -> None:
    (directory / f"{name}.yaml").write_text(content, encoding="utf-8")


def _message_yaml(description: str, *, diff: bool = False) -> str:
    styles = (
        """
diff:
  old: {color: red, bold: false, underline: false}
  new: {color: green, bold: true, underline: false}
"""
        if diff
        else ""
    )
    rendered = json.dumps(description, ensure_ascii=False)
    return (
        styles
        + f"""
added:
  description: {rendered}
modified:
  description: {rendered}
deleted:
  description: {rendered}
"""
    )


def _merge_yaml(
    entry: str = "{key}: {new_value:fallback=deleted}",
    *,
    description: str = "{entries}",
) -> str:
    rendered_entry = json.dumps(entry, ensure_ascii=False)
    rendered_description = json.dumps(description, ensure_ascii=False)
    return f"""
source:
  description: {rendered_description}
target:
  description: {rendered_description}
entries:
  separator: "\\n"
  added: {rendered_entry}
  modified: {rendered_entry}
  deleted: {rendered_entry}
"""


def _values(
    *,
    monitor_id: str = "monitor",
    key: str | None = None,
    old_value: str | None = FIXTURE_OLD_VALUE,
    new_value: str | None = FIXTURE_NEW_VALUE,
    locale: str = "zh-Hans",
    actor: str = FIXTURE_ACTOR,
    action: Action = "modified",
    change_url: str | None = FIXTURE_URL,
) -> MessageValues:
    change = fixture_change()
    return MessageValues(
        monitor_id=monitor_id,
        project="project",
        key=change.key if key is None else key,
        source="Source",
        old_value=old_value,
        new_value=new_value,
        locale=locale,
        actor=actor,
        actor_url=change.actor_url,
        actor_avatar_url=change.actor_avatar_url,
        action=action,
        change_url=change_url,
        timestamp=change.iso_timestamp,
        unix_time=int(change.occurred_at.timestamp()),
    )


class MessageTests(unittest.TestCase):
    def test_message_renders_derived_links_and_omits_empty_urls(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            _write(
                directory,
                "synthetic",
                _message_yaml("{key_link} {new_value:fallback=deleted}").replace(
                    "  description:",
                    '  url: "{change_url}"\n  description:',
                ),
            )
            message = load_message("synthetic", directory)

            linked = message.render("modified", _values())
            rendered = message.render(
                "deleted",
                _values(action="deleted", new_value=None, change_url=None),
            )

        self.assertEqual(
            linked.get("description"),
            (
                "[`fixture.translation.description`]"
                "(https://fixtures.invalid/changes/fixture.translation.description) "
                "Fixture modified value"
            ),
        )
        self.assertEqual(
            rendered.get("description"),
            "`fixture.translation.description` deleted",
        )
        self.assertNotIn("url", rendered)

    def test_message_diff_styles_are_loaded_only_when_used(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            _write(
                directory,
                "diff",
                _message_yaml("{old_diff} -> {new_diff}", diff=True),
            )
            rendered = load_message("diff", directory).render(
                "modified",
                _values(old_value="old", new_value="new"),
            )

            _write(directory, "missing-diff", _message_yaml("{old_diff}"))
            with self.assertRaisesRegex(ValueError, "diff is required"):
                load_message("missing-diff", directory)

        description = rendered.get("description", "")
        self.assertIn("\x1b[31mold", description)
        self.assertIn("\x1b[1;32mnew", description)

    def test_message_rejects_duplicate_or_unknown_yaml_fields(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            duplicate = _message_yaml("{key}") + "added:\n  description: duplicate\n"
            _write(directory, "duplicate", duplicate)
            with self.assertRaisesRegex(ValueError, "duplicate key"):
                load_message("duplicate", directory)

            unknown = _message_yaml("{key}") + "unexpected: true\n"
            _write(directory, "unknown", unknown)
            with self.assertRaisesRegex(ValueError, "unknown fields"):
                load_message("unknown", directory)

            renamed = _merge_yaml().replace(
                'separator: "\\n"',
                'divider: "\\n"',
            )
            _write(directory, "renamed", renamed)
            with self.assertRaises(ValueError) as captured:
                load_merge("renamed", directory)
            message = str(captured.exception)
            self.assertIn("missing required fields: ['separator']", message)
            self.assertIn("unknown fields: ['divider']", message)

    def test_message_name_cannot_escape_the_caller_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            with self.assertRaisesRegex(ValueError, "name must match"):
                load_message("../outside", directory)
            with self.assertRaisesRegex(ValueError, "Unknown message"):
                load_message("missing", directory)

    def test_message_rejects_unknown_placeholders_before_rendering(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            _write(directory, "invalid", _message_yaml("{unknown}"))
            with self.assertRaisesRegex(ValueError, "unknown placeholder"):
                load_message("invalid", directory)

    def test_message_enforces_rendered_discord_limits(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            _write(directory, "limited", _message_yaml("{new_value}"))
            message = load_message("limited", directory)
            with self.assertRaisesRegex(ValueError, "4096"):
                message.render(
                    "modified",
                    _values(new_value="x" * 4097),
                )

    def test_template_truncation_counts_wide_characters(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            _write(directory, "truncate", _message_yaml("{new_value:truncate=5}"))
            rendered = load_message("truncate", directory).render(
                "modified",
                _values(new_value="你好世界"),
            )

        self.assertEqual(rendered.get("description"), "你好…")

    def test_merge_renders_synthetic_entries_and_counts(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            _write(
                directory,
                "merge",
                _merge_yaml(
                    "{key}: {new_value:fallback=deleted}",
                    description="{count} changes by {actors}\n{entries}",
                ),
            )
            pages = load_merge("merge", directory).render(
                (
                    _values(key="first", new_value="one", actor="alice"),
                    _values(
                        key="second",
                        action="deleted",
                        new_value=None,
                        actor="bob",
                    ),
                )
            )

        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].count, 2)
        self.assertEqual(
            pages[0].embed.get("description"),
            "2 changes by alice, bob\nfirst: one\nsecond: deleted",
        )

    def test_merge_splits_without_losing_entry_counts(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            _write(directory, "split", _merge_yaml("{new_value}"))
            pages = load_merge("split", directory).render(
                (
                    _values(key="first", new_value="a" * 2100),
                    _values(key="second", new_value="b" * 2100),
                )
            )

        self.assertEqual([page.count for page in pages], [1, 1])

    def test_merge_rejects_mixed_locale_or_monitor_context(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            _write(directory, "merge", _merge_yaml())
            merge = load_merge("merge", directory)
            with self.assertRaisesRegex(ValueError, "mix locales"):
                merge.render((_values(), _values(locale="fr")))
            with self.assertRaisesRegex(ValueError, "mix monitor contexts"):
                merge.render((_values(), _values(monitor_id="other")))

    def test_merge_page_requires_at_least_one_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            _write(directory, "merge", _merge_yaml())
            merge = load_merge("merge", directory)

            with self.assertRaisesRegex(ValueError, "at least one"):
                merge.render(())


if __name__ == "__main__":
    unittest.main()
