"""Monitoring policy and its infrastructure contracts."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .models import (
    Change,
    Checkpoint,
    Delivery,
    DeliveryPolicy,
    JsonValue,
    PollResult,
    QueuedDelivery,
    ResetMode,
)
from .sources import Source

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RenderedNotification:
    """One side-effect-free notification preview."""

    route_id: str
    content: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class MonitorPreview:
    """Structured result of one side-effect-free monitoring preview."""

    baseline: bool
    scanned: int
    source_texts: int
    notifications: tuple[RenderedNotification, ...] = ()


class StateStore(Protocol):
    """Checkpoint, outbox, and delivery persistence boundary."""

    def load(self) -> Checkpoint | None: ...

    def save(self, checkpoint: Checkpoint, /) -> None: ...

    def reset(
        self,
        mode: ResetMode,
        checkpoint: Checkpoint,
        notified_routes: Sequence[str],
        /,
    ) -> None:
        """Apply one reset mode and establish the supplied baseline."""

        ...

    def collect(
        self,
        checkpoint: Checkpoint,
        deliveries: Mapping[str, Sequence[Delivery]],
        observed_at: datetime,
        /,
    ) -> None:
        """Atomically advance the checkpoint and enqueue route deliveries."""

        ...

    def pending(self, route_id: str, /) -> tuple[QueuedDelivery, ...]: ...

    def acknowledge(self, route_id: str, change_ids: Sequence[str], /) -> None: ...

    def discard(self, route_id: str, change_ids: Sequence[str], /) -> None: ...

    def baseline_notified(self, route_id: str, /) -> bool: ...

    def record_baseline(self, route_id: str, /) -> None: ...

    def prune(self, retention_days: int, /) -> None: ...


class Route(Protocol):
    """Change subscription and delivery policy for one target."""

    @property
    def id(self) -> str: ...

    @property
    def delivery(self) -> DeliveryPolicy: ...

    @property
    def notify_baseline(self) -> bool: ...

    def accepts_locale(self, locale: str, /) -> bool: ...

    def matches(self, change: Change, /) -> bool: ...


class Notifier(Protocol):
    """Route-aware rendering and delivery boundary."""

    @property
    def routes(self) -> Sequence[Route]: ...

    def send(
        self,
        route_id: str,
        deliveries: Sequence[Delivery],
        acknowledge: Callable[[Sequence[Delivery]], None],
        /,
    ) -> None:
        """Deliver a route batch and acknowledge each successful request."""

        ...

    def render(
        self, route_id: str, deliveries: Sequence[Delivery], /
    ) -> Sequence[Mapping[str, JsonValue]]:
        """Render a route batch without delivery or persistence side effects."""

        ...

    def send_baseline(
        self, route_id: str, changes: int, source_texts: int, /
    ) -> None: ...


def _now() -> datetime:
    return datetime.now(UTC)


def _advance_source_texts(
    previous: Mapping[str, str],
    result: PollResult,
) -> dict[str, str]:
    if result.source_texts is not None:
        return dict(result.source_texts)
    current = dict(previous)
    for change in result.changes:
        if not change.is_source:
            continue
        if change.action == "deleted":
            current.pop(change.key, None)
        elif change.new_value is None:
            raise RuntimeError("Source change does not contain its current text")
        else:
            current[change.key] = change.new_value
    return current


def _change_ids(deliveries: Sequence[Delivery]) -> tuple[str, ...]:
    return tuple(
        change_id for delivery in deliveries for change_id in delivery.change_ids
    )


def _fold(
    deliveries: Sequence[Delivery],
) -> tuple[tuple[Delivery, ...], tuple[str, ...]]:
    """Fold each locale/key history into its net delivery and discarded IDs."""

    groups: dict[tuple[str, str], list[Delivery]] = {}
    for delivery in deliveries:
        change = delivery.change
        groups.setdefault((change.locale, change.key), []).append(delivery)

    selected: list[Delivery] = []
    discarded: list[str] = []
    absent = object()
    for group in groups.values():
        ordered = sorted(group, key=lambda item: item.change.occurred_at)
        first, last = ordered[0], ordered[-1]
        initial: object = (
            absent if first.change.action == "added" else first.change.old_value
        )
        final: object = (
            absent if last.change.action == "deleted" else last.change.new_value
        )
        ids = _change_ids(ordered)

        if (initial is absent and final is absent) or initial == final:
            discarded.extend(ids)
            continue
        if initial is absent:
            action = "added"
            old_value = None
            new_value = str(final)
        elif final is absent:
            action = "deleted"
            old_value = str(initial)
            new_value = None
        else:
            action = "modified"
            old_value = str(initial)
            new_value = str(final)

        change = replace(
            last.change,
            action=action,
            old_value=old_value,
            new_value=new_value,
        )
        if change.is_source:
            source_text = old_value if action == "deleted" else new_value
        else:
            source_text = next(
                (
                    item.source_text
                    for item in reversed(ordered)
                    if item.source_text is not None
                ),
                None,
            )
        selected.append(Delivery(change, source_text, ids))

    selected.sort(key=lambda item: item.change.occurred_at)
    return tuple(selected), tuple(discarded)


class Monitor:
    """Collect changes durably, then deliver mature route batches."""

    def __init__(
        self,
        source: Source,
        state: StateStore,
        notifier: Notifier,
        *,
        retention_days: int,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        if type(retention_days) is not int or retention_days < 0:
            raise ValueError("retention_days must be a non-negative integer")
        self._source: Source = source
        self._state: StateStore = state
        self._notifier: Notifier = notifier
        self._retention_days: int = retention_days
        self._clock: Callable[[], datetime] = clock

    def run(self, *, flush: bool = False) -> None:
        """Collect and deliver one monitoring cycle."""

        checkpoint = self._state.load()
        result = self._source.poll(None if checkpoint is None else checkpoint.cursor)
        if checkpoint is None:
            self._create_baseline(result)
            return

        if result.changes:
            _LOGGER.info("Found %d new changes", len(result.changes))
        current = _advance_source_texts(checkpoint.source_texts, result)
        observed_at = self._clock()
        if observed_at.utcoffset() is None:
            raise ValueError("Monitor clock must return a timezone-aware datetime")
        queued = self._route_deliveries(
            result.changes,
            current,
            checkpoint.source_texts,
        )
        self._send_baselines(result.scanned, len(current))
        self._state.collect(Checkpoint(result.cursor, current), queued, observed_at)
        self._flush(observed_at, force=flush)
        self._state.prune(self._retention_days)
        collected = sum(len(items) for items in queued.values())
        if collected:
            _LOGGER.info("Collected %d route deliveries", collected)
        else:
            _LOGGER.debug("Collection completed without routed changes")

    def preview(self) -> MonitorPreview:
        """Render one monitoring cycle without changing state or sending."""

        checkpoint = self._state.load()
        result = self._source.poll(None if checkpoint is None else checkpoint.cursor)
        texts = result.source_texts
        if checkpoint is None:
            if texts is None:
                raise RuntimeError("Initial source poll did not include source texts")
            return MonitorPreview(True, result.scanned, len(texts))
        current = _advance_source_texts(checkpoint.source_texts, result)
        collected = self._route_deliveries(
            result.changes,
            current,
            checkpoint.source_texts,
        )
        notifications: list[RenderedNotification] = []
        for route in self._notifier.routes:
            deliveries = [queued.delivery for queued in self._state.pending(route.id)]
            deliveries.extend(collected.get(route.id, ()))
            selected, _ = self._prepare(route, deliveries)
            notifications.extend(
                RenderedNotification(route.id, dict(content))
                for content in self._notifier.render(route.id, selected)
            )
        return MonitorPreview(
            False,
            result.scanned,
            len(current),
            tuple(notifications),
        )

    def reset(self, mode: ResetMode, *, notify: bool = False) -> None:
        """Establish a fresh baseline under the selected reset mode."""

        result = self._source.poll(None)
        texts = result.source_texts
        if texts is None:
            raise RuntimeError("Initial source poll did not include source texts")
        routes = self._reset_routes(result.scanned, len(texts), notify)
        self._state.reset(
            mode,
            Checkpoint(result.cursor, texts),
            routes,
        )
        self._state.prune(self._retention_days)
        _LOGGER.info(
            "%s reset created a baseline from %d changes and %d source texts",
            mode,
            result.scanned,
            len(texts),
        )

    def _create_baseline(self, result: PollResult) -> None:
        texts = result.source_texts
        if texts is None:
            raise RuntimeError("Initial source poll did not include source texts")
        self._send_baselines(result.scanned, len(texts))
        self._state.save(Checkpoint(result.cursor, texts))
        self._state.prune(self._retention_days)
        _LOGGER.info(
            "Baseline created from %d changes and %d source texts",
            result.scanned,
            len(texts),
        )

    def _reset_routes(
        self,
        changes: int,
        source_texts: int,
        notify: bool,
    ) -> tuple[str, ...]:
        routes: list[str] = []
        for route in self._notifier.routes:
            if not route.notify_baseline:
                continue
            if notify:
                self._notifier.send_baseline(route.id, changes, source_texts)
            routes.append(route.id)
        return tuple(routes)

    def _send_baselines(self, changes: int, source_texts: int) -> None:
        for route in self._notifier.routes:
            if not route.notify_baseline or self._state.baseline_notified(route.id):
                continue
            self._notifier.send_baseline(route.id, changes, source_texts)
            self._state.record_baseline(route.id)

    def _route_deliveries(
        self,
        changes: Sequence[Change],
        current: Mapping[str, str],
        previous: Mapping[str, str],
    ) -> dict[str, list[Delivery]]:
        routes = self._notifier.routes
        queued: dict[str, list[Delivery]] = {route.id: [] for route in routes}
        for change in changes:
            matching = tuple(
                route
                for route in routes
                if (
                    route.accepts_locale(change.locale)
                    if route.delivery.fold_changes
                    else route.matches(change)
                )
            )
            if not matching:
                continue
            delivery = Delivery.from_change(change, current, previous)
            for route in matching:
                queued[route.id].append(delivery)
        return queued

    def _prepare(
        self, route: Route, deliveries: Sequence[Delivery]
    ) -> tuple[tuple[Delivery, ...], tuple[str, ...]]:
        """Fold histories before filtering because folding can change the action."""

        if route.delivery.fold_changes:
            prepared, discarded = _fold(deliveries)
        else:
            prepared, discarded = tuple(deliveries), ()
        selected: list[Delivery] = []
        ignored = list(discarded)
        for delivery in prepared:
            if route.matches(delivery.change):
                selected.append(delivery)
            else:
                ignored.extend(delivery.change_ids)
        return tuple(selected), tuple(ignored)

    def _flush(self, now: datetime, *, force: bool) -> None:
        for route in self._notifier.routes:
            queued = self._state.pending(route.id)
            if not queued or (
                not force and not self._ready(queued, route.delivery, now)
            ):
                continue
            deliveries, discarded = self._prepare(
                route, tuple(item.delivery for item in queued)
            )
            self._state.discard(route.id, discarded)
            if not deliveries:
                continue

            def acknowledge(sent: Sequence[Delivery], route_id: str = route.id) -> None:
                self._state.acknowledge(route_id, _change_ids(sent))

            self._notifier.send(route.id, deliveries, acknowledge)
            _LOGGER.info("Delivered %d changes to route %s", len(deliveries), route.id)

    @staticmethod
    def _ready(
        queued: Sequence[QueuedDelivery], policy: DeliveryPolicy, now: datetime
    ) -> bool:
        """Return whether inactivity or the maximum wait has matured the batch."""

        first = min(item.queued_at for item in queued)
        last = max(item.queued_at for item in queued)
        quiet_at = last + timedelta(seconds=policy.quiet_seconds)
        forced_at = first + timedelta(seconds=policy.max_wait_seconds)
        return now >= quiet_at or now >= forced_at
