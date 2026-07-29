"""Domain values shared by sources, monitoring, state, and rendering."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from typing import Literal, TypeGuard

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
type Action = Literal["added", "modified", "deleted"]
type ResetMode = Literal["baseline", "full"]

ACTIONS: tuple[Action, ...] = ("added", "modified", "deleted")
SOURCE_LOCALE = "en"


def _is_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def normalize_json(value: object, label: str) -> JsonValue:
    """Return a validated copy in the supported JSON data model."""

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} contains a non-finite number")
        return value
    if _is_list(value):
        return [
            normalize_json(item, f"{label}[{index}]")
            for index, item in enumerate(value)
        ]
    if _is_dict(value):
        output: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{label} must use string mapping keys")
            output[key] = normalize_json(item, f"{label}.{key}")
        return output
    raise ValueError(f"{label} contains a non-JSON value")


@dataclass(frozen=True, slots=True)
class Change:
    """One normalized translation change."""

    id: str
    key: str
    locale: str
    actor: str
    old_value: str | None
    new_value: str | None
    action: Action
    occurred_at: datetime
    url: str | None
    actor_url: str | None = None
    actor_avatar_url: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("id", self.id),
            ("key", self.key),
            ("locale", self.locale),
            ("actor", self.actor),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"Change {name} must be a non-empty string")
        for name, value in (
            ("url", self.url),
            ("actor_url", self.actor_url),
            ("actor_avatar_url", self.actor_avatar_url),
        ):
            if value is not None and (type(value) is not str or not value.strip()):
                raise ValueError(f"Change {name} must be None or a non-empty string")
        if self.action not in ACTIONS:
            raise ValueError(f"Invalid change action: {self.action!r}")
        if (
            type(self.occurred_at) is not datetime
            or self.occurred_at.utcoffset() is None
        ):
            raise ValueError("Change occurred_at must include a timezone")
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(UTC))

        old, new = self.old_value, self.new_value
        valid = {
            "added": old is None and isinstance(new, str),
            "modified": isinstance(old, str) and isinstance(new, str),
            "deleted": isinstance(old, str) and new is None,
        }
        if not valid[self.action]:
            raise ValueError(f"Change values do not match action {self.action!r}")

    @property
    def is_source(self) -> bool:
        return self.locale == SOURCE_LOCALE

    @property
    def iso_timestamp(self) -> str:
        microsecond = self.occurred_at.microsecond
        if microsecond % 1000:
            timespec = "microseconds"
        elif microsecond:
            timespec = "milliseconds"
        else:
            timespec = "seconds"
        return self.occurred_at.isoformat(timespec=timespec).replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class PollResult:
    """One source poll with normalized changes and an opaque cursor."""

    changes: tuple[Change, ...]
    cursor: JsonValue
    scanned: int
    source_texts: dict[str, str] | None = None

    def __post_init__(self) -> None:
        cursor = normalize_json(self.cursor, "Poll result cursor")
        if cursor is None:
            raise ValueError("Poll result cursor must not be None")
        object.__setattr__(self, "cursor", cursor)
        if type(self.scanned) is not int or self.scanned < len(self.changes):
            raise ValueError("Poll result scanned count is invalid")
        identifiers = [change.id for change in self.changes]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Poll result change IDs must be unique")
        if any(
            older.occurred_at > newer.occurred_at
            for older, newer in pairwise(self.changes)
        ):
            raise ValueError("Poll result changes must be ordered oldest first")
        if self.source_texts is not None:
            if any(
                type(key) is not str or not key or type(value) is not str
                for key, value in self.source_texts.items()
            ):
                raise ValueError(
                    "Poll result source texts must map non-empty strings to strings"
                )
            object.__setattr__(self, "source_texts", dict(self.source_texts))


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """Opaque cursor and its matching complete English snapshot."""

    cursor: JsonValue
    source_texts: dict[str, str]

    def __post_init__(self) -> None:
        cursor = normalize_json(self.cursor, "Checkpoint cursor")
        if cursor is None:
            raise ValueError("Checkpoint cursor must not be None")
        if any(
            type(key) is not str or not key or type(value) is not str
            for key, value in self.source_texts.items()
        ):
            raise ValueError(
                "Checkpoint source_texts must map non-empty strings to strings"
            )
        object.__setattr__(self, "cursor", cursor)
        object.__setattr__(self, "source_texts", dict(self.source_texts))


@dataclass(frozen=True, slots=True)
class DeliveryPolicy:
    """Accumulation, folding, and optional locale-merge behavior."""

    quiet_seconds: int
    max_wait_seconds: int
    fold_changes: bool
    merge_threshold: int | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("quiet_seconds", self.quiet_seconds),
            ("max_wait_seconds", self.max_wait_seconds),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"Delivery {name} must be non-negative")
        if self.max_wait_seconds < self.quiet_seconds:
            raise ValueError(
                "Delivery max_wait_seconds must not be less than quiet_seconds"
            )
        if type(self.fold_changes) is not bool:
            raise ValueError("Delivery fold_changes must be a boolean")
        if self.merge_threshold is not None and (
            type(self.merge_threshold) is not int or self.merge_threshold < 0
        ):
            raise ValueError("Delivery merge_threshold must be None or non-negative")


@dataclass(frozen=True, slots=True)
class Delivery:
    """A change enriched with the English text needed for rendering."""

    change: Change
    source_text: str | None
    change_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        identifiers = self.change_ids or (self.change.id,)
        if any(type(item) is not str or not item for item in identifiers):
            raise ValueError("Delivery change IDs must be non-empty strings")
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Delivery change IDs must be unique")
        object.__setattr__(self, "change_ids", identifiers)

    @classmethod
    def from_change(
        cls,
        change: Change,
        source_texts: Mapping[str, str],
        previous_source_texts: Mapping[str, str] | None = None,
    ) -> Delivery:
        if change.is_source:
            source_text = (
                change.old_value if change.action == "deleted" else change.new_value
            )
        else:
            source_text = source_texts.get(change.key)
            if source_text is None and previous_source_texts is not None:
                source_text = previous_source_texts.get(change.key)
        return cls(change, source_text)


@dataclass(frozen=True, slots=True)
class QueuedDelivery:
    """A delivery retained in the outbox until its route is ready."""

    delivery: Delivery
    queued_at: datetime

    def __post_init__(self) -> None:
        if type(self.queued_at) is not datetime or self.queued_at.utcoffset() is None:
            raise ValueError("Queued delivery time must include a timezone")
        object.__setattr__(self, "queued_at", self.queued_at.astimezone(UTC))
