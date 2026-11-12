"""HTTP sessions, request parameters, and resource ownership."""

from __future__ import annotations

import math
from collections.abc import Iterable
from types import TracebackType
from typing import Self

import requests
from requests.adapters import HTTPAdapter, Retry

from .models import _text, translation_url

_RETRYABLE_METHODS = frozenset({"GET", "POST"})
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


def create_session() -> requests.Session:
    """Create a session with retries for transient HTTP failures."""
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


class HttpClient:
    """Own a project identifier and an optional HTTP session."""

    def __init__(
        self,
        project: str,
        *,
        timeout: float = 30,
        session: requests.Session | None = None,
        proxy: str | None = None,
    ) -> None:
        """Configure project identity and HTTP resources.

        ``timeout`` applies to each connection and read attempt. Supplied
        sessions remain owned by the caller. ``proxy=None`` uses environment
        settings; ``proxy=""`` connects directly. Configure supplied sessions
        directly for proxy and retry policies.
        """
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a positive finite number")
        self._project = _text(project, "project")
        if session is not None and proxy is not None:
            raise ValueError("Configure the proxy on the supplied session")
        self._timeout = timeout
        self._owns_session = session is None
        self._session = session if session is not None else create_session()
        if proxy is not None:
            self._session.trust_env = False
            if proxy:
                self._session.proxies.update({"http": proxy, "https": proxy})

    @property
    def slug(self) -> str:
        """Website project identifier."""
        return self._project

    def translation_url(self, key: str, locale: str, /) -> str:
        """Return the website editor URL for an exact translation."""
        return translation_url(
            self._project, _text(key, "key"), _text(locale, "locale")
        )

    def close(self) -> None:
        """Close the internally owned HTTP session."""
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
