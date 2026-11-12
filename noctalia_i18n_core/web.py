"""Read Noctalia Translate project data."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import batched, pairwise
from urllib.parse import quote

import msgspec
import requests

from .http import HttpClient, _keys, _locales
from .models import (
    _BASE_URL,
    SOURCE_LOCALE,
    Change,
    ChangePage,
    Locale,
    Missing,
    Project,
    Review,
    Revision,
    RevisionPage,
    _text,
    _utc,
)

_TRANSLATION_BATCH_SIZE = 100
_CATALOG_LOOKUP_THRESHOLD = 2000
type _JsonValue = (
    None | bool | int | float | str | list[_JsonValue] | dict[str, _JsonValue]
)
_JSON_DECODER: msgspec.json.Decoder[_JsonValue] = msgspec.json.Decoder()


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


def _string(value: _JsonValue, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        expected = "a string" if allow_empty else "a non-empty string"
        raise RuntimeError(f"{label} is not {expected}")
    return value


def _optional_string(value: _JsonValue, label: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise RuntimeError(f"{label} is not a string or null")
    return value


def _integer(value: _JsonValue, label: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise RuntimeError(f"{label} is not an integer of at least {minimum}")
    return value


def _timestamp(value: _JsonValue, label: str) -> datetime:
    milliseconds = _integer(value, f"{label} created_at", minimum=0)
    try:
        seconds, remainder = divmod(milliseconds, 1000)
        return datetime.fromtimestamp(seconds, UTC) + timedelta(milliseconds=remainder)
    except (OSError, OverflowError, ValueError) as exc:
        raise RuntimeError(f"{label} created_at is out of range") from exc


def _revision(record: dict[str, _JsonValue], label: str) -> Revision:
    try:
        return Revision(
            id=_string(_member(record, "id", label), f"{label} ID"),
            key=_string(_member(record, "translation_key", label), f"{label} key"),
            locale=_string(_member(record, "locale", label), f"{label} locale"),
            actor=_optional_string(
                _member(record, "created_by_login", label), f"{label} actor"
            ),
            old_value=_optional_string(
                _member(record, "text", label), f"{label} old value"
            ),
            new_value=_optional_string(
                _member(record, "new_text", label), f"{label} new value"
            ),
            occurred_at=_timestamp(_member(record, "created_at", label), label),
        )
    except ValueError as exc:
        raise RuntimeError(f"{label} is invalid: {exc}") from exc


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
        return _JSON_DECODER.decode(response.content)
    except msgspec.DecodeError as exc:
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


def _page_object(
    payload: _JsonValue, resource: str, *fields: str
) -> dict[str, _JsonValue]:
    values = _page_values(payload, resource)
    root = _object(values[0], resource)
    selected = {name: root[name] for name in fields if name in root}
    return _object(_expand_indexed(selected, values, resource), resource)


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
    root = _page_object(
        payload,
        resource,
        "project",
        "stats",
        "locales",
        "missingByLocale",
        "outdatedByLocale",
    )
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
    for index, item in enumerate(values):
        code = _string(item, f"Project locale {index}")
        missing_count = _integer(
            _member(missing, code, resource),
            f"{resource} {code!r} missing count",
            minimum=0,
        )
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
    root = _page_object(payload, resource, "project", "tree", "totalKeys")
    _validate_project(root, project, resource)
    keys = _tree_keys(_member(root, "tree", resource))
    total = _integer(_member(root, "totalKeys", resource), "Key tree total", minimum=0)
    if len(keys) != total:
        raise RuntimeError("Key tree total does not match its keys")
    return tuple(keys)


def _flatten(value: _JsonValue, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        if not value:
            raise RuntimeError(
                f"Translation export contains an empty mapping at {path or '<root>'!r}"
            )
        for segment, child in value.items():
            if not segment:
                raise RuntimeError("Translation export contains an empty mapping key")
            yield from _flatten(child, f"{path}.{segment}" if path else segment)
        return
    if not isinstance(value, str):
        raise RuntimeError(
            f"Translation export contains a non-string value at {path!r}"
        )
    yield path, value


def _parse_translations(
    payload: _JsonValue,
    requested_keys: tuple[str, ...],
    selected_locales: tuple[str, ...],
) -> dict[str, dict[str, str]]:
    resource = "Translations"
    root = _object(payload, resource)
    records = _object(_member(root, "translations", resource), resource)
    output: dict[str, dict[str, str]] = {key: {} for key in requested_keys}
    languages = frozenset(selected_locales)
    for key, values in records.items():
        if key not in output:
            raise RuntimeError(f"Translations contains unexpected key {key!r}")
        locale_records = _object(values, f"Translation {key!r}")
        for locale, value in locale_records.items():
            if locale not in languages:
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
            text = _string(
                _member(record, "text", label), f"{label} text", allow_empty=True
            )
            output[key][locale] = text
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
    root = _page_object(
        payload,
        resource,
        "project",
        "search",
        "initialLocale",
        "page",
        "totalPages",
        "keys",
    )
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
                _string(
                    _member(record, "enNew", label),
                    f"{label} new source",
                    allow_empty=True,
                ),
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
        revision = _revision(_object(item, label), label)
        if revision.id in identifiers:
            raise RuntimeError(f"{resource} contains duplicate ID {revision.id!r}")
        identifiers.add(revision.id)
        if revision.key != key:
            raise RuntimeError(f"{label} belongs to another key")
        if revision.locale != locale:
            raise RuntimeError(f"{label} belongs to another locale")
        try:
            change = revision.change(project)
        except ValueError as exc:
            raise RuntimeError(f"{label} is invalid: {exc}") from exc
        if change is not None:
            output.append(change)
    if any(newer.occurred_at < older.occurred_at for newer, older in pairwise(output)):
        raise RuntimeError(f"{resource} is not ordered newest first")
    return tuple(output)


def _parse_revision_page(
    payload: _JsonValue, project: str, locale_filter: str | None
) -> RevisionPage:
    resource = "Recent Changes"
    root = _page_object(
        payload,
        resource,
        "project",
        "changesLocale",
        "changesPage",
        "changesTotalPages",
        "recentChanges",
    )
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
    output: list[Revision] = []
    for index, item in enumerate(records):
        label = f"{resource} record {index}"
        record = _object(item, label)
        if _member(record, "project_id", label) != project_id:
            raise RuntimeError(f"{label} belongs to another project")
        revision = _revision(record, label)
        if locale_filter is not None and revision.locale != locale_filter:
            raise RuntimeError(f"{label} belongs to another locale")
        output.append(revision)
    if any(newer.occurred_at < older.occurred_at for newer, older in pairwise(output)):
        raise RuntimeError(f"{resource} is not ordered newest first")
    try:
        return RevisionPage(
            number=number,
            total_pages=total_pages,
            revisions=tuple(output),
            cursor=output[-1].id if output else None,
        )
    except ValueError as exc:
        raise RuntimeError(f"{resource} is invalid: {exc}") from exc


class WebClient(HttpClient):
    """Read current project data from Noctalia Translate.

    Paginated queries fetch pages lazily. The default HTTP session retries
    transient failures three times. Request and response-validation failures
    raise :class:`RuntimeError` with the affected resource.
    """

    def __init__(
        self,
        project: str,
        *,
        timeout: float = 30,
        session: requests.Session | None = None,
        proxy: str | None = None,
    ) -> None:
        """Select a website project and configure HTTP access.

        ``timeout`` applies to each connection and read attempt. Supplied
        sessions remain owned by the caller. ``proxy=None`` uses environment
        settings; ``proxy=""`` connects directly. Configure supplied sessions
        directly for proxy and retry policies.
        """

        super().__init__(project, timeout=timeout, session=session, proxy=proxy)
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

    def project(self) -> Project:
        """Return current project metadata and per-language progress."""

        payload = _request_json(
            self._session,
            self._project_url,
            resource="Project metadata",
            timeout=self._timeout,
        )
        return _parse_project(payload, self._project)

    def keys(self, *, prefix: str | None = None) -> tuple[str, ...]:
        """Return website keys, optionally restricted to a hierarchy.

        ``prefix`` matches the exact key and descendants separated by ``.``.
        """

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

    def catalog(self, *, locale: str | None = None) -> dict[str, dict[str, str]]:
        """Return the complete export as ``{key: {locale: value}}``.

        Nested JSON keys are joined with ``.``. Empty strings are preserved.
        """

        if locale is not None:
            locale = _text(locale, "locale")
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
        for language, tree in root.items():
            if not language.strip():
                raise RuntimeError("Translation export contains an invalid locale")
            if not isinstance(tree, dict):
                raise RuntimeError(f"Invalid translation data for {language!r}")
            if not tree or (locale is not None and language != locale):
                continue
            for key, value in _flatten(tree):
                output.setdefault(key, {})[language] = value
        return output

    def translations(
        self,
        key: str,
        /,
        *keys: str,
        locales: Iterable[str] | None = None,
    ) -> dict[str, dict[str, str]]:
        """Return current values for exact keys as ``{key: {locale: value}}``.

        ``locales=None`` reads the project languages before fetching values.
        Keys are fetched in batches.
        """

        selected_keys = _keys((key, *keys), "keys")
        selected_locales = _locales(locales, "locales")
        if selected_locales is None:
            selected_locales = tuple(locale.code for locale in self.project().locales)
        output: dict[str, dict[str, str]] = {}
        for batch in batched(selected_keys, _TRANSLATION_BATCH_SIZE):
            body = {"keys": list(batch), "locales": list(selected_locales)}
            payload = _request_json(
                self._session,
                self._batch_url,
                resource="Translations response",
                timeout=self._timeout,
                body=body,
            )
            output.update(_parse_translations(payload, batch, selected_locales))
        return output

    def search(
        self,
        query: str,
        *,
        search_locale: str,
        locales: Iterable[str] | None = None,
    ) -> dict[str, dict[str, str]]:
        """Search key names and text in ``search_locale`` across all result pages.

        ``locales`` selects the returned values and defaults to
        ``search_locale``.
        """

        query = _text(query, "query")
        search_locale = _text(search_locale, "search_locale")
        selected_locales = _locales(locales, "locales")
        page = 1
        found: dict[str, None] = {}
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
            found.update(dict.fromkeys(keys))
            if current_page >= total_pages:
                break
            page = current_page + 1
        if selected_locales is None:
            selected_locales = (search_locale,)
        if not found:
            return {}
        return self.translations(*found, locales=selected_locales)

    def missing(
        self, locale: str, /, *, query: str | None = None
    ) -> tuple[Missing, ...]:
        """Return missing translations with available English source text.

        ``query`` is forwarded to the website's search filter.
        """

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
        translations = self._lookup_translations(keys, SOURCE_LOCALE)
        output: list[Missing] = []
        for key in keys:
            source = translations.get(key)
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
        """Return translations whose English source needs review.

        ``query`` is forwarded to the website's search filter. A review entry
        without its saved target translation raises :class:`RuntimeError`.
        """

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
        translations = self._lookup_translations(keys, locale)
        output: list[Review] = []
        for key, old_source, new_source in sources:
            translation = translations.get(key)
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
        """Return complete website history for the selected keys and language.

        Results are newest first, with equal timestamps ordered by identifier.
        """

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
        """Yield project changes from newest to oldest.

        ``since`` and ``until`` are inclusive, timezone-aware bounds.
        ``after`` excludes that raw revision and all older records.

        Args:
            on_progress: Called with the current and total page numbers after
                each fetched page.

        Raises:
            RuntimeError: ``after`` cannot be found in the website history.
        """

        if locale is not None:
            locale = _text(locale, "locale")
        if since is not None:
            since = _utc(since, "since")
        if until is not None:
            until = _utc(until, "until")
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
        """Yield normalized change pages in website order.

        Empty-text revisions are omitted from ``changes`` while ``cursor``
        retains the raw continuation position. See :meth:`revision_pages`
        for cursor and starting-page semantics.
        """

        return self._change_pages(
            self.revision_pages(locale=locale, start_page=start_page, cursor=cursor)
        )

    def revision_pages(
        self,
        *,
        locale: str | None = None,
        start_page: int = 1,
        cursor: str | None = None,
    ) -> Iterator[RevisionPage]:
        """Yield raw revision pages from newest to oldest.

        ``cursor`` excludes that revision and newer records. ``start_page`` is
        a hint for locating it when website pages shift. Without a cursor,
        reading begins at ``start_page``.

        Raises:
            RuntimeError: The cursor cannot be found.
        """

        if locale is not None:
            locale = _text(locale, "locale")
        if type(start_page) is not int or start_page < 1:
            raise ValueError("start_page must be a positive integer")
        if cursor is not None:
            cursor = _text(cursor, "cursor")
        return self._revision_pages(locale=locale, start_page=start_page, cursor=cursor)

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
        for result in self.revision_pages(locale=locale):
            if on_progress is not None:
                on_progress(result.number, result.total_pages)
            for revision in result.revisions:
                if revision.id == after:
                    yield from pending
                    return
                if since is not None and revision.occurred_at < since:
                    if after is None:
                        return
                    continue
                if until is not None and revision.occurred_at > until:
                    continue
                change = revision.change(self.slug)
                if change is not None:
                    if after is None:
                        yield change
                    else:
                        pending.append(change)
        if after is not None:
            raise RuntimeError(f"Change {after!r} was not found in the project stream")

    def _revision_pages(
        self,
        *,
        locale: str | None,
        start_page: int,
        cursor: str | None,
    ) -> Iterator[RevisionPage]:
        page = start_page
        seen: set[str] = set()
        previous_time: datetime | None = None
        anchor_found = cursor is None
        while True:
            result = self._fetch_revision_page(page, locale)
            if result.total_pages and result.number > result.total_pages:
                break
            records: list[Revision] = []
            for item in result.revisions:
                if item.id in seen:
                    continue
                if previous_time is not None and item.occurred_at > previous_time:
                    raise RuntimeError("Recent Changes is not ordered newest first")
                seen.add(item.id)
                previous_time = item.occurred_at
                records.append(item)
            if not anchor_found:
                for index, item in enumerate(records):
                    if item.id == cursor:
                        anchor_found = True
                        records = records[index + 1 :]
                        break
                else:
                    records = []
            if anchor_found:
                yield replace(result, revisions=tuple(records))
            if result.number >= result.total_pages:
                break
            page = result.number + 1
        if not anchor_found:
            raise RuntimeError(
                f"Change cursor {cursor!r} was not found in the project stream"
            )

    def _change_pages(self, pages: Iterator[RevisionPage]) -> Iterator[ChangePage]:
        for result in pages:
            if not result.total_pages:
                return
            yield ChangePage(
                number=result.number,
                total_pages=result.total_pages,
                changes=tuple(
                    change
                    for item in result.revisions
                    if (change := item.change(self.slug)) is not None
                ),
                cursor=result.cursor,
            )

    def _lookup_translations(
        self, keys: tuple[str, ...], locale: str
    ) -> dict[str, str]:
        if len(keys) > _CATALOG_LOOKUP_THRESHOLD:
            values = self.catalog(locale=locale)
        else:
            values = self.translations(*keys, locales=(locale,))
        return {
            key: translations[locale]
            for key in keys
            if locale in (translations := values.get(key, {}))
        }

    def _fetch_changes(self, key: str, locale: str) -> tuple[Change, ...]:
        payload = _request_json(
            self._session,
            self._changes_url,
            resource="Translation changes",
            timeout=self._timeout,
            parameters={"key": key, "locale": locale},
        )
        return _parse_changes(payload, self._project, key, locale)

    def _fetch_revision_page(self, page: int, locale: str | None) -> RevisionPage:
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
        result = _parse_revision_page(payload, self._project, locale)
        if result.number != page:
            raise RuntimeError("Recent Changes returned an unexpected page")
        if page > result.total_pages and result.revisions:
            raise RuntimeError("Recent Changes records do not match page count")
        return result
