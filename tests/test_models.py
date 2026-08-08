"""Tests for the public translation change value."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime

from noctalia_i18n_core import Change, ChangeAction, Locale, Project


def _change(
    *,
    action: ChangeAction = "modified",
    old_value: str | None = "滚动重复",
    new_value: str | None = "滚动触发模式",
) -> Change:
    return Change(
        id="change-1",
        key="settings.widgets.settings.scroll-repeat.description",
        locale="zh-Hans",
        action=action,
        old_value=old_value,
        new_value=new_value,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        translation_url="https://i18n.noctalia.dev/projects/noctalia/translate",
        actor_login="Obelusod",
    )


class ChangeTests(unittest.TestCase):
    def test_action_and_values_must_agree(self) -> None:
        for values in (
            {"action": "added", "old_value": "old"},
            {"action": "deleted", "new_value": "new"},
            {"action": "modified", "old_value": None},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                replace(_change(), **values)

    def test_required_fields_and_time_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            replace(_change(), key="")
        with self.assertRaises(ValueError):
            replace(_change(), occurred_at=datetime(2026, 1, 1))
        with self.assertRaises(ValueError):
            replace(_change(), translation_url="")

    def test_automated_changes_need_no_actor(self) -> None:
        change = replace(_change(), actor_login=None)
        self.assertIsNone(change.actor_login)
        self.assertEqual(change.occurred_at.tzinfo, UTC)


class ProjectTests(unittest.TestCase):
    def test_progress_is_derived_from_locale_counts(self) -> None:
        project = Project(
            slug="noctalia",
            name="Noctalia v5",
            description="A beautiful, minimal desktop shell for Wayland",
            translation_context=None,
            key_count=10,
            locales=(
                Locale(
                    code="en",
                    translation_count=10,
                    missing_count=0,
                    review_count=0,
                ),
                Locale(
                    code="zh-Hans",
                    translation_count=8,
                    missing_count=2,
                    review_count=1,
                ),
            ),
        )
        self.assertEqual(project.language_count, 2)
        self.assertEqual(project.translation_count, 18)
        self.assertEqual(project.missing_count, 2)
        self.assertEqual(project.review_count, 1)
        self.assertEqual(project.progress, 90)

    def test_project_rejects_inconsistent_locale_counts(self) -> None:
        locale = Locale(
            code="zh-Hans",
            translation_count=8,
            missing_count=2,
            review_count=0,
        )
        with self.assertRaises(ValueError):
            Project(
                slug="noctalia",
                name="Noctalia v5",
                description=None,
                translation_context=None,
                key_count=11,
                locales=(locale,),
            )


if __name__ == "__main__":
    unittest.main()
