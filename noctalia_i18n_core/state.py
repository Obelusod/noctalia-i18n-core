"""SQLite monitoring state."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import (
    ACTIONS,
    Action,
    Change,
    Checkpoint,
    Delivery,
    JsonValue,
    QueuedDelivery,
    ResetMode,
    normalize_json,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery_receipts (
    route_id TEXT NOT NULL,
    change_id TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    PRIMARY KEY(route_id, change_id)
);
CREATE INDEX IF NOT EXISTS delivery_receipts_delivered_at
    ON delivery_receipts(delivered_at);
CREATE TABLE IF NOT EXISTS baseline_receipts (
    route_id TEXT PRIMARY KEY,
    delivered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    route_id TEXT NOT NULL,
    change_id TEXT NOT NULL,
    delivery TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    PRIMARY KEY(route_id, change_id)
);
CREATE INDEX IF NOT EXISTS outbox_route_queued_at
    ON outbox(route_id, queued_at);
"""
_SCHEMA_COLUMNS = {
    "baseline_receipts": ("route_id", "delivered_at"),
    "delivery_receipts": ("route_id", "change_id", "delivered_at"),
    "meta": ("key", "value"),
    "outbox": ("route_id", "change_id", "delivery", "queued_at"),
}


@dataclass(frozen=True, slots=True)
class StateSummary:
    """Operational view of persisted monitoring state."""

    initialized: bool
    updated_at: datetime | None
    source_texts: int
    delivery_receipts: int
    baseline_receipts: int
    pending_deliveries: int
    pending_routes: int


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"{label} is not a valid timestamp") from exc
    if parsed.utcoffset() is None:
        raise RuntimeError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _string(value: JsonValue, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{label} must be a non-empty string")
    return value


def _optional_string(value: JsonValue, label: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise RuntimeError(f"{label} must be a string or null")
    return value


def _serialize_delivery(delivery: Delivery) -> str:
    change = delivery.change
    value = {
        "key": change.key,
        "locale": change.locale,
        "actor": change.actor,
        "old_value": change.old_value,
        "new_value": change.new_value,
        "action": change.action,
        "occurred_at": change.iso_timestamp,
        "url": change.url,
        "actor_url": change.actor_url,
        "actor_avatar_url": change.actor_avatar_url,
        "source_text": delivery.source_text,
    }
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _deserialize_delivery(change_id: str, raw: str) -> Delivery:
    try:
        value: object = json.loads(raw)
        payload = normalize_json(value, "Stored outbox delivery")
    except ValueError as exc:
        raise RuntimeError("Stored outbox delivery is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Stored outbox delivery must be a JSON object")
    required = {
        "key",
        "locale",
        "actor",
        "old_value",
        "new_value",
        "action",
        "occurred_at",
        "url",
        "actor_url",
        "actor_avatar_url",
        "source_text",
    }
    if payload.keys() != required:
        raise RuntimeError("Stored outbox delivery has invalid fields")
    action_value = _string(payload["action"], "Stored outbox action")
    if action_value not in ACTIONS:
        raise RuntimeError(f"Stored outbox action is invalid: {action_value!r}")
    action: Action = action_value
    try:
        change = Change(
            id=change_id,
            key=_string(payload["key"], "Stored outbox key"),
            locale=_string(payload["locale"], "Stored outbox locale"),
            actor=_string(payload["actor"], "Stored outbox actor"),
            old_value=_optional_string(payload["old_value"], "Stored outbox old value"),
            new_value=_optional_string(payload["new_value"], "Stored outbox new value"),
            action=action,
            occurred_at=_parse_timestamp(
                _string(payload["occurred_at"], "Stored outbox occurrence time"),
                "Stored outbox occurrence time",
            ),
            url=_optional_string(payload["url"], "Stored outbox URL"),
            actor_url=_optional_string(payload["actor_url"], "Stored outbox actor URL"),
            actor_avatar_url=_optional_string(
                payload["actor_avatar_url"], "Stored outbox actor avatar URL"
            ),
        )
        return Delivery(
            change,
            _optional_string(payload["source_text"], "Stored outbox source text"),
        )
    except ValueError as exc:
        raise RuntimeError(f"Stored outbox delivery is invalid: {exc}") from exc


@contextmanager
def _database_errors() -> Generator[None, None, None]:
    try:
        yield
    except sqlite3.Error as exc:
        raise RuntimeError(f"State database failure: {exc}") from None


class SQLiteState:
    """SQLite-backed checkpoints, pending deliveries, and delivery receipts."""

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self._read_only: bool = read_only
        self._db: sqlite3.Connection
        with _database_errors():
            if read_only and path.is_file():
                self._db = sqlite3.connect(
                    f"{path.resolve().as_uri()}?mode=ro", uri=True
                )
                self._db.execute("PRAGMA query_only=ON")
                self._validate_schema()
                return

            if read_only:
                # A first preview needs an empty schema without creating state.
                self._db = sqlite3.connect(":memory:")
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._db = sqlite3.connect(path)
                if self._tables():
                    self._validate_schema()
                self._db.execute("PRAGMA journal_mode=WAL")
                self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def load(self) -> Checkpoint | None:
        with _database_errors():
            cursor = self._get("cursor")
            if cursor is None:
                return None
            texts = self._get("source_texts", {})
            if not isinstance(texts, dict):
                raise RuntimeError("Stored source-text snapshot is invalid")
            source_texts: dict[str, str] = {}
            for key, value in texts.items():
                if not isinstance(value, str):
                    raise RuntimeError("Stored source-text snapshot is invalid")
                source_texts[key] = value
            try:
                return Checkpoint(cursor, source_texts)
            except ValueError as exc:
                raise RuntimeError(f"Stored checkpoint is invalid: {exc}") from exc

    def save(self, checkpoint: Checkpoint) -> None:
        with self._transaction():
            self._write_checkpoint(checkpoint, _now())

    def reset(
        self,
        mode: ResetMode,
        checkpoint: Checkpoint,
        acknowledged_routes: Sequence[str],
    ) -> None:
        """Apply one reset mode and establish the supplied baseline."""

        with self._transaction():
            if mode == "full":
                for table in (
                    "meta",
                    "delivery_receipts",
                    "baseline_receipts",
                    "outbox",
                ):
                    self._db.execute(f"DELETE FROM {table}")
            now = _now()
            self._write_checkpoint(checkpoint, now)
            self._db.executemany(
                "INSERT OR IGNORE INTO baseline_receipts(route_id, delivered_at) "
                "VALUES(?, ?)",
                ((route_id, now.isoformat()) for route_id in acknowledged_routes),
            )

    def collect(
        self,
        checkpoint: Checkpoint,
        deliveries: Mapping[str, Sequence[Delivery]],
        observed_at: datetime,
    ) -> None:
        """Atomically advance the source and enqueue matching route deliveries."""

        if observed_at.utcoffset() is None:
            raise ValueError("Collection time must include a timezone")
        queued_at = observed_at.astimezone(UTC).isoformat()
        with self._transaction():
            self._write_checkpoint(checkpoint, observed_at)
            route_ids = tuple(deliveries)
            if route_ids:
                placeholders = ",".join("?" for _ in route_ids)
                self._db.execute(
                    f"DELETE FROM outbox WHERE route_id NOT IN ({placeholders})",
                    route_ids,
                )
                self._db.execute(
                    f"DELETE FROM baseline_receipts "
                    f"WHERE route_id NOT IN ({placeholders})",
                    route_ids,
                )
            else:
                self._db.execute("DELETE FROM outbox")
                self._db.execute("DELETE FROM baseline_receipts")
            for route_id, items in deliveries.items():
                for delivery in items:
                    if delivery.change_ids != (delivery.change.id,):
                        raise ValueError("Only raw deliveries can enter the outbox")
                    self._db.execute(
                        """
                        INSERT OR IGNORE INTO outbox(
                            route_id, change_id, delivery, queued_at
                        )
                        SELECT ?, ?, ?, ?
                        WHERE NOT EXISTS (
                            SELECT 1 FROM delivery_receipts
                            WHERE route_id = ? AND change_id = ?
                        )
                        """,
                        (
                            route_id,
                            delivery.change.id,
                            _serialize_delivery(delivery),
                            queued_at,
                            route_id,
                            delivery.change.id,
                        ),
                    )

    def pending(self, route_id: str) -> tuple[QueuedDelivery, ...]:
        with _database_errors():
            rows = self._db.execute(
                """
                SELECT change_id, delivery, queued_at
                FROM outbox
                WHERE route_id = ?
                ORDER BY queued_at, rowid
                """,
                (route_id,),
            ).fetchall()
        return tuple(
            QueuedDelivery(
                _deserialize_delivery(
                    _string(change_id, "Stored outbox change ID"),
                    _string(delivery, "Stored outbox delivery"),
                ),
                _parse_timestamp(
                    _string(queued_at, "Stored outbox queued time"),
                    "Stored outbox queued time",
                ),
            )
            for change_id, delivery, queued_at in rows
        )

    def acknowledge(
        self,
        route_id: str,
        deliveries: Sequence[Delivery],
    ) -> None:
        """Record deliveries completed by an external transport."""

        if not deliveries:
            return
        change_ids = tuple(
            change_id for delivery in deliveries for change_id in delivery.change_ids
        )
        with self._transaction():
            delivered_at = _now().isoformat()
            self._db.executemany(
                """
                INSERT OR IGNORE INTO delivery_receipts(
                    route_id, change_id, delivered_at
                ) VALUES(?, ?, ?)
                """,
                ((route_id, change_id, delivered_at) for change_id in change_ids),
            )
            self._delete_pending(route_id, change_ids)

    def discard(self, route_id: str, change_ids: Sequence[str]) -> None:
        if not change_ids:
            return
        with self._transaction():
            self._delete_pending(route_id, change_ids)

    def baseline_acknowledged(self, route_id: str) -> bool:
        with _database_errors():
            return (
                self._db.execute(
                    "SELECT 1 FROM baseline_receipts WHERE route_id = ?",
                    (route_id,),
                ).fetchone()
                is not None
            )

    def acknowledge_baseline(self, route_id: str) -> None:
        """Record a baseline notice completed by an external transport."""

        with self._transaction():
            self._db.execute(
                "INSERT OR IGNORE INTO baseline_receipts(route_id, delivered_at) "
                "VALUES(?, ?)",
                (route_id, _now().isoformat()),
            )

    def prune(self, retention_days: int) -> None:
        if type(retention_days) is not int or retention_days < 0:
            raise ValueError("retention_days must be a non-negative integer")
        with self._transaction():
            cutoff = (_now() - timedelta(days=retention_days)).isoformat()
            self._db.execute(
                "DELETE FROM delivery_receipts WHERE delivered_at < ?", (cutoff,)
            )

    def summary(self) -> StateSummary:
        with _database_errors():
            checkpoint = self.load()
            updated = self._get("updated_at")
            if updated is not None and not isinstance(updated, str):
                raise RuntimeError("Stored state 'updated_at' is not text")
            receipts = self._db.execute(
                "SELECT COUNT(*) FROM delivery_receipts"
            ).fetchone()
            routes = self._db.execute(
                "SELECT COUNT(*) FROM baseline_receipts"
            ).fetchone()
            pending = self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()
            pending_routes = self._db.execute(
                "SELECT COUNT(DISTINCT route_id) FROM outbox"
            ).fetchone()
            return StateSummary(
                initialized=checkpoint is not None,
                updated_at=(
                    None
                    if updated is None
                    else _parse_timestamp(updated, "Stored state update time")
                ),
                source_texts=(
                    0 if checkpoint is None else len(checkpoint.source_texts)
                ),
                delivery_receipts=int(receipts[0]),
                baseline_receipts=int(routes[0]),
                pending_deliveries=int(pending[0]),
                pending_routes=int(pending_routes[0]),
            )

    def close(self) -> None:
        with _database_errors():
            self._db.close()

    def _ensure_writable(self) -> None:
        if self._read_only:
            raise RuntimeError("State is read-only")

    @contextmanager
    def _transaction(self) -> Generator[None, None, None]:
        self._ensure_writable()
        with _database_errors(), self._db:
            yield

    def _write_checkpoint(self, checkpoint: Checkpoint, updated_at: datetime) -> None:
        values = {
            "cursor": checkpoint.cursor,
            "source_texts": checkpoint.source_texts,
            "updated_at": updated_at.astimezone(UTC).isoformat(),
        }
        self._db.executemany(
            """
            INSERT INTO meta(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            WHERE meta.value <> excluded.value
            """,
            (
                (
                    key,
                    json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                )
                for key, value in values.items()
            ),
        )

    def _delete_pending(self, route_id: str, change_ids: Sequence[str]) -> None:
        self._db.executemany(
            "DELETE FROM outbox WHERE route_id = ? AND change_id = ?",
            ((route_id, change_id) for change_id in change_ids),
        )

    def _tables(self) -> set[str]:
        return {
            str(row[0])
            for row in self._db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    def _validate_schema(self) -> None:
        if self._tables() != _SCHEMA_COLUMNS.keys():
            raise RuntimeError("State database schema is incompatible")
        for table, expected in _SCHEMA_COLUMNS.items():
            columns = tuple(
                str(row[1]) for row in self._db.execute(f"PRAGMA table_info({table})")
            )
            if columns != expected:
                raise RuntimeError("State database schema is incompatible")

    def _get(self, key: str, default: JsonValue = None) -> JsonValue:
        row = self._db.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return default
        raw: object = row[0]
        if not isinstance(raw, str):
            raise RuntimeError(f"Stored state {key!r} is not JSON text")
        try:
            value: object = json.loads(raw)
            return normalize_json(value, f"Stored state {key!r}")
        except ValueError as exc:
            raise RuntimeError(f"Stored state {key!r} is invalid JSON") from exc
