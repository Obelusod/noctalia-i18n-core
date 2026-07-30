"""Monitoring policy tests using in-memory contracts."""

from __future__ import annotations

import unittest
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from noctalia_i18n_core.models import (
    Action,
    Change,
    Checkpoint,
    Delivery,
    DeliveryPolicy,
    JsonValue,
    PollResult,
    QueuedDelivery,
    ResetMode,
)
from noctalia_i18n_core.monitor import Monitor

from .fixtures import RUN_AT, fixture_change


@dataclass(frozen=True, slots=True)
class _Route:
    id: str
    locales: frozenset[str] = frozenset({"en", "zh-Hans"})
    actions: frozenset[Action] = frozenset({"added", "modified", "deleted"})
    notify_baseline: bool = True
    delivery: DeliveryPolicy = field(default_factory=lambda: DeliveryPolicy(0, 0, True))

    def accepts_locale(self, locale: str) -> bool:
        return "*" in self.locales or locale in self.locales

    def matches(self, change: Change) -> bool:
        return self.accepts_locale(change.locale) and change.action in self.actions


class _Source:
    def __init__(self, result: PollResult, source_texts: dict[str, str] | None) -> None:
        self.result = result
        self.source_texts = source_texts
        self.polled_with: list[JsonValue | None] = []
        self.source_text_reads = 0

    def poll(self, cursor: JsonValue | None) -> PollResult:
        self.polled_with.append(cursor)
        include_texts = cursor is None or (
            bool(self.result.changes) and self.source_texts is not None
        )
        if include_texts:
            self.source_text_reads += 1
        return replace(
            self.result,
            source_texts=self.source_texts if include_texts else None,
        )


class _State:
    def __init__(self, checkpoint: Checkpoint | None = None) -> None:
        self.checkpoint = checkpoint
        self.receipts: set[tuple[str, str]] = set()
        self.baseline_receipts: set[str] = set()
        self.outbox: dict[str, list[QueuedDelivery]] = {}
        self.pruned: list[int] = []

    def load(self) -> Checkpoint | None:
        return self.checkpoint

    def save(self, checkpoint: Checkpoint) -> None:
        self.checkpoint = checkpoint

    def reset(
        self,
        mode: ResetMode,
        checkpoint: Checkpoint,
        acknowledged_routes: Sequence[str],
    ) -> None:
        if mode == "full":
            self.receipts.clear()
            self.baseline_receipts.clear()
            self.outbox.clear()
        self.checkpoint = checkpoint
        self.baseline_receipts.update(acknowledged_routes)

    def collect(
        self,
        checkpoint: Checkpoint,
        deliveries: Mapping[str, Sequence[Delivery]],
        observed_at: datetime,
    ) -> None:
        self.checkpoint = checkpoint
        active_routes = set(deliveries)
        self.outbox = {
            route_id: queued
            for route_id, queued in self.outbox.items()
            if route_id in active_routes
        }
        self.baseline_receipts.intersection_update(active_routes)
        for route_id, items in deliveries.items():
            queued = self.outbox.setdefault(route_id, [])
            known = {item.delivery.change.id for item in queued}
            for delivery in items:
                if (
                    delivery.change.id not in known
                    and (route_id, delivery.change.id) not in self.receipts
                ):
                    queued.append(QueuedDelivery(delivery, observed_at))

    def pending(self, route_id: str) -> tuple[QueuedDelivery, ...]:
        return tuple(self.outbox.get(route_id, ()))

    def acknowledge(
        self,
        route_id: str,
        deliveries: Sequence[Delivery],
    ) -> None:
        change_ids = tuple(
            change_id for delivery in deliveries for change_id in delivery.change_ids
        )
        self.receipts.update((route_id, change_id) for change_id in change_ids)
        self._remove(route_id, change_ids)

    def discard(self, route_id: str, change_ids: Sequence[str]) -> None:
        self._remove(route_id, change_ids)

    def baseline_acknowledged(self, route_id: str) -> bool:
        return route_id in self.baseline_receipts

    def acknowledge_baseline(self, route_id: str) -> None:
        self.baseline_receipts.add(route_id)

    def prune(self, retention_days: int) -> None:
        self.pruned.append(retention_days)

    def _remove(self, route_id: str, change_ids: Sequence[str]) -> None:
        removed = set(change_ids)
        self.outbox[route_id] = [
            item
            for item in self.outbox.get(route_id, ())
            if item.delivery.change.id not in removed
        ]


def _policy(
    quiet_seconds: int = 0,
    max_wait_seconds: int = 0,
    *,
    fold_changes: bool = True,
) -> DeliveryPolicy:
    return DeliveryPolicy(quiet_seconds, max_wait_seconds, fold_changes)


def _monitor(
    source: _Source,
    state: _State,
    routes: Sequence[_Route] = (_Route("main"),),
    now: datetime = RUN_AT,
) -> Monitor:
    return Monitor(
        source,
        state,
        routes,
        retention_days=180,
        clock=lambda: now,
    )


class MonitorTests(unittest.TestCase):
    def test_routes_mature_independently_and_flush_overrides_the_window(self) -> None:
        changed = fixture_change(id="changed")
        routes = (
            _Route("immediate"),
            _Route("delayed", delivery=_policy(180, 900)),
        )
        state = _State(Checkpoint("old", {}))
        first = _monitor(
            _Source(PollResult((changed,), "next", 1), {changed.key: "English"}),
            state,
            routes,
        ).run()
        self.assertEqual(tuple(first.deliveries), ("immediate",))

        later = _monitor(
            _Source(PollResult((), "later", 0), {}),
            state,
            routes,
            RUN_AT + timedelta(seconds=180),
        ).run()
        self.assertEqual(tuple(later.deliveries), ("immediate", "delayed"))

        forced_state = _State(Checkpoint("old", {}))
        forced = _monitor(
            _Source(PollResult((changed,), "next", 1), {changed.key: "English"}),
            forced_state,
            (_Route("delayed", delivery=_policy(180, 900)),),
        ).run(flush=True)
        self.assertEqual(tuple(forced.deliveries), ("delayed",))

    def test_maximum_wait_matures_a_continuously_updated_batch(self) -> None:
        route = _Route("main", delivery=_policy(180, 240))
        state = _State(Checkpoint("old", {}))
        first = fixture_change(id="first", old_value="A", new_value="B")
        changes = (
            (RUN_AT, first),
            (
                RUN_AT + timedelta(seconds=120),
                fixture_change(
                    id="second",
                    old_value="B",
                    new_value="C",
                    occurred_at=first.occurred_at + timedelta(seconds=120),
                ),
            ),
            (
                RUN_AT + timedelta(seconds=220),
                fixture_change(
                    id="third",
                    old_value="C",
                    new_value="D",
                    occurred_at=first.occurred_at + timedelta(seconds=220),
                ),
            ),
        )
        for index, (now, change) in enumerate(changes):
            result = _monitor(
                _Source(
                    PollResult((change,), f"cursor-{index}", 1),
                    {first.key: "English"},
                ),
                state,
                (route,),
                now,
            ).run()
            self.assertEqual(result.deliveries, {})

        result = _monitor(
            _Source(PollResult((), "final", 0), {}),
            state,
            (route,),
            RUN_AT + timedelta(seconds=240),
        ).run()
        self.assertEqual(len(result.deliveries["main"]), 1)

    def test_folding_resolves_net_changes_and_discards_no_ops(self) -> None:
        cases = (
            (
                fixture_change(
                    id="added", action="added", old_value=None, new_value="A"
                ),
                fixture_change(id="modified", old_value="A", new_value="B"),
                ("added", None, "B"),
            ),
            (
                fixture_change(id="modified", old_value="A", new_value="B"),
                fixture_change(
                    id="deleted", action="deleted", old_value="B", new_value=None
                ),
                ("deleted", "A", None),
            ),
            (
                fixture_change(
                    id="deleted", action="deleted", old_value="A", new_value=None
                ),
                fixture_change(
                    id="added", action="added", old_value=None, new_value="B"
                ),
                ("modified", "A", "B"),
            ),
        )
        for first, second, expected in cases:
            later = replace(
                second,
                occurred_at=first.occurred_at + timedelta(seconds=1),
            )
            with self.subTest(actions=(first.action, later.action)):
                result = _monitor(
                    _Source(
                        PollResult((first, later), "next", 2),
                        {first.key: "English"},
                    ),
                    _State(Checkpoint("old", {})),
                ).run()
                change = result.deliveries["main"][0].change
                self.assertEqual(
                    (change.action, change.old_value, change.new_value),
                    expected,
                )

        added = fixture_change(
            id="added", action="added", old_value=None, new_value="A"
        )
        deleted = fixture_change(
            id="deleted",
            action="deleted",
            old_value="A",
            new_value=None,
            occurred_at=added.occurred_at + timedelta(seconds=1),
        )
        state = _State(Checkpoint("old", {}))
        result = _monitor(
            _Source(PollResult((added, deleted), "next", 2), {added.key: "English"}),
            state,
        ).run()
        self.assertEqual(result.deliveries, {})
        self.assertEqual(state.pending("main"), ())

        modified = fixture_change(
            id="modified",
            old_value="A",
            new_value="B",
            occurred_at=added.occurred_at + timedelta(seconds=1),
        )
        filtered = _monitor(
            _Source(
                PollResult((added, modified), "next", 2),
                {added.key: "English"},
            ),
            _State(Checkpoint("old", {})),
            (_Route("main", actions=frozenset({"added"})),),
        ).run()
        self.assertEqual(filtered.deliveries["main"][0].change.new_value, "B")

    def test_unfolded_routes_queue_only_matching_changes(self) -> None:
        modified = fixture_change(id="modified")
        added = fixture_change(
            id="added",
            key="other.key",
            action="added",
            old_value=None,
            occurred_at=modified.occurred_at + timedelta(seconds=1),
        )
        route = _Route(
            "main",
            actions=frozenset({"modified"}),
            delivery=_policy(fold_changes=False),
        )
        result = _monitor(
            _Source(
                PollResult((modified, added), "next", 2),
                {modified.key: "English", added.key: "Other"},
            ),
            _State(Checkpoint("old", {})),
            (route,),
        ).run()
        self.assertEqual(
            [delivery.change.id for delivery in result.deliveries["main"]],
            ["modified"],
        )

    def test_run_returns_durable_batches_until_the_application_acknowledges(
        self,
    ) -> None:
        first = fixture_change(id="first")
        second = fixture_change(
            id="second",
            key="other.key",
            occurred_at=first.occurred_at + timedelta(seconds=1),
        )
        route = _Route("main", delivery=_policy(fold_changes=False))
        state = _State(Checkpoint("old", {}))
        monitor = _monitor(
            _Source(
                PollResult((first, second), "next", 2),
                {first.key: "First", second.key: "Second"},
            ),
            state,
            (route,),
        )
        result = monitor.run()
        state.acknowledge("main", result.deliveries["main"][:1])

        retry = _monitor(
            _Source(PollResult((), "later", 0), {}),
            state,
            (route,),
        ).run()
        self.assertEqual(
            [delivery.change.id for delivery in retry.deliveries["main"]],
            ["second"],
        )
        self.assertEqual(
            state.checkpoint,
            Checkpoint("later", {first.key: "First", second.key: "Second"}),
        )

    def test_baseline_notices_remain_pending_until_acknowledged(self) -> None:
        routes = (_Route("first"), _Route("second"))
        state = _State()
        monitor = _monitor(
            _Source(PollResult((), {"opaque": "cursor"}, 125), {"key": "English"}),
            state,
            routes,
        )
        result = monitor.run()
        self.assertTrue(result.baseline)
        self.assertEqual(result.baseline_routes, ("first", "second"))
        self.assertEqual(
            state.checkpoint,
            Checkpoint({"opaque": "cursor"}, {"key": "English"}),
        )

        state.acknowledge_baseline("first")
        retry = _monitor(
            _Source(PollResult((), "next", 0), {}),
            state,
            routes,
        ).run()
        self.assertEqual(retry.baseline_routes, ("second",))

    def test_preview_reports_complete_work_without_state_changes(self) -> None:
        changed = fixture_change(id="changed")
        checkpoint = Checkpoint("old", {changed.key: "Previous"})
        state = _State(checkpoint)
        result = _monitor(
            _Source(PollResult((changed,), "next", 1), {changed.key: "Current"}),
            state,
        ).preview()
        self.assertEqual(state.checkpoint, checkpoint)
        self.assertEqual(state.outbox, {})
        self.assertEqual(
            [delivery.change.id for delivery in result.deliveries["main"]],
            ["changed"],
        )

        delayed = _monitor(
            _Source(PollResult((changed,), "next", 1), {changed.key: "Current"}),
            state,
            (_Route("delayed", delivery=_policy(60, 600)),),
        )
        self.assertEqual(delayed.preview().deliveries, {})
        self.assertEqual(tuple(delayed.preview(flush=True).deliveries), ("delayed",))

        empty_state = _State()
        baseline = _monitor(
            _Source(PollResult((), "cursor", 7), {"key": "English"}),
            empty_state,
        ).preview()
        self.assertTrue(baseline.baseline)
        self.assertEqual(baseline.baseline_routes, ("main",))
        self.assertIsNone(empty_state.checkpoint)

    def test_reset_modes_control_delivery_and_baseline_receipts(self) -> None:
        state = _State(Checkpoint("old", {}))
        state.receipts.add(("old", "delivered"))
        state.baseline_receipts.add("old")
        state.outbox["old"] = [
            QueuedDelivery(Delivery(fixture_change(), "Source"), RUN_AT)
        ]
        source = _Source(PollResult((), "fresh", 3), {})

        baseline = _monitor(source, state).reset("baseline")
        self.assertEqual(source.polled_with, [None])
        self.assertEqual(baseline.baseline_routes, ())
        self.assertEqual(state.receipts, {("old", "delivered")})
        self.assertEqual(state.baseline_receipts, {"old", "main"})

        full = _monitor(source, state).reset("full", notify=True)
        self.assertEqual(full.baseline_routes, ("main",))
        self.assertEqual(state.receipts, set())
        self.assertEqual(state.baseline_receipts, set())
        self.assertEqual(state.outbox, {})

    def test_source_snapshot_advances_without_fetching_an_export(self) -> None:
        changes = (
            fixture_change(
                id="modified", locale="en", old_value="Old", new_value="New"
            ),
            fixture_change(
                id="deleted",
                key="deleted.key",
                locale="en",
                action="deleted",
                old_value="Removed",
                new_value=None,
                occurred_at=fixture_change().occurred_at + timedelta(seconds=1),
            ),
        )
        source = _Source(PollResult(changes, "next", 2), None)
        state = _State(
            Checkpoint("old", {changes[0].key: "Old", changes[1].key: "Removed"})
        )
        result = _monitor(
            source,
            state,
            (_Route("target", locales=frozenset({"zh-Hans"})),),
        ).run()
        self.assertEqual(result.deliveries, {})
        self.assertEqual(source.source_text_reads, 0)
        self.assertEqual(state.checkpoint, Checkpoint("next", {changes[0].key: "New"}))

    def test_invalid_clock_and_route_ids_fail_before_collection(self) -> None:
        source = _Source(PollResult((), "next", 0), {})
        state = _State(Checkpoint("old", {}))
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            _monitor(source, state, now=RUN_AT.replace(tzinfo=None)).run()
        self.assertEqual(state.checkpoint, Checkpoint("old", {}))

        for routes in ((_Route(""),), (_Route("same"), _Route("same"))):
            with (
                self.subTest(routes=routes),
                self.assertRaisesRegex(ValueError, "Route id"),
            ):
                _monitor(source, state, routes)


if __name__ == "__main__":
    unittest.main()
