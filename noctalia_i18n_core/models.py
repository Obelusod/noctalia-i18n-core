"""Normalized Noctalia Translate values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import quote, urlencode

_BASE_URL = "https://i18n.noctalia.dev"

SOURCE_LOCALE = "en"
type ChangeAction = Literal["added", "modified", "deleted"]


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _count(value: object, label: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")


def _utc(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True, kw_only=True)
class Locale:
    """Translation progress for one language.

    ``review_count`` counts translated entries whose English source changed.
    """

    code: str
    translation_count: int
    missing_count: int
    review_count: int

    def __post_init__(self) -> None:
        _text(self.code, "Locale code")
        for name, value in (
            ("translation_count", self.translation_count),
            ("missing_count", self.missing_count),
            ("review_count", self.review_count),
        ):
            _count(value, f"Locale {name}")
        if self.review_count > self.translation_count:
            raise ValueError("Locale review_count exceeds translation_count")

    @property
    def progress(self) -> int:
        """Completion percentage rounded down; zero for an empty scope."""

        total = self.translation_count + self.missing_count
        return self.translation_count * 100 // total if total else 0


@dataclass(frozen=True, slots=True, kw_only=True)
class Project:
    """Public project metadata and per-language translation progress."""

    slug: str
    name: str
    description: str | None
    translation_context: str | None
    key_count: int
    locales: tuple[Locale, ...]

    def __post_init__(self) -> None:
        _text(self.slug, "Project slug")
        _text(self.name, "Project name")
        for name, value in (
            ("description", self.description),
            ("translation_context", self.translation_context),
        ):
            if value is not None:
                _text(value, f"Project {name}")
        _count(self.key_count, "Project key_count")
        if len(self.locales) != len({locale.code for locale in self.locales}):
            raise ValueError("Project contains duplicate locales")
        if any(
            locale.translation_count + locale.missing_count != self.key_count
            for locale in self.locales
        ):
            raise ValueError("Project locale counts do not match key_count")

    @property
    def language_count(self) -> int:
        """Number of configured languages."""

        return len(self.locales)

    @property
    def translation_count(self) -> int:
        """Current translations summed across project languages."""

        return sum(locale.translation_count for locale in self.locales)

    @property
    def missing_count(self) -> int:
        """Missing translations summed across project languages."""

        return sum(locale.missing_count for locale in self.locales)

    @property
    def review_count(self) -> int:
        """Translations needing review summed across project languages."""

        return sum(locale.review_count for locale in self.locales)

    @property
    def progress(self) -> int:
        """Completion percentage rounded down; zero for an empty scope."""

        total = self.key_count * self.language_count
        return self.translation_count * 100 // total if total else 0


@dataclass(frozen=True, slots=True, kw_only=True)
class Missing:
    """An untranslated key with available English text and its website editor URL."""

    key: str
    locale: str
    source: str | None
    translation_url: str

    def __post_init__(self) -> None:
        for name, value in (
            ("key", self.key),
            ("locale", self.locale),
            ("translation_url", self.translation_url),
        ):
            _text(value, f"Missing {name}")
        if self.source is not None and not isinstance(self.source, str):
            raise ValueError("Missing source must be text or None")


@dataclass(frozen=True, slots=True, kw_only=True)
class Review:
    """A saved translation whose English source has changed.

    ``old_source`` is the source at the time of translation; ``new_source``
    is the current source.
    """

    key: str
    locale: str
    old_source: str | None
    new_source: str
    translation: str
    translation_url: str

    def __post_init__(self) -> None:
        for name, value in (
            ("key", self.key),
            ("locale", self.locale),
            ("translation_url", self.translation_url),
        ):
            _text(value, f"Review {name}")
        for name, value in (
            ("new_source", self.new_source),
            ("translation", self.translation),
        ):
            if not isinstance(value, str):
                raise ValueError(f"Review {name} must be text")
        if self.old_source is not None and not isinstance(self.old_source, str):
            raise ValueError("Review old_source must be text or None")


@dataclass(frozen=True, slots=True, kw_only=True)
class Change:
    """A normalized translation change with a UTC timestamp.

    Absent old or new text is ``None``. Unattributed actors use ``"API"``.

    Attributes:
        source: English context supplied by the query, or ``None`` when
            unavailable. English deletions retain their old source text.
    """

    id: str
    key: str
    locale: str
    action: ChangeAction
    actor: str
    old_value: str | None
    new_value: str | None
    occurred_at: datetime
    translation_url: str
    source: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("id", self.id),
            ("key", self.key),
            ("locale", self.locale),
        ):
            _text(value, f"Change {name}")
        if self.action not in ("added", "modified", "deleted"):
            raise ValueError(f"Invalid change action: {self.action!r}")
        has_old = isinstance(self.old_value, str) and self.old_value != ""
        has_new = isinstance(self.new_value, str) and self.new_value != ""
        if self.action == "added":
            valid = self.old_value is None and has_new
        elif self.action == "deleted":
            valid = has_old and self.new_value is None
        else:
            valid = has_old and has_new
        if not valid:
            raise ValueError(f"Change values do not match action {self.action!r}")
        if self.source is not None and not isinstance(self.source, str):
            raise ValueError("Change source must be text or None")
        _text(self.actor, "Change actor")
        object.__setattr__(
            self, "occurred_at", _utc(self.occurred_at, "Change occurred_at")
        )
        _text(self.translation_url, "Change translation_url")


@dataclass(frozen=True, slots=True, kw_only=True)
class ChangePage:
    """Normalized changes in website order.

    ``cursor`` identifies a raw revision, including records omitted from
    ``changes``.
    """

    number: int
    total_pages: int
    changes: tuple[Change, ...]
    cursor: str | None

    def __post_init__(self) -> None:
        _count(self.number, "Change page number")
        if self.number == 0:
            raise ValueError("Change page number must be positive")
        _count(self.total_pages, "Change page total_pages")
        if self.total_pages == 0:
            raise ValueError("Change page total_pages must be positive")
        if self.number > self.total_pages:
            raise ValueError("Change page number exceeds total_pages")
        if len(self.changes) != len({change.id for change in self.changes}):
            raise ValueError("Change page contains duplicate changes")
        if self.cursor is not None:
            _text(self.cursor, "Change page cursor")


@dataclass(frozen=True, slots=True, kw_only=True)
class Revision:
    """A raw website revision with its timestamp normalized to UTC.

    ``old_value`` and ``new_value`` preserve empty strings and nulls.
    Identifiers remain usable as cursors for revisions without text changes.
    """

    id: str
    key: str
    locale: str
    actor: str | None
    old_value: str | None
    new_value: str | None
    occurred_at: datetime

    def __post_init__(self) -> None:
        for name in ("id", "key", "locale"):
            _text(getattr(self, name), f"Revision {name}")
        for name in ("actor", "old_value", "new_value"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"Revision {name} must be text or None")
        object.__setattr__(
            self, "occurred_at", _utc(self.occurred_at, "Revision occurred_at")
        )

    def change(self, project: str, *, source: str | None = None) -> Change | None:
        """Normalize this revision, returning ``None`` when both texts are empty.

        Empty values become ``None`` in the resulting change. ``source``
        supplies English context for target-language revisions; English
        revisions use their own new or deleted text.
        """

        _text(project, "project")
        old, new = self.old_value or None, self.new_value or None
        if old is None and new is None:
            return None
        action: ChangeAction = (
            "added" if old is None else "deleted" if new is None else "modified"
        )
        return Change(
            id=self.id,
            key=self.key,
            locale=self.locale,
            action=action,
            actor=self.actor or "API",
            old_value=old,
            new_value=new,
            occurred_at=self.occurred_at,
            translation_url=translation_url(project, self.key, self.locale),
            source=(old if new is None else new)
            if self.locale == SOURCE_LOCALE
            else source,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RevisionPage:
    """Raw website revisions in page order, preserving empty strings and nulls."""

    number: int
    total_pages: int
    revisions: tuple[Revision, ...]
    cursor: str | None

    def __post_init__(self) -> None:
        _count(self.number, "Revision page number")
        _count(self.total_pages, "Revision page total_pages")
        if self.number < 1 or (self.revisions and self.number > self.total_pages):
            raise ValueError("Invalid revision page number")
        if len(self.revisions) != len({item.id for item in self.revisions}):
            raise ValueError("Revision page contains duplicate IDs")
        if self.cursor is not None:
            _text(self.cursor, "Revision page cursor")


@dataclass(frozen=True, slots=True, kw_only=True)
class CollectionStatus:
    """Availability and independent collection progress for one project.

    Attributes:
        head: ``None`` before history collection; ``""`` after collecting an
            empty history; otherwise the newest raw revision identifier.
        history_complete: Whether history extends through the oldest website
            page. Counts and dates describe only the published range.
        pending_revisions: Saved records awaiting atomic publication.
        source_count: English catalog entries, including empty strings.
    """

    project: str
    initialized: bool
    head: str | None
    oldest_at: datetime | None
    history_complete: bool
    history_checked_at: datetime | None
    catalog_checked_at: datetime | None
    revision_count: int
    pending_revisions: int
    source_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ChangeBatch:
    """An oldest-first change batch and its matching processing position.

    ``cursor`` includes raw revisions without text changes. ``has_more`` means
    the next request should continue from that cursor. ``status`` describes the
    same committed view as the returned changes.
    """

    status: CollectionStatus
    cursor: str
    changes: tuple[Change, ...]
    has_more: bool


def translation_url(project: str, key: str, locale: str) -> str:
    """Return the website editor URL for a translation."""

    return (
        f"{_BASE_URL}/projects/{quote(project, safe='')}/translate?"
        f"{urlencode({'search': key, 'locale': locale})}"
    )
