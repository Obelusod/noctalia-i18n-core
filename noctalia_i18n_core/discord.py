"""Discord routing, rendering, batching, and webhook transport."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

import requests

from .messages import Embed, MergePage, MessageValues, embed_size
from .models import (
    ACTIONS,
    SOURCE_LOCALE,
    Action,
    Change,
    Delivery,
    DeliveryPolicy,
    JsonValue,
    normalize_json,
)

_MAX_EMBEDS = 10
_MAX_MESSAGE_CHARACTERS = 6000
_MAX_ATTEMPTS = 5
_MAX_BACKOFF_SECONDS = 20.0


class ChangeRenderer(Protocol):
    def render(self, action: Action, values: Mapping[str, object], /) -> Embed: ...


class MergeRenderer(Protocol):
    def render(self, values: Sequence[MessageValues], /) -> Sequence[MergePage]: ...


class DiscordSender(Protocol):
    """Send a completed payload to a caller-resolved target reference."""

    def send(self, target_ref: str, payload: Mapping[str, object], /) -> None: ...


def _retry_after(response: requests.Response) -> float:
    try:
        body = normalize_json(response.json(), "Discord rate limit response")
        data = body if isinstance(body, dict) else {}
        raw = data.get("retry_after", 1)
        delay = float(raw) if isinstance(raw, (int, float, str)) else 1
    except (TypeError, ValueError):
        delay = 1
    if not math.isfinite(delay):
        delay = 1
    return min(max(delay, 0.25), _MAX_BACKOFF_SECONDS)


class DiscordWebhookSender:
    """Send payloads to caller-supplied Discord webhook targets."""

    def __init__(
        self,
        session: requests.Session,
        targets: Mapping[str, str],
        timeout: float,
    ) -> None:
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a positive finite number")
        if any(
            type(target_ref) is not str
            or not target_ref.strip()
            or type(url) is not str
            or not url.strip()
            for target_ref, url in targets.items()
        ):
            raise ValueError(
                "targets must map non-empty references to non-empty webhook URLs"
            )
        self._session = session
        self._targets = dict(targets)
        self._timeout = timeout

    def send(self, target_ref: str, payload: Mapping[str, object], /) -> None:
        try:
            url = self._targets[target_ref]
        except KeyError:
            raise ValueError(f"Unknown Discord target: {target_ref!r}") from None
        body = normalize_json(dict(payload), "Discord payload")
        if not isinstance(body, dict):
            raise TypeError("Discord payload must be a JSON object")

        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = self._session.post(
                    url,
                    json=body,
                    timeout=self._timeout,
                )
            except requests.RequestException:
                if attempt + 1 == _MAX_ATTEMPTS:
                    raise RuntimeError(
                        "Cannot reach Discord: network failure"
                    ) from None
                time.sleep(min(2**attempt, _MAX_BACKOFF_SECONDS))
                continue
            if 200 <= response.status_code < 300:
                return
            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable or attempt + 1 == _MAX_ATTEMPTS:
                raise RuntimeError(
                    f"Discord rejected the webhook payload: HTTP {response.status_code}"
                )
            delay = (
                _retry_after(response)
                if response.status_code == 429
                else min(2**attempt, _MAX_BACKOFF_SECONDS)
            )
            time.sleep(delay)


@dataclass(frozen=True, slots=True)
class DiscordRoute:
    """One delivery route plus externally supplied message renderers."""

    id: str
    target_ref: str
    monitor_id: str
    project: str
    locales: frozenset[str]
    actions: frozenset[Action]
    delivery: DeliveryPolicy
    source_renderer: ChangeRenderer | None
    target_renderer: ChangeRenderer | None
    merge_renderer: MergeRenderer | None = None
    baseline_renderer: Callable[[int, int], Embed] | None = None
    username: str = ""
    avatar_url: str = ""

    def __post_init__(self) -> None:
        for name, value in (
            ("id", self.id),
            ("target_ref", self.target_ref),
            ("monitor_id", self.monitor_id),
            ("project", self.project),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"Route {name} must be a non-empty string")
        if type(self.username) is not str or type(self.avatar_url) is not str:
            raise ValueError("Route username and avatar_url must be strings")
        if not self.actions or not self.actions <= frozenset(ACTIONS):
            raise ValueError("Route actions must contain supported actions")
        if not self.locales or any(
            type(locale) is not str or not locale for locale in self.locales
        ):
            raise ValueError("Route locales must contain one or more non-empty strings")
        if "*" in self.locales and len(self.locales) != 1:
            raise ValueError("Route wildcard locale must be used alone")
        accepts_source = self.accepts_locale(SOURCE_LOCALE)
        accepts_target = "*" in self.locales or any(
            locale != SOURCE_LOCALE for locale in self.locales
        )
        if accepts_source and self.source_renderer is None:
            raise ValueError("Route accepting source changes requires source_renderer")
        if accepts_target and self.target_renderer is None:
            raise ValueError("Route accepting target changes requires target_renderer")
        if (self.delivery.merge_threshold is None) != (self.merge_renderer is None):
            raise ValueError(
                "Route merge policy and merge_renderer must be set together"
            )

    @property
    def notify_baseline(self) -> bool:
        return self.baseline_renderer is not None

    def accepts_locale(self, locale: str) -> bool:
        return "*" in self.locales or locale in self.locales

    def matches(self, change: Change) -> bool:
        return self.accepts_locale(change.locale) and change.action in self.actions

    def render(self, delivery: Delivery) -> Embed:
        change = delivery.change
        renderer = self.source_renderer if change.is_source else self.target_renderer
        if renderer is None:
            raise RuntimeError(f"Route {self.id!r} cannot render {change.locale!r}")
        return renderer.render(change.action, self._values(delivery))

    def render_merge(self, deliveries: Sequence[Delivery]) -> Sequence[MergePage]:
        if not deliveries:
            raise ValueError("A merge requires at least one delivery")
        locale = deliveries[0].change.locale
        if any(item.change.locale != locale for item in deliveries):
            raise ValueError("A merge cannot mix locales")
        if self.merge_renderer is None:
            raise RuntimeError(f"Route {self.id!r} has no merge renderer")
        return self.merge_renderer.render(
            tuple(self._values(item) for item in deliveries)
        )

    def _values(self, delivery: Delivery) -> MessageValues:
        change = delivery.change
        return MessageValues(
            monitor_id=self.monitor_id,
            project=self.project,
            key=change.key,
            source=delivery.source_text,
            old_value=change.old_value,
            new_value=change.new_value,
            locale=change.locale,
            actor=change.actor,
            actor_url=change.actor_url,
            actor_avatar_url=change.actor_avatar_url,
            action=change.action,
            change_url=change.url,
            timestamp=change.iso_timestamp,
            unix_time=int(change.occurred_at.timestamp()),
        )


@dataclass(frozen=True, slots=True)
class _Rendered:
    deliveries: tuple[Delivery, ...]
    embed: Embed


def _embed_data(embed: Embed) -> dict[str, JsonValue]:
    data = normalize_json(embed, "Discord embed")
    if not isinstance(data, dict):
        raise TypeError("Discord embed must be a JSON object")
    return data


def _payload(route: DiscordRoute, embeds: list[Embed]) -> dict[str, JsonValue]:
    normalized_embeds: list[JsonValue] = [_embed_data(embed) for embed in embeds]
    payload: dict[str, JsonValue] = {
        "allowed_mentions": {"parse": []},
        "embeds": normalized_embeds,
    }
    if route.username:
        payload["username"] = route.username
    if route.avatar_url:
        payload["avatar_url"] = route.avatar_url
    return payload


def _batches(items: Iterable[_Rendered]) -> Iterator[tuple[_Rendered, ...]]:
    batch: list[_Rendered] = []
    characters = 0
    for item in items:
        size = embed_size(item.embed)
        if size > _MAX_MESSAGE_CHARACTERS:
            raise ValueError("Discord embed exceeds the 6000-character limit")
        if batch and (
            len(batch) >= _MAX_EMBEDS or characters + size > _MAX_MESSAGE_CHARACTERS
        ):
            yield tuple(batch)
            batch = []
            characters = 0
        batch.append(item)
        characters += size
    if batch:
        yield tuple(batch)


class DiscordNotifier:
    """Render route messages and pass payloads to the supplied sender."""

    def __init__(self, routes: Sequence[DiscordRoute], sender: DiscordSender) -> None:
        identifiers = [route.id for route in routes]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Route IDs must be unique")
        self._route_list = tuple(routes)
        self._routes = {route.id: route for route in routes}
        self._sender = sender

    @property
    def routes(self) -> tuple[DiscordRoute, ...]:
        return self._route_list

    def render(
        self,
        route_id: str,
        deliveries: Sequence[Delivery],
    ) -> tuple[dict[str, JsonValue], ...]:
        return tuple(
            _embed_data(item.embed)
            for item in self._render(self._route(route_id), deliveries)
        )

    def send(
        self,
        route_id: str,
        deliveries: Sequence[Delivery],
        acknowledge: Callable[[Sequence[Delivery]], None],
    ) -> None:
        route = self._route(route_id)
        for batch in _batches(self._render(route, deliveries)):
            self._sender.send(
                route.target_ref,
                _payload(route, [item.embed for item in batch]),
            )
            acknowledge(
                tuple(item for rendered in batch for item in rendered.deliveries)
            )

    def send_baseline(self, route_id: str, changes: int, source_texts: int) -> None:
        route = self._route(route_id)
        if route.baseline_renderer is None:
            raise RuntimeError(f"Route {route.id!r} has no baseline renderer")
        self._sender.send(
            route.target_ref,
            _payload(
                route,
                [route.baseline_renderer(changes, source_texts)],
            ),
        )

    def _route(self, route_id: str) -> DiscordRoute:
        try:
            return self._routes[route_id]
        except KeyError:
            raise ValueError(f"Unknown route: {route_id!r}") from None

    def _render(
        self,
        route: DiscordRoute,
        deliveries: Sequence[Delivery],
    ) -> Iterator[_Rendered]:
        merge_threshold = route.delivery.merge_threshold
        if merge_threshold is None:
            for delivery in deliveries:
                yield _Rendered((delivery,), route.render(delivery))
            return

        groups: dict[str, list[Delivery]] = {}
        for delivery in deliveries:
            groups.setdefault(delivery.change.locale, []).append(delivery)
        merged: set[str] = set()
        for delivery in deliveries:
            group = groups[delivery.change.locale]
            if len(group) <= merge_threshold:
                yield _Rendered((delivery,), route.render(delivery))
                continue
            if delivery.change.locale in merged:
                continue
            merged.add(delivery.change.locale)
            offset = 0
            for page in route.render_merge(group):
                end = offset + page.count
                yield _Rendered(tuple(group[offset:end]), page.embed)
                offset = end
            if offset != len(group):
                raise RuntimeError("Merge renderer did not consume every delivery")
