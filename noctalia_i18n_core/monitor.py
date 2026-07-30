"""Monitoring policy and its infrastructure contracts."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .models import (
    Change,
    Checkpoint,
    Delivery,
    DeliveryPolicy,
    PollResult,
    QueuedDelivery,
    ResetMode,
)
from .sources import Source

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MonitorResult:
    """Changes and baseline notices ready for an application to deliver."""

    baseline: bool
    scanned: int
    source_texts: int
    baseline_routes: tuple[str, ...] = ()
    deliveries: Mapping[str, tuple[Delivery, ...]] = field(
        default_factory=dict[str, tuple[Delivery, ...]]
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "deliveries", dict(self.deliveries))


class StateStore(Protocol):
    """Checkpoint, outbox, and delivery persistence boundary."""

    def load(self) -> Checkpoint | None: ...

    def save(self, checkpoint: Checkpoint, /) -> None: ...

    def reset(
        self,
        mode: ResetMode,
        checkpoint: Checkpoint,
        acknowledged_routes: Sequence[str],
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

    def discard(self, route_id: str, change_ids: Sequence[str], /) -> None: ...

    def baseline_acknowledged(self, route_id: str, /) -> bool: ...

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


def _now() -> datetime:
    return datetime.now(UTC)


def _count(value: int, singular: str, plural: str | None = None) -> str:
    return f"{value} {singular if value == 1 else plural or singular + 's'}"


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
    """Collect changes durably and prepare mature route batches."""

    def __init__(
        self,
        source: Source,
        state: StateStore,
        routes: Sequence[Route],
        *,
        retention_days: int,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        if type(retention_days) is not int or retention_days < 0:
            raise ValueError("retention_days must be a non-negative integer")
        self._source: Source = source
        self._state: StateStore = state
        self._routes: tuple[Route, ...] = self._validate_routes(routes)
        self._retention_days: int = retention_days
        self._clock: Callable[[], datetime] = clock

    def run(self, *, flush: bool = False) -> MonitorResult:
        """Collect one cycle and return batches ready for delivery."""

        checkpoint = self._state.load()
        result = self._source.poll(None if checkpoint is None else checkpoint.cursor)
        if checkpoint is None:
            return self._create_baseline(result)

        if result.changes:
            _LOGGER.info("Found %s", _count(len(result.changes), "new change"))
        current = _advance_source_texts(checkpoint.source_texts, result)
        observed_at = self._observed_at()
        queued = self._route_deliveries(
            result.changes,
            current,
            checkpoint.source_texts,
        )
        self._state.collect(Checkpoint(result.cursor, current), queued, observed_at)
        deliveries = self._ready_deliveries(observed_at, force=flush)
        self._state.prune(self._retention_days)
        collected = sum(len(items) for items in queued.values())
        if collected:
            _LOGGER.info(
                "Collected %s",
                _count(collected, "route delivery", "route deliveries"),
            )
        else:
            _LOGGER.debug("Collection completed without routed changes")
        return MonitorResult(
            False,
            result.scanned,
            len(current),
            self._baseline_routes(),
            deliveries,
        )

    def preview(self, *, flush: bool = False) -> MonitorResult:
        """Prepare one monitoring cycle without changing state."""

        checkpoint = self._state.load()
        result = self._source.poll(None if checkpoint is None else checkpoint.cursor)
        texts = result.source_texts
        if checkpoint is None:
            if texts is None:
                raise RuntimeError("Initial source poll did not include source texts")
            return MonitorResult(
                True,
                result.scanned,
                len(texts),
                tuple(route.id for route in self._routes if route.notify_baseline),
            )
        current = _advance_source_texts(checkpoint.source_texts, result)
        collected = self._route_deliveries(
            result.changes,
            current,
            checkpoint.source_texts,
        )
        observed_at = self._observed_at()
        deliveries: dict[str, tuple[Delivery, ...]] = {}
        for route in self._routes:
            queued = list(self._state.pending(route.id))
            queued.extend(
                QueuedDelivery(delivery, observed_at)
                for delivery in collected.get(route.id, ())
            )
            if not queued or (
                not flush and not self._ready(queued, route.delivery, observed_at)
            ):
                continue
            selected, _ = self._prepare(
                route,
                tuple(item.delivery for item in queued),
            )
            if selected:
                deliveries[route.id] = selected
        return MonitorResult(
            False,
            result.scanned,
            len(current),
            self._baseline_routes(),
            deliveries,
        )

    def reset(self, mode: ResetMode, *, notify: bool = False) -> MonitorResult:
        """Establish a fresh baseline under the selected reset mode."""

        result = self._source.poll(None)
        texts = result.source_texts
        if texts is None:
            raise RuntimeError("Initial source poll did not include source texts")
        baseline_routes = tuple(
            route.id for route in self._routes if route.notify_baseline
        )
        self._state.reset(
            mode,
            Checkpoint(result.cursor, texts),
            () if notify else baseline_routes,
        )
        self._state.prune(self._retention_days)
        _LOGGER.info(
            "%s reset created a baseline from %s and %s",
            mode.capitalize(),
            _count(result.scanned, "change"),
            _count(len(texts), "source text"),
        )
        return MonitorResult(
            True,
            result.scanned,
            len(texts),
            baseline_routes if notify else (),
        )

    def _create_baseline(self, result: PollResult) -> MonitorResult:
        texts = result.source_texts
        if texts is None:
            raise RuntimeError("Initial source poll did not include source texts")
        self._state.save(Checkpoint(result.cursor, texts))
        self._state.prune(self._retention_days)
        _LOGGER.info(
            "Baseline created from %s and %s",
            _count(result.scanned, "change"),
            _count(len(texts), "source text"),
        )
        return MonitorResult(
            True,
            result.scanned,
            len(texts),
            self._baseline_routes(),
        )

    def _baseline_routes(self) -> tuple[str, ...]:
        return tuple(
            route.id
            for route in self._routes
            if route.notify_baseline and not self._state.baseline_acknowledged(route.id)
        )

    def _route_deliveries(
        self,
        changes: Sequence[Change],
        current: Mapping[str, str],
        previous: Mapping[str, str],
    ) -> dict[str, list[Delivery]]:
        queued: dict[str, list[Delivery]] = {route.id: [] for route in self._routes}
        for change in changes:
            matching = tuple(
                route
                for route in self._routes
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

    def _ready_deliveries(
        self,
        now: datetime,
        *,
        force: bool,
    ) -> dict[str, tuple[Delivery, ...]]:
        ready: dict[str, tuple[Delivery, ...]] = {}
        for route in self._routes:
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
            ready[route.id] = deliveries
            _LOGGER.info(
                "Prepared %s for route %s",
                _count(len(deliveries), "change"),
                route.id,
            )
        return ready

    def _observed_at(self) -> datetime:
        observed_at = self._clock()
        if observed_at.utcoffset() is None:
            raise ValueError("Monitor clock must return a timezone-aware datetime")
        return observed_at

    @staticmethod
    def _validate_routes(routes: Sequence[Route]) -> tuple[Route, ...]:
        snapshot = tuple(routes)
        identifiers: set[str] = set()
        for route in snapshot:
            route_id = route.id
            if type(route_id) is not str or not route_id.strip():
                raise ValueError("Route id must be a non-empty string")
            if route_id in identifiers:
                raise ValueError(f"Route id must be unique: {route_id!r}")
            identifiers.add(route_id)
        return snapshot

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
