"""Typed queries against a Noctalia data service."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from itertools import batched
from typing import override
from urllib.parse import quote, urlencode, urlsplit

import msgspec
import requests

from .http import HttpClient, _keys, _locales
from .models import (
    Change,
    ChangeAction,
    ChangeBatch,
    CollectionStatus,
    Missing,
    Project,
    Review,
    Revision,
)

type _Parameters = dict[str, str | int | tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class _Error:
    error: str


def _timestamp(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("Timestamps must include a timezone")
    return value.isoformat()


class DataClient(HttpClient):
    """Query a locally deployed or remote translation data service.

    Each response describes a committed database view. :meth:`updates`
    couples changes to their processing position.
    """

    def __init__(
        self,
        project: str,
        *,
        url: str,
        timeout: float = 30,
        session: requests.Session | None = None,
        proxy: str | None = None,
    ) -> None:
        """Connect through an HTTP(S) service address.

        ``timeout`` applies to each connection and read attempt. Supplied
        sessions remain owned by the caller. Conditional requests reuse the
        last response for each query endpoint.
        """
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Data service URL must be an absolute HTTP(S) address")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Configure credentials on the supplied session")
        if parsed.query or parsed.fragment:
            raise ValueError("Data service URL cannot contain a query or fragment")
        super().__init__(project, timeout=timeout, session=session, proxy=proxy)
        self._url = url.rstrip("/") + "/v1/projects/" + quote(project, safe="")
        self._cache: dict[str, tuple[str, str, bytes]] = {}

    def status(self) -> CollectionStatus:
        """Return collection progress and available data ranges."""
        return self._get("status", CollectionStatus)

    def project(self) -> Project:
        """Return project metadata and language progress."""
        return self._get("project", Project)

    def keys(self, *, prefix: str | None = None) -> tuple[str, ...]:
        """Return keys matching the exact ``prefix`` and its dot-separated descendants."""
        return self._get(
            "keys", tuple[str, ...], {} if prefix is None else {"prefix": prefix}
        )

    def catalog(self, *, locale: str | None = None) -> dict[str, dict[str, str]]:
        """Return the collected catalog as ``{key: {locale: value}}``."""
        return self._get(
            "catalog",
            dict[str, dict[str, str]],
            {} if locale is None else {"locale": locale},
        )

    def translations(
        self, key: str, /, *keys: str, locales: Iterable[str] | None = None
    ) -> dict[str, dict[str, str]]:
        """Return exact-key values, preserving empty strings and absent-key maps."""
        languages = _locales(locales, "locales")
        result: dict[str, dict[str, str]] = {}
        for selected in batched(_keys((key, *keys), "keys"), 100):
            parameters: _Parameters = {"keys": selected}
            if languages is not None:
                parameters["locales"] = languages
            result.update(
                self._get("translations", dict[str, dict[str, str]], parameters)
            )
        return result

    def search(
        self, query: str, *, search_locale: str, locales: Iterable[str] | None = None
    ) -> dict[str, dict[str, str]]:
        """Search key names and text; returned languages default to ``search_locale``."""
        parameters: _Parameters = {"query": query, "search_locale": search_locale}
        languages = _locales(locales, "locales")
        if languages is not None:
            parameters["locales"] = languages
        return self._get("search", dict[str, dict[str, str]], parameters)

    def missing(
        self, locale: str, /, *, query: str | None = None
    ) -> tuple[Missing, ...]:
        """Return missing translations with available English text."""
        parameters: _Parameters = {"locale": locale}
        if query is not None:
            parameters["query"] = query
        return self._get("missing", tuple[Missing, ...], parameters)

    def reviews(
        self, locale: str, /, *, query: str | None = None
    ) -> tuple[Review, ...]:
        """Return the language's collected review state."""
        parameters: _Parameters = {"locale": locale}
        if query is not None:
            parameters["query"] = query
        return self._get("reviews", tuple[Review, ...], parameters)

    def revision(self, identifier: str) -> Revision:
        """Return a published raw revision, preserving empty and null values."""
        return self._get("revision", Revision, {"id": identifier})

    def history(
        self,
        *,
        after: str | None = None,
        before: str | None = None,
        locale: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        actor: str | None = None,
        action: ChangeAction | None = None,
        keys: Iterable[str] = (),
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[Change, ...]:
        """Read a newest-first page of collected changes.

        ``before`` provides stable pagination as newer changes arrive.
        Time bounds are inclusive. ``limit`` is at most 1000.
        """
        parameters: _Parameters = {
            "limit": limit,
            "offset": offset,
            "keys": tuple(keys),
        }
        for field, value in (
            ("after", after),
            ("before", before),
            ("locale", locale),
            ("actor", actor),
            ("action", action),
        ):
            if value is not None:
                parameters[field] = value
        for field, boundary in (("since", since), ("until", until)):
            if boundary is not None:
                parameters[field] = _timestamp(boundary)
        return self._get("history", tuple[Change, ...], parameters)

    def changes(self, key: str, /, *keys: str, locale: str) -> tuple[Change, ...]:
        """Read all collected history for exact keys with cursor pagination."""
        result: list[Change] = []
        before: str | None = None
        while True:
            page = self.history(
                keys=(key, *keys), locale=locale, before=before, limit=1000
            )
            result.extend(page)
            if len(page) < 1000:
                return tuple(result)
            before = page[-1].id

    def contributors(
        self, *, locale: str | None = None
    ) -> tuple[tuple[str, str, str, int], ...]:
        """Return ``(actor, locale, action, count)`` tuples, ordered by descending count."""
        return self._get(
            "contributors",
            tuple[tuple[str, str, str, int], ...],
            {} if locale is None else {"locale": locale},
        )

    def updates(self, after: str | None = None, *, limit: int = 500) -> ChangeBatch:
        """Read an oldest-first batch and its matching processing position.

        ``after=None`` establishes the current position. Continue from
        :attr:`ChangeBatch.cursor` while :attr:`ChangeBatch.has_more` is true.
        Raw records without text changes also advance the cursor.
        """
        parameters: _Parameters = {"limit": limit}
        if after is not None:
            parameters["after"] = after
        return self._get("updates", ChangeBatch, parameters)

    @override
    def close(self) -> None:
        """Release internally owned HTTP resources and cached responses."""
        super().close()
        self._cache.clear()

    def _get[T](
        self,
        resource: str,
        response_type: type[T],
        parameters: _Parameters | None = None,
    ) -> T:
        query: _Parameters = {} if parameters is None else parameters
        cache_key = urlencode(query, doseq=True)
        cached = self._cache.get(resource)
        headers = {"Accept": "application/json"}
        if cached is not None and cached[0] == cache_key:
            headers["If-None-Match"] = cached[1]
        try:
            with self._session.get(
                self._url + "/" + resource,
                params=query,
                headers=headers,
                timeout=self._timeout,
            ) as response:
                if response.status_code == 304:
                    if cached is None or cached[0] != cache_key:
                        raise RuntimeError(
                            "Service returned 304 without a cached response"
                        )
                    body = cached[2]
                elif response.status_code == 400:
                    raise ValueError(
                        msgspec.json.decode(response.content, type=_Error).error
                    )
                else:
                    response.raise_for_status()
                    body = response.content
                result = msgspec.json.decode(body, type=response_type)
                if response.status_code != 304 and (
                    etag := response.headers.get("ETag")
                ):
                    self._cache[resource] = (cache_key, etag, body)
                return result
        except (requests.RequestException, msgspec.DecodeError) as error:
            raise RuntimeError(f"Data service query {resource!r} failed") from error
