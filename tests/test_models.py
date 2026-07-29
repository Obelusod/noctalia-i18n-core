"""Domain model tests."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta

from noctalia_i18n_core.models import (
    Delivery,
    JsonValue,
    PollResult,
)

from .fixtures import fixture_change


class ModelTests(unittest.TestCase):
    def test_change_rejects_invalid_action_value_combinations(self) -> None:
        cases = [
            {"action": "added", "old_value": "old"},
            {"action": "deleted", "new_value": "new"},
            {"action": "modified", "old_value": None},
        ]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                replace(fixture_change(), **values)

    def test_change_requires_a_timezone_and_non_empty_locale(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone"):
            replace(fixture_change(), occurred_at=datetime(2026, 1, 1))
        with self.assertRaisesRegex(ValueError, "locale"):
            replace(fixture_change(), locale="")

    def test_change_keeps_locale_identifiers_opaque(self) -> None:
        for locale in ("de", "zh-Hans", "zh_Hans", "project.custom"):
            with self.subTest(locale=locale):
                self.assertEqual(
                    replace(fixture_change(), locale=locale).locale, locale
                )

    def test_change_rejects_empty_actor_metadata(self) -> None:
        for field in ("actor_url", "actor_avatar_url"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                replace(fixture_change(), **{field: ""})

    def test_change_rejects_a_blank_actor_name(self) -> None:
        for actor in ("", "   "):
            with self.subTest(actor=actor), self.assertRaisesRegex(ValueError, "actor"):
                replace(fixture_change(), actor=actor)

    def test_change_detail_url_is_optional(self) -> None:
        self.assertIsNone(replace(fixture_change(), url=None).url)
        with self.assertRaisesRegex(ValueError, "url"):
            replace(fixture_change(), url="")

    def test_source_change_is_derived_from_the_noctalia_locale(self) -> None:
        self.assertTrue(fixture_change(locale="en").is_source)
        self.assertFalse(fixture_change(locale="de").is_source)

    def test_iso_timestamp_preserves_available_precision(self) -> None:
        occurred_at = fixture_change().occurred_at
        self.assertEqual(
            fixture_change(
                occurred_at=occurred_at.replace(microsecond=123000)
            ).iso_timestamp,
            "2026-07-17T09:51:04.123Z",
        )
        self.assertEqual(
            fixture_change(
                occurred_at=occurred_at.replace(microsecond=123456)
            ).iso_timestamp,
            "2026-07-17T09:51:04.123456Z",
        )

    def test_delivery_resolves_current_and_previous_source_text(self) -> None:
        translated = fixture_change(key="fixture.key", locale="de")
        self.assertEqual(
            Delivery.from_change(translated, {"fixture.key": "Current"}).source_text,
            "Current",
        )
        self.assertEqual(
            Delivery.from_change(
                translated,
                {},
                {"fixture.key": "Previous"},
            ).source_text,
            "Previous",
        )

    def test_source_delivery_uses_its_changed_value(self) -> None:
        added = fixture_change(
            locale="en", action="added", old_value=None, new_value="Added"
        )
        deleted = fixture_change(
            locale="en", action="deleted", old_value="Deleted", new_value=None
        )
        self.assertEqual(Delivery.from_change(added, {}).source_text, "Added")
        self.assertEqual(Delivery.from_change(deleted, {}).source_text, "Deleted")

    def test_poll_result_requires_oldest_first_changes(self) -> None:
        older = fixture_change(id="older")
        newer = fixture_change(
            id="newer",
            occurred_at=older.occurred_at + timedelta(minutes=1),
        )
        self.assertEqual(
            PollResult((older, newer), "cursor", 2).changes, (older, newer)
        )
        with self.assertRaisesRegex(ValueError, "oldest first"):
            PollResult((newer, older), "cursor", 2)
        with self.assertRaisesRegex(ValueError, "unique"):
            PollResult((older, older), "cursor", 2)

    def test_poll_result_validates_source_texts(self) -> None:
        with self.assertRaisesRegex(ValueError, "source texts"):
            PollResult((), "cursor", 0, {"": "invalid"})

    def test_poll_result_normalizes_its_json_cursor(self) -> None:
        ids: list[JsonValue] = ["first"]
        cursor: dict[str, JsonValue] = {"ids": ids}
        result = PollResult((), cursor, 0)

        ids.append("second")

        self.assertEqual(result.cursor, {"ids": ["first"]})
