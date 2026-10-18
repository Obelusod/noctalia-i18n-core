"""Read Noctalia Translate project data."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import batched, pairwise
from types import TracebackType
from typing import Self, TypeGuard
from urllib.parse import quote, urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from .models import (
    SOURCE_LOCALE,
    Change,
    ChangeAction,
    ChangePage,
    Locale,
    Missing,
    Project,
    Review,
)

_BASE_URL = "https://i18n.noctalia.dev"
_RETRYABLE_METHODS = frozenset({"GET", "POST"})
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_TRANSLATION_BATCH_SIZE = 50
type _JsonValue = (
    None | bool | int | float | str | list[_JsonValue] | dict[str, _JsonValue]
)


@dataclass(frozen=True, slots=True)
class _ParsedChangePage:
    number: int
    total_pages: int
    records: tuple[tuple[str, datetime, Change | None], ...]

    @property
    def cursor(self) -> str | None:
        return self.records[-1][0] if self.records else None

    def to_page(
        self, records: tuple[tuple[str, datetime, Change | None], ...]
    ) -> ChangePage:
        return ChangePage(
            number=self.number,
            total_pages=self.total_pages,
            changes=tuple(change for _, _, change in records if change is not None),
            cursor=self.cursor,
        )


def _is_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _normalize(value: object, label: str) -> _JsonValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} contains a non-finite number")
        return value
    if _is_list(value):
        return [
            _normalize(item, f"{label}[{index}]") for index, item in enumerate(value)
        ]
    if _is_dict(value):
        output: dict[str, _JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{label} must use string mapping keys")
            output[key] = _normalize(item, f"{label}.{key}")
        return output
    raise ValueError(f"{label} contains a non-JSON value")


def _object(value: _JsonValue, label: str) -> dict[str, _JsonValue]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} is not an object")
    return value


def _array(value: _JsonValue, label: str) -> list[_JsonValue]:
    if not isinstance(value, list):
        raise RuntimeError(f"{label} is not an array")
    return value


def _member(record: dict[str, _JsonValue], name: str, label: str) -> _JsonValue:
    try:
        return record[name]
    except KeyError:
        raise RuntimeError(f"{label} is missing {name!r}") from None


def _reference(
    values: list[_JsonValue], reference: _JsonValue, label: str
) -> _JsonValue:
    if type(reference) is not int or not 0 <= reference < len(values):
        raise RuntimeError(f"{label} contains an invalid reference")
    return values[reference]


def _string(value: _JsonValue, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label} is not a non-empty string")
    return value


def _optional_string(value: _JsonValue, label: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise RuntimeError(f"{label} is not a string or null")
    return value


def _integer(value: _JsonValue, label: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise RuntimeError(f"{label} is not an integer of at least {minimum}")
    return value


def _boundary(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return value.astimezone(UTC)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _keys(values: tuple[object, ...], label: str) -> tuple[str, ...]:
    validated = (_text(value, f"{label} item") for value in values)
    return tuple(dict.fromkeys(validated))


def _locales(value: Iterable[str] | None, label: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        raise ValueError(f"{label} must be an iterable of locale identifiers")
    selected = tuple(dict.fromkeys(_text(locale, f"{label} item") for locale in value))
    if not selected:
        raise ValueError(f"{label} must not be empty")
    return selected


def _timestamp(value: _JsonValue, label: str) -> datetime:
    milliseconds = _integer(value, f"{label} created_at", minimum=0)
    try:
        seconds, remainder = divmod(milliseconds, 1000)
        return datetime.fromtimestamp(seconds, UTC) + timedelta(milliseconds=remainder)
    except (OSError, OverflowError) as exc:
        raise RuntimeError(f"{label} created_at is out of range") from exc


def _change_values(
    old_value: str | None, new_value: str | None
) -> tuple[ChangeAction, str | None, str | None] | None:
    if old_value in {None, ""}:
        return None if not new_value else ("added", None, new_value)
    if new_value in {None, ""}:
        return "deleted", old_value, None
    return "modified", old_value, new_value


def _translation_url(project: str, key: str, locale: str) -> str:
    return (
        f"{_BASE_URL}/projects/{quote(project, safe='')}/translate?"
        f"{urlencode({'search': key, 'locale': locale})}"
    )


def _change(
    *,
    project: str,
    change_id: str,
    key: str,
    locale: str,
    actor: str,
    old_value: str | None,
    new_value: str | None,
    occurred_at: datetime,
    label: str,
) -> Change | None:
    values = _change_values(old_value, new_value)
    if values is None:
        return None
    action, old_value, new_value = values
    try:
        return Change(
            id=change_id,
            key=key,
            locale=locale,
            action=action,
            actor=actor,
            old_value=old_value,
            new_value=new_value,
            occurred_at=occurred_at,
            translation_url=_translation_url(project, key, locale),
        )
    except ValueError as exc:
        raise RuntimeError(f"{label} is invalid: {exc}") from exc


def _create_session() -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(
        max_retries=Retry(
            total=3,
            allowed_methods=_RETRYABLE_METHODS,
            status_forcelist=_RETRYABLE_STATUSES,
            backoff_factor=0.5,
            raise_on_status=False,
        )
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _request_json(
    session: requests.Session,
    url: str,
    *,
    resource: str,
    timeout: float,
    parameters: dict[str, int | str] | None = None,
    body: dict[str, list[str]] | None = None,
) -> _JsonValue:
    try:
        if body is None:
            response = session.get(
                url,
                params=parameters,
                headers={"Accept": "application/json"},
                timeout=timeout,
            )
        else:
            response = session.post(
                url,
                json=body,
                headers={"Accept": "application/json"},
                timeout=timeout,
            )
        response.raise_for_status()
        raw: object = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{resource} is not valid JSON") from exc
    except requests.RequestException as exc:
        response = exc.response
        if response is not None:
            detail = f"HTTP {response.status_code}"
        elif isinstance(exc, requests.Timeout):
            detail = "request timed out"
        elif isinstance(exc, requests.ConnectionError):
            detail = "connection failed"
        else:
            detail = "network failure"
        raise RuntimeError(f"Cannot retrieve {resource}: {detail}") from exc
    try:
        return _normalize(raw, resource)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


def _page_values(payload: _JsonValue, resource: str) -> list[_JsonValue]:
    root = _object(payload, resource)
    if root.get("type") != "data":
        raise RuntimeError(f"{resource} has an unexpected type")
    nodes = _array(_member(root, "nodes", resource), f"{resource} nodes")
    if not nodes:
        raise RuntimeError(f"{resource} nodes are empty")
    node = _object(nodes[-1], f"{resource} page node")
    if node.get("type") != "data":
        raise RuntimeError(f"{resource} page node has an unexpected type")
    values = _array(_member(node, "data", resource), f"{resource} page data")
    if not values:
        raise RuntimeError(f"{resource} page data is empty")
    return values


def _expand_indexed(
    value: _JsonValue, values: list[_JsonValue], label: str
) -> _JsonValue:
    if isinstance(value, dict):
        mapping_output: dict[str, _JsonValue] = {}
        for name, child in value.items():
            if type(child) is int:
                child = _reference(values, child, f"{label}.{name}")
            mapping_output[name] = _expand_indexed(child, values, f"{label}.{name}")
        return mapping_output
    if isinstance(value, list):
        list_output: list[_JsonValue] = []
        for index, child in enumerate(value):
            if type(child) is int:
                child = _reference(values, child, f"{label}[{index}]")
            list_output.append(_expand_indexed(child, values, f"{label}[{index}]"))
        return list_output
    return value


def _page_object(payload: _JsonValue, resource: str) -> dict[str, _JsonValue]:
    values = _page_values(payload, resource)
    return _object(_expand_indexed(values[0], values, resource), resource)


def _validate_project(
    root: dict[str, _JsonValue], project: str, resource: str
) -> dict[str, _JsonValue]:
    data = _object(_member(root, "project", resource), f"{resource} project")
    slug = _string(_member(data, "slug", resource), f"{resource} project slug")
    if slug != project:
        raise RuntimeError(f"{resource} returned an unexpected project")
    return data


def _parse_project(payload: _JsonValue, project: str) -> Project:
    resource = "Project metadata"
    root = _page_object(payload, resource)
    data = _validate_project(root, project, resource)
    name = _string(_member(data, "name", resource), f"{resource} project name")
    description = _optional_string(
        _member(data, "description", resource), f"{resource} project description"
    )
    context = _optional_string(
        _member(data, "ai_context", resource),
        f"{resource} project translation context",
    )
    stats = _object(_member(root, "stats", resource), f"{resource} statistics")
    key_count = _integer(
        _member(stats, "totalKeys", resource), f"{resource} key count", minimum=0
    )
    values = _array(_member(root, "locales", resource), f"{resource} locales")
    missing = _object(
        _member(root, "missingByLocale", resource), f"{resource} missing counts"
    )
    review = _object(
        _member(root, "outdatedByLocale", resource), f"{resource} review counts"
    )
    locales: list[Locale] = []
    codes: set[str] = set()
    for index, item in enumerate(values):
        code = _string(item, f"Project locale {index}")
        if code in codes:
            raise RuntimeError(f"Project metadata contains duplicate locale {code!r}")
        codes.add(code)
        missing_count = _integer(
            _member(missing, code, resource),
            f"{resource} {code!r} missing count",
            minimum=0,
        )
        if missing_count > key_count:
            raise RuntimeError(f"{resource} {code!r} missing count exceeds key count")
        review_count = _integer(
            review.get(code, 0), f"{resource} {code!r} review count", minimum=0
        )
        try:
            locales.append(
                Locale(
                    code=code,
                    translation_count=key_count - missing_count,
                    missing_count=missing_count,
                    review_count=review_count,
                )
            )
        except ValueError as exc:
            raise RuntimeError(f"{resource} {code!r} is invalid: {exc}") from exc
    try:
        return Project(
            slug=project,
            name=name,
            description=description or None,
            translation_context=context or None,
            key_count=key_count,
            locales=tuple(locales),
        )
    except ValueError as exc:
        raise RuntimeError(f"{resource} is invalid: {exc}") from exc


def _tree_keys(value: _JsonValue) -> list[str]:
    output: list[str] = []

    def visit(node: _JsonValue, path: str) -> int:
        record = _object(node, f"Key tree node {path or '<root>'!r}")
        count = _integer(
            _member(record, "_count", "Key tree node"),
            f"Key tree node {path or '<root>'!r} count",
            minimum=0,
        )
        is_leaf = record.get("_isLeaf")
        children = tuple(
            (segment, child)
            for segment, child in record.items()
            if segment not in {"_count", "_isLeaf"}
        )
        if count == 0:
            if path or is_leaf is not None or children:
                raise RuntimeError("Key tree contains an invalid empty node")
            return 0
        if is_leaf is True:
            if not path or count != 1 or children:
                raise RuntimeError(f"Key tree leaf {path!r} is invalid")
            output.append(path)
            return 1
        if is_leaf is not None or not children:
            raise RuntimeError(f"Key tree branch {path or '<root>'!r} is invalid")
        actual = 0
        for segment, child in children:
            if not segment or "." in segment:
                raise RuntimeError(f"Key tree contains invalid segment {segment!r}")
            child_path = f"{path}.{segment}" if path else segment
            actual += visit(child, child_path)
        if actual != count:
            raise RuntimeError(f"Key tree node {path or '<root>'!r} count is invalid")
        return actual

    visit(value, "")
    return output


def _parse_keys(payload: _JsonValue, project: str) -> tuple[str, ...]:
    resource = "Key tree"
    root = _page_object(payload, resource)
    _validate_project(root, project, resource)
    keys = _tree_keys(_member(root, "tree", resource))
    total = _integer(_member(root, "totalKeys", resource), "Key tree total", minimum=0)
    if len(keys) != total:
        raise RuntimeError("Key tree total does not match its keys")
    return tuple(keys)


def _flatten(value: _JsonValue, path: str = "") -> dict[str, str]:
    output: dict[str, str] = {}
    if isinstance(value, dict):
        if not value:
            raise RuntimeError(
                f"Translation export contains an empty mapping at {path or '<root>'!r}"
            )
        for segment, child in value.items():
            if not segment:
                raise RuntimeError("Translation export contains an empty mapping key")
            output.update(_flatten(child, f"{path}.{segment}" if path else segment))
        return output
    if not isinstance(value, str):
        raise RuntimeError(
            f"Translation export contains a non-string value at {path!r}"
        )
    output[path] = value
    return output


def _parse_translations(
    payload: _JsonValue,
    requested_keys: tuple[str, ...],
    selected_locales: tuple[str, ...] | None,
) -> dict[str, dict[str, str]]:
    resource = "Translations"
    root = _object(payload, resource)
    records = _object(_member(root, "translations", resource), resource)
    output: dict[str, dict[str, str]] = {key: {} for key in requested_keys}
    for key, values in records.items():
        if key not in output:
            raise RuntimeError(f"Translations contains unexpected key {key!r}")
        locale_records = _object(values, f"Translation {key!r}")
        for locale, value in locale_records.items():
            if selected_locales is not None and locale not in selected_locales:
                raise RuntimeError(
                    f"Translation {key!r} contains unexpected locale {locale!r}"
                )
            label = f"Translation {key!r} {locale!r}"
            record = _object(value, label)
            returned_key = _string(
                _member(record, "translation_key", label), f"{label} key"
            )
            returned_locale = _string(
                _member(record, "locale", label), f"{label} locale"
            )
            if returned_key != key or returned_locale != locale:
                raise RuntimeError(f"{label} contains inconsistent identifiers")
            text = _string(_member(record, "text", label), f"{label} text")
            output[key][locale] = text
    if selected_locales is None:
        return output
    return {
        key: {
            locale: output[key][locale]
            for locale in selected_locales
            if locale in output[key]
        }
        for key in requested_keys
    }


def _parse_search_page(
    payload: _JsonValue,
    project: str,
    query: str,
    search_locale: str,
    requested_page: int,
) -> tuple[tuple[str, ...], int, int]:
    resource = "Translation search"
    root = _page_object(payload, resource)
    _validate_project(root, project, resource)
    returned_query = _string(_member(root, "search", resource), f"{resource} query")
    returned_locale = _string(
        _member(root, "initialLocale", resource), f"{resource} locale"
    )
    if returned_query != query:
        raise RuntimeError("Translation search did not apply the requested query")
    if returned_locale != search_locale:
        raise RuntimeError(f"Translation search did not apply locale {search_locale!r}")
    page = _integer(_member(root, "page", resource), f"{resource} page", minimum=1)
    total_pages = _integer(
        _member(root, "totalPages", resource),
        f"{resource} page count",
        minimum=1,
    )
    if page != requested_page or total_pages < page:
        raise RuntimeError("Translation search returned invalid pagination")
    values = _array(_member(root, "keys", resource), f"{resource} keys")
    keys = tuple(
        _string(value, f"{resource} key {index}") for index, value in enumerate(values)
    )
    if len(keys) != len(set(keys)):
        raise RuntimeError("Translation search contains duplicate keys")
    return keys, page, total_pages


def _parse_missing(payload: _JsonValue) -> tuple[str, ...]:
    resource = "Missing translations"
    root = _object(payload, resource)
    values = _array(_member(root, "keys", resource), f"{resource} keys")
    keys = tuple(
        _string(value, f"{resource} key {index}") for index, value in enumerate(values)
    )
    count = _integer(_member(root, "count", resource), f"{resource} count", minimum=0)
    if len(keys) != count or len(keys) != len(set(keys)):
        raise RuntimeError(f"{resource} count does not match its unique keys")
    return keys


def _parse_review_sources(
    payload: _JsonValue,
) -> tuple[tuple[str, str | None, str], ...]:
    resource = "Translations needing review"
    root = _object(payload, resource)
    values = _array(_member(root, "items", resource), f"{resource} items")
    output: list[tuple[str, str | None, str]] = []
    for index, value in enumerate(values):
        label = f"{resource} item {index}"
        record = _object(value, label)
        key = _string(_member(record, "key", label), f"{label} key")
        output.append(
            (
                key,
                _optional_string(_member(record, "enOld", label), f"{label} old source")
                or None,
                _string(_member(record, "enNew", label), f"{label} new source"),
            )
        )
    count = _integer(_member(root, "count", resource), f"{resource} count", minimum=0)
    keys = tuple(item[0] for item in output)
    if len(output) != count or len(keys) != len(set(keys)):
        raise RuntimeError(f"{resource} count does not match its unique items")
    return tuple(output)


def _parse_changes(
    payload: _JsonValue, project: str, key: str, locale: str
) -> tuple[Change, ...]:
    resource = "Translation changes"
    root = _object(payload, resource)
    records = _array(_member(root, "history", resource), f"{resource} records")
    output: list[Change] = []
    identifiers: set[str] = set()
    for index, item in enumerate(records):
        label = f"{resource} record {index}"
        record = _object(item, label)
        change_id = _string(_member(record, "id", label), f"{label} ID")
        if change_id in identifiers:
            raise RuntimeError(f"{resource} contains duplicate ID {change_id!r}")
        identifiers.add(change_id)
        record_key = _string(_member(record, "translation_key", label), f"{label} key")
        if record_key != key:
            raise RuntimeError(f"{label} belongs to another key")
        record_locale = _string(_member(record, "locale", label), f"{label} locale")
        if record_locale != locale:
            raise RuntimeError(f"{label} belongs to another locale")
        change = _change(
            project=project,
            change_id=change_id,
            key=record_key,
            locale=record_locale,
            actor=(
                _optional_string(
                    _member(record, "created_by_login", label), f"{label} actor"
                )
                or "API"
            ),
            old_value=_optional_string(
                _member(record, "text", label), f"{label} old value"
            ),
            new_value=_optional_string(
                _member(record, "new_text", label), f"{label} new value"
            ),
            occurred_at=_timestamp(_member(record, "created_at", label), label),
            label=label,
        )
        if change is not None:
            output.append(change)
    if any(newer.occurred_at < older.occurred_at for newer, older in pairwise(output)):
        raise RuntimeError(f"{resource} is not ordered newest first")
    return tuple(output)


def _parse_change_page(
    payload: _JsonValue, project: str, locale_filter: str | None
) -> _ParsedChangePage:
    resource = "Recent Changes"
    root = _page_object(payload, resource)
    project_data = _validate_project(root, project, resource)
    project_id = _string(
        _member(project_data, "id", resource), f"{resource} project ID"
    )
    returned_locale = _member(root, "changesLocale", resource)
    if not isinstance(returned_locale, str):
        raise RuntimeError(f"{resource} locale filter is not a string")
    if returned_locale != (locale_filter or ""):
        if locale_filter is None:
            raise RuntimeError("Recent Changes unexpectedly applied a locale filter")
        raise RuntimeError(f"Recent Changes did not apply locale {locale_filter!r}")
    number = _integer(
        _member(root, "changesPage", resource), f"{resource} page", minimum=1
    )
    total_pages = _integer(
        _member(root, "changesTotalPages", resource),
        f"{resource} page count",
        minimum=0,
    )
    records = _array(_member(root, "recentChanges", resource), f"{resource} records")
    output: list[tuple[str, datetime, Change | None]] = []
    identifiers: set[str] = set()
    for index, item in enumerate(records):
        label = f"{resource} record {index}"
        record = _object(item, label)
        change_id = _string(_member(record, "id", label), f"{label} ID")
        if change_id in identifiers:
            raise RuntimeError(f"{resource} contains duplicate ID {change_id!r}")
        identifiers.add(change_id)
        if _member(record, "project_id", label) != project_id:
            raise RuntimeError(f"{label} belongs to another project")
        key = _string(_member(record, "translation_key", label), f"{label} key")
        record_locale = _string(_member(record, "locale", label), f"{label} locale")
        if locale_filter is not None and record_locale != locale_filter:
            raise RuntimeError(f"{label} belongs to another locale")
        actor = _optional_string(
            _member(record, "created_by_login", label), f"{label} actor"
        )
        occurred_at = _timestamp(_member(record, "created_at", label), label)
        change = _change(
            project=project,
            change_id=change_id,
            key=key,
            locale=record_locale,
            actor=actor or "API",
            old_value=_optional_string(
                _member(record, "text", label), f"{label} old value"
            ),
            new_value=_optional_string(
                _member(record, "new_text", label), f"{label} new value"
            ),
            occurred_at=occurred_at,
            label=label,
        )
        output.append((change_id, occurred_at, change))
    if any(newer[1] < older[1] for newer, older in pairwise(output)):
        raise RuntimeError(f"{resource} is not ordered newest first")
    return _ParsedChangePage(
        number=number,
        total_pages=total_pages,
        records=tuple(output),
    )


class NoctaliaClient:
    """Read one project from Noctalia Translate."""

    def __init__(
        self,
        project: str,
        *,
        timeout: float,
        session: requests.Session | None = None,
    ) -> None:
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a positive finite number")
        self._project = _text(project, "project")
        self._timeout = timeout
        encoded_project = quote(self._project, safe="")
        base = f"{_BASE_URL}/projects/{encoded_project}"
        self._project_url = f"{base}/__data.json"
        self._translate_url = f"{base}/translate/__data.json"
        self._tree_url = f"{base}/tree/__data.json"
        api = f"{_BASE_URL}/api/projects/{encoded_project}"
        self._export_url = f"{api}/pull"
        self._batch_url = f"{api}/translations/batch"
        self._missing_url = f"{api}/missing"
        self._reviews_url = f"{api}/outdated"
        self._changes_url = f"{api}/translations/history"
        self._owns_session = session is None
        self._session = session if session is not None else _create_session()

    @property
    def slug(self) -> str:
        """Return the project identifier."""

        return self._project

    def translation_url(self, key: str, locale: str, /) -> str:
        """Return the website translation page for one key and locale."""

        return _translation_url(
            self._project,
            _text(key, "key"),
            _text(locale, "locale"),
        )

    def project(self) -> Project:
        """Return public project metadata and current progress."""

        payload = _request_json(
            self._session,
            self._project_url,
            resource="Project metadata",
            timeout=self._timeout,
        )
        return _parse_project(payload, self._project)

    def keys(self, *, prefix: str | None = None) -> tuple[str, ...]:
        """Return translation keys, optionally restricted to a hierarchy."""

        if prefix is not None:
            prefix = _text(prefix, "prefix")
        payload = _request_json(
            self._session,
            self._tree_url,
            resource="Key tree",
            timeout=self._timeout,
        )
        keys = _parse_keys(payload, self._project)
        if prefix is None:
            return keys
        boundary = f"{prefix}."
        return tuple(key for key in keys if key == prefix or key.startswith(boundary))

    def catalog(self) -> dict[str, dict[str, str]]:
        """Return the complete current catalog in key-first form."""

        payload = _request_json(
            self._session,
            self._export_url,
            resource="Translation export",
            timeout=self._timeout,
        )
        root = _object(payload, "Translation export")
        if SOURCE_LOCALE not in root:
            raise RuntimeError("Translation export does not contain the source locale")
        output: dict[str, dict[str, str]] = {}
        for locale, tree in root.items():
            if not locale.strip():
                raise RuntimeError("Translation export contains an invalid locale")
            if not isinstance(tree, dict):
                raise RuntimeError(f"Invalid translation data for {locale!r}")
            if not tree:
                continue
            for key, value in _flatten(tree).items():
                output.setdefault(key, {})[locale] = value
        return output

    def translations(
        self,
        key: str,
        /,
        *keys: str,
        locales: Iterable[str] | None = None,
    ) -> dict[str, dict[str, str]]:
        """Return current values for exact keys."""

        selected_keys = _keys((key, *keys), "keys")
        selected_locales = _locales(locales, "locales")
        output: dict[str, dict[str, str]] = {
            selected_key: {} for selected_key in selected_keys
        }
        for batch in batched(selected_keys, _TRANSLATION_BATCH_SIZE):
            body = {"keys": list(batch)}
            if selected_locales is not None:
                body["locales"] = list(selected_locales)
            payload = _request_json(
                self._session,
                self._batch_url,
                resource="Translations response",
                timeout=self._timeout,
                body=body,
            )
            output.update(_parse_translations(payload, batch, selected_locales))
        return output

    def search_translations(
        self,
        query: str,
        *,
        search_locale: str,
        locales: Iterable[str] | None = None,
    ) -> dict[str, dict[str, str]]:
        """Search one language and return current values for matching keys."""

        query = _text(query, "query")
        search_locale = _text(search_locale, "search_locale")
        selected_locales = _locales(locales, "locales")
        page = 1
        found: list[str] = []
        while True:
            payload = _request_json(
                self._session,
                self._translate_url,
                resource="Translation search",
                timeout=self._timeout,
                parameters={
                    "search": query,
                    "locale": search_locale,
                    "page": page,
                },
            )
            keys, current_page, total_pages = _parse_search_page(
                payload, self._project, query, search_locale, page
            )
            for key in keys:
                if key not in found:
                    found.append(key)
            if current_page >= total_pages:
                break
            page = current_page + 1
        if selected_locales is None:
            selected_locales = (search_locale,)
        if not found:
            return {}
        return self.translations(found[0], *found[1:], locales=selected_locales)

    def missing(
        self, locale: str, /, *, query: str | None = None
    ) -> tuple[Missing, ...]:
        """Return source entries without a translation in one locale."""

        locale = _text(locale, "locale")
        parameters: dict[str, int | str] = {"locale": locale}
        if query is not None:
            parameters["search"] = _text(query, "query")
        payload = _request_json(
            self._session,
            self._missing_url,
            resource="Missing translations",
            timeout=self._timeout,
            parameters=parameters,
        )
        keys = _parse_missing(payload)
        if not keys:
            return ()
        translations = self.translations(keys[0], *keys[1:], locales=(SOURCE_LOCALE,))
        output: list[Missing] = []
        for key in keys:
            source = translations[key].get(SOURCE_LOCALE)
            if source is None:
                raise RuntimeError(f"Missing translation {key!r} has no source text")
            output.append(
                Missing(
                    key=key,
                    locale=locale,
                    source=source,
                    translation_url=self.translation_url(key, locale),
                )
            )
        return tuple(output)

    def reviews(
        self, locale: str, /, *, query: str | None = None
    ) -> tuple[Review, ...]:
        """Return translations whose source changed after they were saved."""

        locale = _text(locale, "locale")
        parameters: dict[str, int | str] = {"locale": locale}
        if query is not None:
            parameters["search"] = _text(query, "query")
        payload = _request_json(
            self._session,
            self._reviews_url,
            resource="Translations needing review",
            timeout=self._timeout,
            parameters=parameters,
        )
        sources = _parse_review_sources(payload)
        if not sources:
            return ()
        keys = tuple(item[0] for item in sources)
        translations = self.translations(*keys, locales=(locale,))
        output: list[Review] = []
        for key, old_source, new_source in sources:
            translation = translations[key].get(locale)
            if translation is None:
                raise RuntimeError(
                    f"Translation needing review {key!r} has no {locale!r} translation"
                )
            output.append(
                Review(
                    key=key,
                    locale=locale,
                    old_source=old_source,
                    new_source=new_source,
                    translation=translation,
                    translation_url=self.translation_url(key, locale),
                )
            )
        return tuple(output)

    def changes(
        self,
        key: str,
        /,
        *keys: str,
        locale: str,
    ) -> tuple[Change, ...]:
        """Return complete changes for exact keys in one language."""

        selected_keys = _keys((key, *keys), "keys")
        locale = _text(locale, "locale")
        return tuple(
            sorted(
                (
                    change
                    for key in selected_keys
                    for change in self._fetch_changes(key, locale)
                ),
                key=lambda change: (change.occurred_at, change.id),
                reverse=True,
            )
        )

    def iter_changes(
        self,
        *,
        locale: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        after: str | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> Iterator[Change]:
        """Yield the project change stream from newest to oldest."""

        if locale is not None:
            locale = _text(locale, "locale")
        if since is not None:
            since = _boundary(since, "since")
        if until is not None:
            until = _boundary(until, "until")
        if since is not None and until is not None and since > until:
            raise ValueError("since must not be later than until")
        if after is not None:
            after = _text(after, "after")
        return self._iter_changes(
            locale=locale,
            since=since,
            until=until,
            after=after,
            on_progress=on_progress,
        )

    def change_pages(
        self,
        *,
        locale: str | None = None,
        start_page: int = 1,
        cursor: str | None = None,
    ) -> Iterator[ChangePage]:
        """Yield validated pages from the project change stream.

        ``cursor`` identifies a raw website record.  The record and all
        newer records are discarded, so the first yielded page continues with
        older records.  This is useful for callers that persist page
        checkpoints without exposing the website response format.
        """

        if locale is not None:
            locale = _text(locale, "locale")
        if type(start_page) is not int or start_page < 1:
            raise ValueError("start_page must be a positive integer")
        if cursor is not None:
            cursor = _text(cursor, "cursor")
        return self._change_pages(
            locale=locale,
            start_page=start_page,
            cursor=cursor,
        )

    def close(self) -> None:
        """Close the HTTP session when it belongs to this client."""

        if self._owns_session:
            self._session.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _iter_changes(
        self,
        *,
        locale: str | None,
        since: datetime | None,
        until: datetime | None,
        after: str | None,
        on_progress: Callable[[int, int], None] | None,
    ) -> Iterator[Change]:
        pending: list[Change] = []
        anchor_found = after is None
        for result in self.change_pages(locale=locale):
            if on_progress is not None:
                on_progress(result.number, result.total_pages)
            for change in result.changes:
                if after is not None and not anchor_found:
                    if change.id == after:
                        anchor_found = True
                        break
                    if (since is None or change.occurred_at >= since) and (
                        until is None or change.occurred_at <= until
                    ):
                        pending.append(change)
                    continue
                if since is not None and change.occurred_at < since:
                    return
                if until is None or change.occurred_at <= until:
                    if after is not None:
                        pending.append(change)
                    else:
                        yield change
            if after is not None and not anchor_found and result.cursor == after:
                anchor_found = True
            if after is not None and anchor_found:
                yield from pending
                return
        if after is not None and not anchor_found:
            raise RuntimeError(f"Change {after!r} was not found in the project stream")

    def _change_pages(
        self,
        *,
        locale: str | None,
        start_page: int,
        cursor: str | None,
    ) -> Iterator[ChangePage]:
        page = start_page
        seen: set[str] = set()
        previous_time: datetime | None = None
        anchor_found = cursor is None
        while True:
            result = self._fetch_change_page(page, locale)
            if result.total_pages == 0 or result.number > result.total_pages:
                break
            unique_records: list[tuple[str, datetime, Change | None]] = []
            for change_id, occurred_at, change in result.records:
                if change_id in seen:
                    continue
                if previous_time is not None and occurred_at > previous_time:
                    raise RuntimeError("Recent Changes is not ordered newest first")
                seen.add(change_id)
                previous_time = occurred_at
                unique_records.append((change_id, occurred_at, change))
            records = tuple(unique_records)
            if cursor is not None and not anchor_found:
                for index, (change_id, _, _) in enumerate(records):
                    if change_id == cursor:
                        anchor_found = True
                        records = records[index + 1 :]
                        break
                else:
                    records = ()
            yield result.to_page(records)
            if result.number >= result.total_pages:
                break
            page = result.number + 1
        if cursor is not None and not anchor_found:
            raise RuntimeError(
                f"Change cursor {cursor!r} was not found in the project stream"
            )

    def _fetch_changes(self, key: str, locale: str) -> tuple[Change, ...]:
        payload = _request_json(
            self._session,
            self._changes_url,
            resource="Translation changes",
            timeout=self._timeout,
            parameters={"key": key, "locale": locale},
        )
        return _parse_changes(payload, self._project, key, locale)

    def _fetch_change_page(self, page: int, locale: str | None) -> _ParsedChangePage:
        parameters: dict[str, int | str] = {"changesPage": page}
        if locale is not None:
            parameters["changesLocale"] = locale
        payload = _request_json(
            self._session,
            self._project_url,
            resource="Recent Changes",
            timeout=self._timeout,
            parameters=parameters,
        )
        result = _parse_change_page(payload, self._project, locale)
        if result.number != page:
            raise RuntimeError("Recent Changes returned an unexpected page")
        if page > result.total_pages and result.records:
            raise RuntimeError("Recent Changes records do not match page count")
        return result
