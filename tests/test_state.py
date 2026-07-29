"""SQLite state tests."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from noctalia_i18n_core.models import Checkpoint, Delivery
from noctalia_i18n_core.state import SQLiteState

from .fixtures import RUN_AT, delivery


class StateTests(unittest.TestCase):
    def test_incompatible_schema_is_rejected_without_modification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with closing(sqlite3.connect(path)) as database:
                database.executescript(
                    """
                    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE delivery_receipts (
                        route_id TEXT NOT NULL,
                        change_id TEXT NOT NULL,
                        delivered_at TEXT NOT NULL,
                        PRIMARY KEY(route_id, change_id)
                    );
                    CREATE TABLE baseline_notifications (
                        route_id TEXT PRIMARY KEY,
                        delivered_at TEXT NOT NULL
                    );
                    CREATE TABLE outbox (
                        route_id TEXT NOT NULL,
                        change_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        queued_at TEXT NOT NULL,
                        PRIMARY KEY(route_id, change_id)
                    );
                    """
                )
            original = path.read_bytes()

            with self.assertRaisesRegex(RuntimeError, "schema is incompatible"):
                SQLiteState(path)

            self.assertEqual(path.read_bytes(), original)

    def test_database_errors_do_not_escape_the_state_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            path.write_bytes(b"not a SQLite database")
            with self.assertRaisesRegex(RuntimeError, "State database failure"):
                SQLiteState(path)

            path.unlink()
            state = SQLiteState(path)
            state.close()
            with self.assertRaisesRegex(RuntimeError, "State database failure"):
                state.load()

    def test_collection_and_acknowledgement_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            state = SQLiteState(path)
            try:
                checkpoint = Checkpoint(
                    {"source": "fake", "token": "next"},
                    {"key": "English"},
                )
                primary = delivery(id="change-id")
                secondary = delivery(id="change-id")
                state.collect(
                    checkpoint,
                    {"primary": (primary,), "secondary": (secondary,)},
                    RUN_AT,
                )
                state.collect(
                    checkpoint,
                    {"primary": (primary,), "secondary": (secondary,)},
                    RUN_AT,
                )
                self.assertEqual(state.load(), checkpoint)
                state.acknowledge("primary", ("change-id", "change-id"))
                state.collect(
                    checkpoint,
                    {"primary": (primary,), "secondary": ()},
                    RUN_AT,
                )
                state.discard("secondary", ("change-id",))
                self.assertEqual(state.pending("primary"), ())
                self.assertEqual(state.pending("secondary"), ())
                self.assertFalse(state.baseline_notified("primary"))
                state.record_baseline("primary")
                state.record_baseline("primary")
                self.assertTrue(state.baseline_notified("primary"))
                summary = state.summary()
                self.assertTrue(summary.initialized)
                self.assertEqual(summary.source_texts, 1)
                self.assertEqual(summary.delivery_receipts, 1)
                self.assertEqual(summary.baseline_notifications, 1)
                self.assertEqual(summary.pending_deliveries, 0)
                self.assertEqual(summary.pending_routes, 0)
                self.assertIsNotNone(summary.updated_at)
            finally:
                state.close()

    def test_failed_collection_rolls_back_the_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = SQLiteState(Path(directory) / "state.sqlite3")
            try:
                previous = Checkpoint("previous", {})
                state.save(previous)
                item = delivery(id="folded")
                folded = Delivery(
                    item.change,
                    item.source_text,
                    ("first", "second"),
                )

                with self.assertRaisesRegex(ValueError, "raw deliveries"):
                    state.collect(
                        Checkpoint("next", {}),
                        {"main": (folded,)},
                        RUN_AT,
                    )

                self.assertEqual(state.load(), previous)
                self.assertEqual(state.pending("main"), ())
            finally:
                state.close()

    def test_pending_deliveries_survive_reopening(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            item = delivery(id="queued-change")
            checkpoint = Checkpoint("next", {item.change.key: "Source text"})
            state = SQLiteState(path)
            state.collect(checkpoint, {"main": (item,)}, RUN_AT)
            state.close()

            state = SQLiteState(path)
            try:
                pending = state.pending("main")
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0].delivery, item)
                self.assertEqual(pending[0].queued_at, RUN_AT)
                summary = state.summary()
                self.assertEqual(summary.pending_deliveries, 1)
                self.assertEqual(summary.pending_routes, 1)
            finally:
                state.close()

    def test_collection_removes_pending_data_for_deleted_routes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = SQLiteState(Path(directory) / "state.sqlite3")
            try:
                item = delivery(id="queued-change")
                checkpoint = Checkpoint("first", {})
                state.record_baseline("old")
                state.collect(checkpoint, {"old": (item,), "current": ()}, RUN_AT)
                state.collect(Checkpoint("second", {}), {"current": ()}, RUN_AT)
                self.assertEqual(state.pending("old"), ())
                self.assertFalse(state.baseline_notified("old"))
            finally:
                state.close()

    def test_reset_modes_preserve_or_clear_delivery_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = SQLiteState(Path(directory) / "state.sqlite3")
            try:
                delivered = delivery(id="delivered")
                pending = delivery(id="pending")
                state.collect(
                    Checkpoint("old", {}),
                    {"old": (delivered, pending)},
                    RUN_AT,
                )
                state.acknowledge("old", ("delivered",))
                state.record_baseline("old")

                baseline = Checkpoint("baseline", {"key": "Source"})
                state.reset("baseline", baseline, ("current",))
                self.assertEqual(state.load(), baseline)
                self.assertEqual(len(state.pending("old")), 1)
                self.assertTrue(state.baseline_notified("old"))
                self.assertTrue(state.baseline_notified("current"))
                self.assertEqual(state.summary().delivery_receipts, 1)

                cleared = Checkpoint("full", {"key": "Updated source"})
                state.reset("full", cleared, ("current",))
                self.assertEqual(state.load(), cleared)
                self.assertEqual(state.pending("old"), ())
                self.assertFalse(state.baseline_notified("old"))
                self.assertTrue(state.baseline_notified("current"))
                self.assertEqual(state.summary().delivery_receipts, 0)
            finally:
                state.close()

    def test_read_only_state_never_changes_the_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            original = Checkpoint("one", {})
            state = SQLiteState(path)
            state.save(original)
            state.close()

            read_only = SQLiteState(path, read_only=True)
            try:
                self.assertEqual(read_only.load(), original)
                with self.assertRaisesRegex(RuntimeError, "read-only"):
                    read_only.save(Checkpoint("two", {}))
            finally:
                read_only.close()

            state = SQLiteState(path)
            try:
                self.assertEqual(state.load(), original)
            finally:
                state.close()

    def test_first_read_only_run_does_not_create_a_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            state = SQLiteState(path, read_only=True)
            try:
                self.assertIsNone(state.load())
            finally:
                state.close()
            self.assertFalse(path.exists())

    def test_retention_prunes_only_expired_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            state = SQLiteState(path)
            try:
                old = (RUN_AT - timedelta(days=181)).isoformat()
                recent = (RUN_AT - timedelta(days=1)).isoformat()
                with closing(sqlite3.connect(path)) as database:
                    database.executemany(
                        "INSERT INTO delivery_receipts VALUES (?, ?, ?)",
                        (
                            ("main", suffix, timestamp)
                            for suffix, timestamp in (("old", old), ("recent", recent))
                        ),
                    )
                    database.commit()
                state.prune(180)
                with closing(sqlite3.connect(path)) as database:
                    remaining = database.execute(
                        "SELECT change_id FROM delivery_receipts"
                    ).fetchall()
                self.assertEqual(remaining, [("recent",)])
            finally:
                state.close()
