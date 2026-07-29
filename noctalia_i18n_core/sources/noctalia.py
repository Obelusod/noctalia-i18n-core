"""Noctalia Translate change source."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from urllib.parse import quote, urlencode

import requests

from ..models import (
    SOURCE_LOCALE,
    Action,
    Change,
    JsonValue,
    PollResult,
    normalize_json,
)

_BASE_URL = "https://i18n.noctalia.dev"
_CURSOR_TYPE = "noctalia-web"
_PROJECT_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _object(value: JsonValue, label: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} is not an object")
    return value


def _array(value: JsonValue, label: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise RuntimeError(f"{label} is not an array")
    return value


def _reference(
    values: list[JsonValue],
    reference: JsonValue,
    label: str,
) -> JsonValue:
    if type(reference) is not int or not 0 <= reference < len(values):
        raise RuntimeError(f"{label} contains an invalid reference")
    return values[reference]


def _field(
    values: list[JsonValue],
    record: dict[str, JsonValue],
    name: str,
    label: str,
) -> JsonValue:
    try:
        reference = record[name]
    except KeyError:
        raise RuntimeError(f"{label} is missing {name!r}") from None
    return _reference(values, reference, label)


def _string(value: JsonValue, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{label} is not a non-empty string")
    return value


def _optional_string(value: JsonValue, label: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise RuntimeError(f"{label} is not a string or null")
    return value


def _integer(value: JsonValue, label: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise RuntimeError(f"{label} is not an integer of at least {minimum}")
    return value


def _timestamp(value: JsonValue) -> datetime:
    milliseconds = _integer(value, "Recent Changes created_at", minimum=0)
    try:
        seconds, remainder = divmod(milliseconds, 1000)
        return datetime.fromtimestamp(seconds, UTC) + timedelta(milliseconds=remainder)
    except (OSError, OverflowError) as exc:
        raise RuntimeError("Recent Changes created_at is out of range") from exc


def _change_values(
    old_value: str | None,
    new_value: str | None,
) -> tuple[Action, str | None, str | None]:
    if old_value in {None, ""}:
        if not new_value:
            raise RuntimeError("Recent Changes contains an empty change")
        return "added", None, new_value
    if new_value in {None, ""}:
        return "deleted", old_value, None
    return "modified", old_value, new_value


def _github_urls(login: str | None) -> tuple[str | None, str | None]:
    if login is None:
        return None, None
    path = quote(login, safe="")
    return f"https://github.com/{path}", f"https://github.com/{path}.png"


def _page_values(payload: JsonValue) -> list[JsonValue]:
    root = _object(payload, "Recent Changes response")
    if root.get("type") != "data":
        raise RuntimeError("Recent Changes response has an unexpected type")
    nodes = _array(root.get("nodes"), "Recent Changes nodes")
    if not nodes:
        raise RuntimeError("Recent Changes nodes are empty")
    node = _object(nodes[-1], "Recent Changes page node")
    if node.get("type") != "data":
        raise RuntimeError("Recent Changes page node has an unexpected type")
    values = _array(node.get("data"), "Recent Changes page data")
    if not values:
        raise RuntimeError("Recent Changes page data is empty")
    return values


@dataclass(frozen=True, slots=True)
class _Page:
    changes: tuple[Change, ...]
    number: int
    total_pages: int


def _parse_page(payload: JsonValue, project: str) -> _Page:
    values = _page_values(payload)
    root = _object(values[0], "Recent Changes page data")
    project_data = _object(
        _field(values, root, "project", "Recent Changes project"),
        "Recent Changes project",
    )
    project_id = _string(
        _field(values, project_data, "id", "Recent Changes project"),
        "Recent Changes project ID",
    )
    slug = _string(
        _field(values, project_data, "slug", "Recent Changes project"),
        "Recent Changes project slug",
    )
    if slug != project:
        raise RuntimeError("Recent Changes returned an unexpected project")
    if _field(values, root, "changesLocale", "Recent Changes locale") != "":
        raise RuntimeError("Recent Changes returned a filtered history")

    number = _integer(
        _field(values, root, "changesPage", "Recent Changes page"),
        "Recent Changes page",
        minimum=1,
    )
    total_pages = _integer(
        _field(values, root, "changesTotalPages", "Recent Changes page count"),
        "Recent Changes page count",
        minimum=0,
    )
    records = _array(
        _field(values, root, "recentChanges", "Recent Changes records"),
        "Recent Changes records",
    )

    changes: list[Change] = []
    identifiers: set[str] = set()
    for index, reference in enumerate(records):
        label = f"Recent Changes record {index}"
        record = _object(_reference(values, reference, label), label)
        change_id = _string(_field(values, record, "id", label), f"{label} ID")
        if change_id in identifiers:
            raise RuntimeError(f"Recent Changes contains duplicate ID {change_id!r}")
        identifiers.add(change_id)
        if _field(values, record, "project_id", label) != project_id:
            raise RuntimeError(f"{label} belongs to another project")

        key = _string(
            _field(values, record, "translation_key", label),
            f"{label} key",
        )
        locale = _string(_field(values, record, "locale", label), f"{label} locale")
        actor_login = (
            _optional_string(
                _field(values, record, "created_by_login", label),
                f"{label} actor",
            )
            or None
        )
        old_value = _optional_string(
            _field(values, record, "text", label),
            f"{label} old value",
        )
        new_value = _optional_string(
            _field(values, record, "new_text", label),
            f"{label} new value",
        )
        action, old_value, new_value = _change_values(old_value, new_value)
        actor_url, actor_avatar_url = _github_urls(actor_login)
        url = (
            f"{_BASE_URL}/projects/{project}/translate?"
            f"{urlencode({'search': key, 'locale': locale})}"
        )
        try:
            changes.append(
                Change(
                    id=change_id,
                    key=key,
                    locale=locale,
                    actor=actor_login or "API",
                    old_value=old_value,
                    new_value=new_value,
                    action=action,
                    occurred_at=_timestamp(_field(values, record, "created_at", label)),
                    url=url,
                    actor_url=actor_url,
                    actor_avatar_url=actor_avatar_url,
                )
            )
        except ValueError as exc:
            raise RuntimeError(f"{label} is invalid: {exc}") from exc

    if any(newer.occurred_at < older.occurred_at for newer, older in pairwise(changes)):
        raise RuntimeError("Recent Changes is not ordered newest first")
    return _Page(tuple(changes), number, total_pages)


def _flatten(value: JsonValue, prefix: tuple[str, ...] = ()) -> dict[str, str]:
    if isinstance(value, dict):
        if not value:
            location = ".".join(prefix) or "<root>"
            raise RuntimeError(
                f"Translation export contains an empty mapping at {location!r}"
            )
        output: dict[str, str] = {}
        for key, child in value.items():
            if not key:
                raise RuntimeError("Translation export contains an invalid mapping key")
            flattened = _flatten(child, (*prefix, key))
            if duplicates := output.keys() & flattened.keys():
                duplicate = min(duplicates)
                raise RuntimeError(
                    f"Translation export contains a duplicate key: {duplicate!r}"
                )
            output.update(flattened)
        return output
    if not isinstance(value, str):
        raise RuntimeError(
            f"Translation export contains a non-string value at {'.'.join(prefix)!r}"
        )
    return {".".join(prefix): value}


def _request_error(resource: str, error: requests.RequestException) -> RuntimeError:
    response = error.response
    detail = (
        f"HTTP {response.status_code}" if response is not None else "network failure"
    )
    return RuntimeError(f"Cannot retrieve {resource}: {detail}")


class NoctaliaSource:
    """Acquire changes and source text from Noctalia Translate."""

    def __init__(
        self,
        project: str,
        timeout: float,
        session: requests.Session | None = None,
    ) -> None:
        if type(project) is not str or not _PROJECT_PATTERN.fullmatch(project):
            raise ValueError("project must be a lowercase project identifier")
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a positive finite number")
        self._project = project
        self._timeout = timeout
        self._data_url = f"{_BASE_URL}/projects/{project}/__data.json"
        self._export_url = f"{_BASE_URL}/api/projects/{project}/pull"
        self._owns_session = session is None
        self._session = session or requests.Session()

    def poll(self, cursor: JsonValue | None, /) -> PollResult:
        """Collect changes newer than an opaque source cursor."""

        previous = None if cursor is None else self._parse_cursor(cursor)
        first = self._fetch_page(1)
        next_cursor: dict[str, JsonValue] = {
            "type": _CURSOR_TYPE,
            "project": self._project,
            "id": first.changes[0].id if first.changes else "",
        }
        if previous is None:
            return self._poll_result(
                (),
                next_cursor,
                len(first.changes),
                source_texts=self._fetch_source_texts(),
            )

        collected: list[Change] = []
        known: set[str] = set()
        scanned = 0
        found = False
        current = first
        page = 1
        total_pages = first.total_pages
        while page <= total_pages:
            scanned += len(current.changes)
            for change in current.changes:
                if change.id in known:
                    continue
                known.add(change.id)
                if change.id == previous:
                    found = True
                    break
                collected.append(change)
            if found:
                break
            page += 1
            if page <= total_pages:
                current = self._fetch_page(page)
                total_pages = max(total_pages, current.total_pages)

        if not previous:
            found = True
        if not found:
            raise RuntimeError(
                "Previous cursor was not found in the complete available history "
                f"of {total_pages} pages. No state was advanced. "
                "Reset the baseline only if older history is no longer available."
            )
        return self._poll_result(
            tuple(reversed(collected)),
            next_cursor,
            scanned,
            source_texts=None,
        )

    def history(self, page: int, /) -> tuple[Change, ...]:
        """Fetch one Recent Changes page."""

        if type(page) is not int or page < 1:
            raise ValueError("page number must be at least 1")
        return self._fetch_page(page).changes

    def close(self) -> None:
        """Close the session created by this source, if any."""

        if self._owns_session:
            self._session.close()

    def _fetch_page(self, number: int) -> _Page:
        try:
            response = self._session.get(
                self._data_url,
                params={"changesPage": number},
                headers={"Accept": "application/json"},
                timeout=self._timeout,
            )
            response.raise_for_status()
            raw: object = response.json()
        except requests.JSONDecodeError:
            raise RuntimeError("Recent Changes is not valid JSON") from None
        except requests.RequestException as exc:
            raise _request_error("Recent Changes", exc) from None
        try:
            payload = normalize_json(raw, "Recent Changes")
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        parsed = _parse_page(payload, self._project)
        if parsed.number != number:
            raise RuntimeError("Recent Changes returned an unexpected page")
        if (number <= parsed.total_pages) != bool(parsed.changes):
            raise RuntimeError(
                "Recent Changes records do not match the reported page count"
            )
        return parsed

    def _fetch_source_texts(self) -> dict[str, str]:
        try:
            response = self._session.get(
                self._export_url,
                headers={"Accept": "application/json"},
                timeout=self._timeout,
            )
            response.raise_for_status()
            raw: object = response.json()
        except requests.JSONDecodeError:
            raise RuntimeError("Translation export is not valid JSON") from None
        except requests.RequestException as exc:
            raise _request_error("the translation export", exc) from None
        try:
            payload = normalize_json(raw, "Translation export")
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if not isinstance(payload, dict) or SOURCE_LOCALE not in payload:
            raise RuntimeError(f"Translation export does not contain {SOURCE_LOCALE!r}")
        source = payload[SOURCE_LOCALE]
        if not isinstance(source, dict):
            raise RuntimeError(f"Invalid translation data for {SOURCE_LOCALE!r}")
        return _flatten(source)

    @staticmethod
    def _poll_result(
        changes: tuple[Change, ...],
        cursor: dict[str, JsonValue],
        scanned: int,
        *,
        source_texts: dict[str, str] | None,
    ) -> PollResult:
        try:
            return PollResult(changes, cursor, scanned, source_texts)
        except ValueError as exc:
            raise RuntimeError(
                f"Recent Changes produced an invalid poll result: {exc}"
            ) from exc

    def _parse_cursor(self, cursor: JsonValue) -> str:
        if not isinstance(cursor, dict) or cursor.get("type") != _CURSOR_TYPE:
            raise RuntimeError(
                "Stored cursor belongs to another source; reset baseline"
            )
        if cursor.get("project") != self._project:
            raise RuntimeError(
                "Stored cursor belongs to another project; reset baseline"
            )
        change_id = cursor.get("id")
        if not isinstance(change_id, str):
            raise RuntimeError("Stored cursor is invalid")
        return change_id
