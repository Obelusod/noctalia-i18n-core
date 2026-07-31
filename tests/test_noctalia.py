"""Noctalia source contract tests with deterministic HTTP responses."""

from __future__ import annotations

import json
import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import requests

from noctalia_i18n_core.models import Change, JsonValue
from noctalia_i18n_core.sources.noctalia import NoctaliaSource

from .contracts import PollScenario, assert_poll_contract
from .fixtures import (
    FIXTURE_SOURCE,
    RECENT_CHANGES_DATA,
    RECORDED_ACTOR,
    RECORDED_ID,
    RECORDED_KEY,
    RECORDED_NEW_VALUE,
    RECORDED_OLD_VALUE,
    page_data,
)

type _Record = tuple[str, str, str | None, int, str | None, str | None]


def _change_id(record: _Record) -> str:
    return f"{record[0]}@{record[3]}"


def _page(
    *records: _Record,
    project: str = "noctalia",
    page: int = 1,
    total_pages: int = 1,
) -> dict[str, JsonValue]:
    return page_data(
        *((_change_id(record), *record) for record in records),
        project=project,
        page=page,
        total_pages=total_pages,
    )


def _response(
    payload: object,
    *,
    project: str = "noctalia",
    export: bool = False,
) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps(payload).encode()  # pyright: ignore[reportPrivateUsage]
    response.headers["Content-Type"] = "application/json"
    suffix = "api/projects" if export else "projects"
    endpoint = f"{project}/pull" if export else f"{project}/__data.json"
    response.url = f"https://i18n.noctalia.dev/{suffix}/{endpoint}"
    return response


def _session(*responses: requests.Response | requests.RequestException) -> MagicMock:
    session = MagicMock(spec=requests.Session)
    session.headers = {}
    session.proxies = {}
    session.get.side_effect = responses
    return session


def _source(
    *responses: requests.Response | requests.RequestException,
    project: str = "noctalia",
) -> tuple[NoctaliaSource, MagicMock]:
    session = _session(*responses)
    return NoctaliaSource(project, 30, session), session


def _records(
    prefix: str,
    count: int,
    *,
    start: int = 1784300000000,
    locale: str = "de",
) -> tuple[_Record, ...]:
    return tuple(
        (
            f"{prefix}.{index}",
            locale,
            "fixture-editor",
            start - index * 1000,
            f"old-{index}",
            f"new-{index}",
        )
        for index in range(count)
    )


def _baseline_responses(
    payload: object,
    records: tuple[_Record, ...] | None = None,
    *,
    project: str = "noctalia",
) -> tuple[requests.Response, ...]:
    baseline = records or _records("fixture.baseline", 5)
    return (
        _response(_page(*baseline, project=project), project=project),
        _response(payload, project=project, export=True),
    )


def _expected_change(record: _Record, *, project: str = "noctalia") -> Change:
    key, locale, actor, created_at, old_value, new_value = record
    action = (
        "added"
        if old_value in {None, ""}
        else "deleted"
        if new_value in {None, ""}
        else "modified"
    )
    return Change(
        id=_change_id(record),
        key=key,
        locale=locale,
        actor=actor or "API",
        old_value=None if action == "added" else old_value,
        new_value=None if action == "deleted" else new_value,
        action=action,
        occurred_at=datetime.fromtimestamp(created_at / 1000, UTC),
        url=(
            f"https://i18n.noctalia.dev/projects/{project}/translate?"
            f"search={key}&locale={locale}"
        ),
        actor_url=f"https://github.com/{actor}" if actor else None,
        actor_avatar_url=f"https://github.com/{actor}.png" if actor else None,
    )


class NoctaliaSourceTests(unittest.TestCase):
    def test_page_parses_recorded_changes(self) -> None:
        source, _ = _source(_response(RECENT_CHANGES_DATA))

        changes = source.history(1)

        self.assertEqual(len(changes), 2)
        self.assertEqual(
            [
                (
                    change.id,
                    change.key,
                    change.locale,
                    change.actor,
                    change.action,
                    change.old_value,
                    change.new_value,
                    change.iso_timestamp,
                )
                for change in changes
            ],
            [
                (
                    RECORDED_ID,
                    RECORDED_KEY,
                    "zh-Hans",
                    RECORDED_ACTOR,
                    "modified",
                    RECORDED_OLD_VALUE,
                    RECORDED_NEW_VALUE,
                    "2026-07-17T09:51:04.838Z",
                ),
                (
                    "60edf3d8-157e-438c-9c39-0ffbd7bb73a2",
                    RECORDED_KEY,
                    "de",
                    "notiant",
                    "added",
                    None,
                    "Zeichne einen Umriss um Benachrichtigungs-Toasts",
                    "2026-07-17T05:40:07.486Z",
                ),
            ],
        )

    def test_page_normalizes_api_additions_and_deletions(self) -> None:
        added: _Record = (
            "fixture.added",
            "sr-Latn-RS",
            None,
            1784300001000,
            "",
            "Added",
        )
        deleted: _Record = (
            "fixture.deleted",
            "de",
            "fixture-editor",
            1784300000000,
            "Former",
            "",
        )
        source, _ = _source(_response(_page(added, deleted)))

        changes = source.history(1)

        self.assertEqual(
            (
                changes[0].actor,
                changes[0].action,
                changes[0].old_value,
                changes[0].new_value,
                changes[0].actor_url,
            ),
            ("API", "added", None, "Added", None),
        )
        self.assertEqual(
            (changes[1].action, changes[1].old_value, changes[1].new_value),
            ("deleted", "Former", None),
        )

    def test_page_skips_an_empty_change_without_dropping_the_page(self) -> None:
        modified: _Record = (
            "fixture.modified",
            "de",
            "fixture-editor",
            1784300002000,
            "Former",
            "Updated",
        )
        empty: _Record = (
            "fixture.empty",
            "pt-BR",
            "fixture-editor",
            1784300001000,
            "",
            "",
        )
        added: _Record = (
            "fixture.added",
            "de",
            "fixture-editor",
            1784300000000,
            "",
            "Added",
        )
        source, _ = _source(_response(_page(modified, empty, added)))

        changes = source.history(1)

        self.assertEqual(
            [(change.id, change.action) for change in changes],
            [
                (_change_id(modified), "modified"),
                (_change_id(added), "added"),
            ],
        )

    def test_page_returns_an_empty_out_of_range_page(self) -> None:
        source, _ = _source(_response(_page(page=2, total_pages=1)))

        self.assertEqual(source.history(2), ())

    def test_page_rejects_invalid_or_inconsistent_upstream_data(self) -> None:
        duplicate = _records("duplicate", 2)
        duplicate_data = page_data(
            ("same", *duplicate[0]),
            ("same", *duplicate[1]),
        )
        unordered = (
            (
                "fixture.older",
                "de",
                "fixture-editor",
                1784300000000,
                "old",
                "new",
            ),
            (
                "fixture.newer",
                "de",
                "fixture-editor",
                1784300001000,
                "old",
                "new",
            ),
        )
        cases: tuple[tuple[str, object], ...] = (
            ("missing nodes", {"type": "data", "nodes": []}),
            ("unexpected project", _page(*_records("project", 1), project="other")),
            (
                "filtered history",
                page_data(
                    *(
                        (_change_id(record), *record)
                        for record in _records("filtered", 1)
                    ),
                    changes_locale="de",
                ),
            ),
            ("duplicate ID", duplicate_data),
            ("unordered records", _page(*unordered)),
        )
        for name, payload in cases:
            source, _ = _source(_response(payload))
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(RuntimeError, "Recent Changes"),
            ):
                source.history(1)

    def test_source_satisfies_the_shared_polling_contract(self) -> None:
        baseline = _records("fixture.baseline", 5)
        newer: _Record = (
            "fixture.newer",
            "de",
            "fixture-editor",
            1784300010000,
            "B",
            "C",
        )
        older: _Record = (
            "fixture.older",
            "de",
            "fixture-editor",
            1784300009000,
            "A",
            "B",
        )
        source_payload = {"en": {"fixture": {"source": {"text": FIXTURE_SOURCE}}}}
        source, _ = _source(
            *_baseline_responses(source_payload, baseline),
            _response(_page(newer, older, *baseline)),
            _response(_page(newer, older, *baseline)),
        )

        assert_poll_contract(
            self,
            source,
            PollScenario(
                source_texts={"fixture.source.text": FIXTURE_SOURCE},
                changes=(_expected_change(older), _expected_change(newer)),
                baseline_scanned=5,
                changed_scanned=7,
                unchanged_scanned=7,
            ),
        )

    def test_poll_recovers_complete_history_with_a_single_id_cursor(self) -> None:
        baseline = _records("fixture.baseline", 5)
        backlog = _records("fixture.backlog", 24, start=1784300100000)
        source_payload = {"en": {"fixture": {"source": "Source"}}}
        source, _ = _source(
            *_baseline_responses(source_payload, baseline),
            _response(_page(*backlog[:5], total_pages=20)),
            _response(_page(backlog[4], backlog[5], page=2, total_pages=21)),
            *(
                _response(_page(record, page=page, total_pages=21))
                for page, record in enumerate(backlog[6:], start=3)
            ),
            _response(_page(*baseline, page=21, total_pages=21)),
        )

        checkpoint = source.poll(None)
        result = source.poll(checkpoint.cursor)

        self.assertEqual(
            result.changes,
            tuple(_expected_change(record) for record in reversed(backlog)),
        )
        self.assertEqual(
            result.cursor,
            {
                "type": "noctalia-web",
                "project": "noctalia",
                "id": _change_id(backlog[0]),
            },
        )
        self.assertIsNone(result.source_texts)

    def test_poll_skips_an_empty_change_while_recovering(self) -> None:
        baseline = _records("fixture.baseline", 3)
        ahead = _records("fixture.ahead", 2, start=1784300100000)
        empty: _Record = (
            "fixture.empty",
            "pt-BR",
            "fixture-editor",
            1784300005000,
            "",
            "",
        )
        source_payload = {"en": {"fixture": {"source": "Source"}}}
        source, _ = _source(
            *_baseline_responses(source_payload, baseline),
            _response(_page(*ahead, empty, *baseline, total_pages=2)),
            _response(_page(*baseline, page=2, total_pages=2)),
        )

        checkpoint = source.poll(None)
        result = source.poll(checkpoint.cursor)

        self.assertEqual(
            result.changes,
            tuple(_expected_change(record) for record in reversed(ahead)),
        )

    def test_empty_history_collects_the_first_changes(self) -> None:
        added = _records(
            "fixture.first",
            2,
            start=1784300010000,
            locale="fr",
        )
        source_payload = {"en": {"fixture": {"first": "First translation"}}}
        source, _ = _source(
            _response(_page(total_pages=0)),
            _response(source_payload, export=True),
            _response(_page(*added)),
            _response(_page(*added)),
        )

        assert_poll_contract(
            self,
            source,
            PollScenario(
                source_texts={"fixture.first": "First translation"},
                changes=tuple(_expected_change(record) for record in reversed(added)),
                baseline_scanned=0,
                changed_scanned=2,
                unchanged_scanned=2,
            ),
        )

    def test_invalid_or_expired_cursor_never_rebuilds_the_baseline(self) -> None:
        source, _ = _source(*_baseline_responses({"en": {"key": "English"}}))
        with self.assertRaisesRegex(RuntimeError, "another source"):
            source.poll({"foreign": "cursor"})

        cursor = source.poll(None).cursor
        other_project, _ = _source(project="official-plugins")
        with self.assertRaisesRegex(RuntimeError, "another project"):
            other_project.poll(cursor)

        responses = (
            *_baseline_responses({"en": {"key": "English"}}),
            _response(_page(*_records("new-page-one", 5), total_pages=2)),
            _response(_page(*_records("new-page-two", 5), page=2, total_pages=2)),
        )
        source, _ = _source(*responses)
        cursor = source.poll(None).cursor
        with self.assertRaisesRegex(RuntimeError, "complete available history"):
            source.poll(cursor)

    def test_poll_rejects_invalid_source_exports(self) -> None:
        cases: tuple[tuple[object, str], ...] = (
            ({"de": {}}, "does not contain 'en'"),
            ({"en": {"settings": {"enabled": False}}}, "non-string"),
            ({"en": {}}, "empty mapping"),
            ({"en": {"": "value"}}, "invalid mapping key"),
            (
                {"en": {"settings": {"label": "nested"}, "settings.label": "flat"}},
                "duplicate key",
            ),
        )
        for payload, error in cases:
            source, _ = _source(*_baseline_responses(payload))
            with self.subTest(error=error), self.assertRaisesRegex(RuntimeError, error):
                source.poll(None)

        invalid_json = requests.Response()
        invalid_json.status_code = 200
        invalid_json._content = b"{"  # pyright: ignore[reportPrivateUsage]
        source, _ = _source(
            _response(_page(*_records("base", 5))),
            invalid_json,
        )
        with self.assertRaisesRegex(RuntimeError, "not valid JSON"):
            source.poll(None)

    def test_construction_validates_inputs_without_network_access(self) -> None:
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.proxies = {}
        with patch(
            "noctalia_i18n_core.sources.noctalia.requests.Session",
            return_value=session,
        ):
            source = NoctaliaSource("official-plugins", 30)

        session.get.assert_not_called()
        source.close()
        session.close.assert_called_once_with()

        invalid: tuple[Callable[[], NoctaliaSource], ...] = (
            lambda: NoctaliaSource("INVALID", 30),
            lambda: NoctaliaSource("-noctalia", 30),
            lambda: NoctaliaSource("noctalia", 0),
            lambda: NoctaliaSource("noctalia", float("nan")),
        )
        for build in invalid:
            with self.subTest(build=build), self.assertRaises(ValueError):
                build()

    def test_http_boundary_preserves_the_caller_session(self) -> None:
        proxy = "http://proxy.invalid:8080"
        project = "official-plugins"
        record = _records("fixture.project", 1)[0]
        session = _session(_response(_page(record, project=project), project=project))
        session.trust_env = True
        session.proxies = {"http": proxy, "https": proxy}
        source = NoctaliaSource(project, 30, session)

        source.history(1)

        self.assertTrue(session.trust_env)
        self.assertEqual(session.proxies, {"http": proxy, "https": proxy})
        session.get.assert_called_once_with(
            "https://i18n.noctalia.dev/projects/official-plugins/__data.json",
            params={"changesPage": 1},
            headers={"Accept": "application/json"},
            timeout=30,
        )
        with self.assertRaisesRegex(ValueError, "at least 1"):
            source.history(0)
        with self.assertRaisesRegex(ValueError, "at least 1"):
            source.history(True)
        source.close()
        session.close.assert_not_called()

    def test_project_selects_its_export_endpoint(self) -> None:
        project = "community-plugins"
        responses = _baseline_responses(
            {"en": {"color_picker": {"label": "Color Picker"}}},
            project=project,
        )
        source, session = _source(*responses, project=project)

        result = source.poll(None)

        self.assertEqual(result.source_texts, {"color_picker.label": "Color Picker"})
        session.get.assert_called_with(
            "https://i18n.noctalia.dev/api/projects/community-plugins/pull",
            headers={"Accept": "application/json"},
            timeout=30,
        )

    def test_network_failures_are_stable_and_hide_credentials(self) -> None:
        error = requests.ConnectionError(
            "proxy http://user:SECRET@proxy.invalid failed"
        )
        source, _ = _source(error)

        with self.assertRaises(RuntimeError) as raised:
            source.history(1)

        self.assertEqual(
            str(raised.exception),
            "Cannot retrieve Recent Changes: network failure",
        )
