"""Public behavior tests for the Noctalia Translate client."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from noctalia_i18n_core import NoctaliaClient

from .fixtures import JsonValue, Session, changes_page, indexed_page, translation_record

DESCRIPTION = "settings.widgets.settings.scroll-repeat.description"
LABEL = "settings.widgets.settings.scroll-repeat.label"
DESCRIPTION_EN = (
    "Choose whether scroll actions follow command semantics, run once per gesture, "
    "or repeat for every scroll step"
)
DESCRIPTION_ZH = (
    "选择滚动触发是自动遵循命令的默认行为，每轮连续滚动仅触发一次，"
    "还是每个滚动步长均触发一次"
)
LABEL_EN = "Scroll Repeat"
LABEL_ZH = "滚动触发模式"
PREVIOUS_DESCRIPTION_ZH = (
    "选择滚动操作是遵循命令语义、每次手势仅运行一次，还是针对每个滚动步长重复运行"
)


class ClientTests(unittest.TestCase):
    def test_project_metadata_and_complete_catalog(self) -> None:
        session = Session(
            get_responses=(
                indexed_page(
                    {
                        "project": {
                            "slug": "noctalia",
                            "name": "Noctalia v5",
                            "description": (
                                "A beautiful, minimal desktop shell for Wayland"
                            ),
                            "ai_context": "Keep Noctalia untranslated.",
                        },
                        "stats": {"totalKeys": 2474},
                        "locales": ["en", "fr", "zh-Hans"],
                        "missingByLocale": {"en": 0, "fr": 443, "zh-Hans": 0},
                        "outdatedByLocale": {"fr": 29},
                    }
                ),
                {
                    "en": {
                        "settings": {
                            "widgets": {
                                "settings": {
                                    "scroll-repeat": {"description": DESCRIPTION_EN}
                                }
                            }
                        }
                    },
                    "zh-Hans": {
                        "settings": {
                            "widgets": {
                                "settings": {
                                    "scroll-repeat": {"description": DESCRIPTION_ZH}
                                }
                            }
                        }
                    },
                },
            )
        )
        with NoctaliaClient("noctalia", timeout=10, session=session) as client:
            project = client.project()
            self.assertEqual(client.slug, "noctalia")
            self.assertEqual(project.name, "Noctalia v5")
            self.assertEqual(project.translation_context, "Keep Noctalia untranslated.")
            self.assertEqual(
                [
                    (
                        locale.code,
                        locale.translation_count,
                        locale.missing_count,
                        locale.review_count,
                    )
                    for locale in project.locales
                ],
                [("en", 2474, 0, 0), ("fr", 2031, 443, 29), ("zh-Hans", 2474, 0, 0)],
            )
            self.assertEqual(
                client.catalog(),
                {DESCRIPTION: {"en": DESCRIPTION_EN, "zh-Hans": DESCRIPTION_ZH}},
            )

    def test_translations_keep_missing_keys_and_locale_order(self) -> None:
        session = Session(
            post_responses=(
                {
                    "translations": {
                        DESCRIPTION: {
                            "zh-Hans": translation_record(
                                DESCRIPTION, "zh-Hans", DESCRIPTION_ZH
                            )
                        }
                    }
                },
            )
        )
        with NoctaliaClient("noctalia", timeout=10, session=session) as client:
            result = client.translations(
                DESCRIPTION,
                LABEL,
                locales=("zh-Hans", "en"),
            )
        self.assertEqual(
            result,
            {
                DESCRIPTION: {"zh-Hans": DESCRIPTION_ZH},
                LABEL: {},
            },
        )
        self.assertEqual(
            session.post_calls[0][1]["json"],
            {
                "keys": [DESCRIPTION, LABEL],
                "locales": ["zh-Hans", "en"],
            },
        )

    def test_search_reads_every_page_then_fetches_values_once(self) -> None:
        session = Session(
            get_responses=(
                indexed_page(
                    {
                        "project": {"slug": "noctalia"},
                        "search": "滚动",
                        "initialLocale": "zh-Hans",
                        "keys": [DESCRIPTION],
                        "page": 1,
                        "totalPages": 2,
                    }
                ),
                indexed_page(
                    {
                        "project": {"slug": "noctalia"},
                        "search": "滚动",
                        "initialLocale": "zh-Hans",
                        "keys": [LABEL],
                        "page": 2,
                        "totalPages": 2,
                    }
                ),
            ),
            post_responses=(
                {
                    "translations": {
                        DESCRIPTION: {
                            "en": translation_record(DESCRIPTION, "en", DESCRIPTION_EN),
                            "zh-Hans": translation_record(
                                DESCRIPTION, "zh-Hans", DESCRIPTION_ZH
                            ),
                        },
                        LABEL: {
                            "en": translation_record(LABEL, "en", LABEL_EN),
                            "zh-Hans": translation_record(LABEL, "zh-Hans", LABEL_ZH),
                        },
                    }
                },
            ),
        )
        with NoctaliaClient("noctalia", timeout=10, session=session) as client:
            result = client.search_translations(
                "滚动",
                search_locale="zh-Hans",
                locales=("en", "zh-Hans"),
            )
        self.assertEqual(
            result,
            {
                DESCRIPTION: {"en": DESCRIPTION_EN, "zh-Hans": DESCRIPTION_ZH},
                LABEL: {"en": LABEL_EN, "zh-Hans": LABEL_ZH},
            },
        )
        self.assertEqual(len(session.post_calls), 1)

    def test_key_tree_preserves_real_segments_and_hierarchy_boundaries(self) -> None:
        tree: JsonValue = {
            "_count": 3,
            "settings": {
                "_count": 3,
                "widgets": {
                    "_count": 3,
                    "options": {
                        "_count": 1,
                        "count": {"_count": 1, "_isLeaf": True},
                    },
                    "settings": {
                        "_count": 2,
                        "scroll-repeat": {
                            "_count": 2,
                            "description": {"_count": 1, "_isLeaf": True},
                            "label": {"_count": 1, "_isLeaf": True},
                        },
                    },
                },
            },
        }
        session = Session(
            get_responses=(
                indexed_page(
                    {
                        "project": {"slug": "noctalia"},
                        "tree": tree,
                        "totalKeys": 3,
                    }
                ),
            )
        )
        with NoctaliaClient("noctalia", timeout=10, session=session) as client:
            self.assertEqual(
                client.keys(prefix="settings.widgets.settings.scroll-repeat"),
                (DESCRIPTION, LABEL),
            )

    def test_missing_keys_and_reviews_preserve_website_information(self) -> None:
        session = Session(
            get_responses=(
                {"keys": [DESCRIPTION, LABEL], "count": 2},
                {
                    "items": [
                        {
                            "key": "settings.schema.appearance.theme-mode.description",
                            "enOld": "Choose dark, light, or auto based on time of day",
                            "enNew": "Choose Dark, Light, or Auto based on time of day",
                        },
                        {
                            "key": "desktop-widgets.editor.settings.change-image",
                            "enOld": "",
                            "enNew": "Change…",
                        },
                    ],
                    "count": 2,
                },
            )
        )
        with NoctaliaClient("noctalia", timeout=10, session=session) as client:
            self.assertEqual(
                client.missing_keys("fr", query="scroll"), (DESCRIPTION, LABEL)
            )
            reviews = client.reviews("fr")

        self.assertEqual(reviews[0].locale, "fr")
        self.assertEqual(
            reviews[0].old_source,
            "Choose dark, light, or auto based on time of day",
        )
        self.assertIsNone(reviews[1].old_source)
        self.assertIn("locale=fr", reviews[0].translation_url)

    def test_changes_merge_keys_newest_first(self) -> None:
        def record(key: str, change_id: str, created_at: int) -> dict[str, object]:
            return {
                "id": change_id,
                "translation_key": key,
                "locale": "zh-Hans",
                "created_by_login": "editor",
                "created_at": created_at,
                "text": PREVIOUS_DESCRIPTION_ZH,
                "new_text": DESCRIPTION_ZH,
            }

        session = Session(
            get_responses=(
                {"history": [record(DESCRIPTION, "1", 1_000)]},
                {"history": [record(LABEL, "2", 2_000)]},
            )
        )
        with NoctaliaClient("noctalia", timeout=10, session=session) as client:
            result = client.changes(DESCRIPTION, LABEL, locale="zh-Hans")
        self.assertEqual([change.id for change in result], ["2", "1"])

    def test_change_stream_is_lazy_and_stops_at_the_time_boundary(self) -> None:
        session = Session(
            get_responses=(
                changes_page(
                    (
                        "2",
                        DESCRIPTION,
                        "zh-Hans",
                        "editor",
                        2_000,
                        PREVIOUS_DESCRIPTION_ZH,
                        DESCRIPTION_ZH,
                    ),
                    page=1,
                    total_pages=2,
                ),
                changes_page(
                    (
                        "1",
                        DESCRIPTION,
                        "zh-Hans",
                        None,
                        1_000,
                        None,
                        PREVIOUS_DESCRIPTION_ZH,
                    ),
                    page=2,
                    total_pages=2,
                ),
            )
        )
        client = NoctaliaClient("noctalia", timeout=10, session=session)
        iterator = client.iter_changes(since=datetime.fromtimestamp(1.5, UTC))
        self.assertEqual(len(session.get_calls), 0)
        self.assertEqual(next(iterator).id, "2")
        self.assertEqual(len(session.get_calls), 1)
        with self.assertRaises(StopIteration):
            next(iterator)
        self.assertEqual(len(session.get_calls), 2)

    def test_change_anchor_is_confirmed_before_results_are_returned(self) -> None:
        session = Session(
            get_responses=(
                changes_page(
                    (
                        "3",
                        DESCRIPTION,
                        "zh-Hans",
                        "editor",
                        3_000,
                        PREVIOUS_DESCRIPTION_ZH,
                        DESCRIPTION_ZH,
                    ),
                    (
                        "2",
                        DESCRIPTION,
                        "zh-Hans",
                        "editor",
                        2_000,
                        PREVIOUS_DESCRIPTION_ZH,
                        DESCRIPTION_ZH,
                    ),
                ),
            )
        )
        with NoctaliaClient("noctalia", timeout=10, session=session) as client:
            self.assertEqual(
                [change.id for change in client.iter_changes(after="2")], ["3"]
            )

        missing = Session(
            get_responses=(
                changes_page(
                    (
                        "1",
                        DESCRIPTION,
                        "zh-Hans",
                        "editor",
                        1_000,
                        PREVIOUS_DESCRIPTION_ZH,
                        DESCRIPTION_ZH,
                    )
                ),
            )
        )
        with NoctaliaClient("noctalia", timeout=10, session=missing) as client:
            with self.assertRaises(RuntimeError):
                tuple(client.iter_changes(after="missing"))

    def test_invalid_inputs_and_inconsistent_responses_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            NoctaliaClient("", timeout=10)
        with self.assertRaises(ValueError):
            NoctaliaClient("noctalia", timeout=0)
        with self.assertRaises(ValueError):
            NoctaliaClient("noctalia", timeout=10, session=Session()).iter_changes(
                since=datetime(2026, 1, 1)
            )
        session = Session(
            post_responses=(
                {
                    "translations": {
                        DESCRIPTION: {
                            "en": translation_record(LABEL, "en", DESCRIPTION_EN)
                        }
                    }
                },
            )
        )
        with NoctaliaClient("noctalia", timeout=10, session=session) as client:
            with self.assertRaises(RuntimeError):
                client.translations(DESCRIPTION)

    def test_session_ownership_is_respected(self) -> None:
        external = Session()
        NoctaliaClient("noctalia", timeout=10, session=external).close()
        self.assertFalse(external.closed_by_client)

        owned = Session()
        with patch("noctalia_i18n_core.client.requests.Session", return_value=owned):
            client = NoctaliaClient("noctalia", timeout=10)
        client.close()
        self.assertTrue(owned.closed_by_client)


if __name__ == "__main__":
    unittest.main()
