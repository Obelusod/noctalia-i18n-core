"""Normalized Noctalia Translate values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

SOURCE_LOCALE = "en"
type ChangeAction = Literal["added", "modified", "deleted"]


def _text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


def _count(value: object, label: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")


def _utc(value: object) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Change occurred_at must include a timezone")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True, kw_only=True)
class Locale:
    """Current translation progress for one configured locale."""

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
        """Return the website completion percentage."""

        total = self.translation_count + self.missing_count
        return self.translation_count * 100 // total if total else 0


@dataclass(frozen=True, slots=True, kw_only=True)
class Project:
    """Public metadata and current translation progress for one project."""

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
        codes = tuple(locale.code for locale in self.locales)
        if len(codes) != len(set(codes)):
            raise ValueError("Project contains duplicate locales")
        if any(
            locale.translation_count + locale.missing_count != self.key_count
            for locale in self.locales
        ):
            raise ValueError("Project locale counts do not match key_count")

    @property
    def language_count(self) -> int:
        """Return the number of configured locales."""

        return len(self.locales)

    @property
    def translation_count(self) -> int:
        """Return the number of current translations."""

        return sum(locale.translation_count for locale in self.locales)

    @property
    def missing_count(self) -> int:
        """Return the number of missing translations."""

        return sum(locale.missing_count for locale in self.locales)

    @property
    def review_count(self) -> int:
        """Return the number of translations that need review."""

        return sum(locale.review_count for locale in self.locales)

    @property
    def progress(self) -> int:
        """Return the website completion percentage."""

        total = self.key_count * self.language_count
        return self.translation_count * 100 // total if total else 0


@dataclass(frozen=True, slots=True, kw_only=True)
class Missing:
    """One source entry without a translation in the selected locale."""

    key: str
    locale: str
    source: str
    translation_url: str

    def __post_init__(self) -> None:
        for name, value in (
            ("key", self.key),
            ("locale", self.locale),
            ("source", self.source),
            ("translation_url", self.translation_url),
        ):
            _text(value, f"Missing {name}")


@dataclass(frozen=True, slots=True, kw_only=True)
class Review:
    """One translation whose source changed after it was saved."""

    key: str
    locale: str
    source: str
    previous_source: str | None
    translation_url: str

    def __post_init__(self) -> None:
        for name, value in (
            ("key", self.key),
            ("locale", self.locale),
            ("source", self.source),
            ("translation_url", self.translation_url),
        ):
            _text(value, f"Review {name}")
        if self.previous_source is not None:
            _text(self.previous_source, "Review previous_source")


@dataclass(frozen=True, slots=True, kw_only=True)
class Change:
    """One translation change returned by Noctalia Translate."""

    id: str
    key: str
    locale: str
    action: ChangeAction
    old_value: str | None
    new_value: str | None
    occurred_at: datetime
    translation_url: str
    actor_login: str | None
    actor_url: str | None = None
    actor_avatar_url: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("id", self.id),
            ("key", self.key),
            ("locale", self.locale),
        ):
            _text(value, f"Change {name}")
        if self.action not in ("added", "modified", "deleted"):
            raise ValueError(f"Invalid change action: {self.action!r}")
        valid = {
            "added": self.old_value is None
            and isinstance(self.new_value, str)
            and self.new_value != "",
            "modified": isinstance(self.old_value, str)
            and self.old_value != ""
            and isinstance(self.new_value, str)
            and self.new_value != "",
            "deleted": isinstance(self.old_value, str)
            and self.old_value != ""
            and self.new_value is None,
        }
        if not valid[self.action]:
            raise ValueError(f"Change values do not match action {self.action!r}")
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at))
        _text(self.translation_url, "Change translation_url")
        if self.actor_login is not None:
            _text(self.actor_login, "Change actor_login")
        for name, value in (
            ("actor_url", self.actor_url),
            ("actor_avatar_url", self.actor_avatar_url),
        ):
            if value is not None:
                _text(value, f"Change {name}")


@dataclass(frozen=True, slots=True, kw_only=True)
class ChangePage:
    """One validated page from the project change stream."""

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
        if type(self.changes) is not tuple:
            raise ValueError("Change page changes must be a tuple")
        identifiers = tuple(change.id for change in self.changes)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Change page contains duplicate changes")
        if self.cursor is not None:
            _text(self.cursor, "Change page cursor")
