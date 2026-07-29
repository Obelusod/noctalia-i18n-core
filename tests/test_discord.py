"""Discord integration tests."""

from __future__ import annotations

import unittest
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TypeGuard
from unittest.mock import patch

import requests

from noctalia_i18n_core.discord import (
    DiscordNotifier,
    DiscordRoute,
    DiscordWebhookSender,
)
from noctalia_i18n_core.messages import Embed, MergePage, MessageValues
from noctalia_i18n_core.models import (
    ACTIONS,
    Action,
    Delivery,
    DeliveryPolicy,
)

from .fixtures import delivery


class _Message:
    def render(self, action: Action, values: Mapping[str, object], /) -> Embed:
        return {"description": f"{values['key']}:{action}"}


class _Merge:
    def __init__(self, count: int | None = None) -> None:
        self.count = count

    def render(self, values: Sequence[MessageValues], /) -> tuple[MergePage, ...]:
        count = len(values) if self.count is None else self.count
        return (MergePage(count, {"description": f"merged:{len(values)}"}),)


class _Sender:
    def __init__(self) -> None:
        self.payloads: list[Mapping[str, object]] = []

    def send(self, _target_ref: str, payload: Mapping[str, object], /) -> None:
        self.payloads.append(payload)


def _route(
    *, merge_threshold: int | None = None, merge_count: int | None = None
) -> DiscordRoute:
    return DiscordRoute(
        id="main",
        target_ref="target",
        monitor_id="monitor",
        project="project",
        locales=frozenset({"*"}),
        actions=frozenset(ACTIONS),
        delivery=DeliveryPolicy(
            quiet_seconds=0,
            max_wait_seconds=0,
            fold_changes=True,
            merge_threshold=merge_threshold,
        ),
        source_renderer=_Message(),
        target_renderer=_Message(),
        merge_renderer=(None if merge_threshold is None else _Merge(merge_count)),
    )


def _is_array(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _embed_count(payload: Mapping[str, object]) -> int:
    embeds = payload["embeds"]
    if not _is_array(embeds):
        raise AssertionError("Discord payload embeds must be a list")
    return len(embeds)


def _response(status: int) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    return response


class DiscordTests(unittest.TestCase):
    def test_payloads_are_split_at_discords_embed_limit(self) -> None:
        sender = _Sender()
        notifier = DiscordNotifier((_route(),), sender)
        deliveries = tuple(
            delivery(id=f"change-{index}", key=f"key.{index}") for index in range(11)
        )
        acknowledged: list[tuple[str, ...]] = []

        notifier.send(
            "main",
            deliveries,
            lambda sent: acknowledged.append(tuple(item.change.id for item in sent)),
        )

        self.assertEqual(
            [_embed_count(payload) for payload in sender.payloads], [10, 1]
        )
        self.assertEqual([len(batch) for batch in acknowledged], [10, 1])

    def test_locale_batches_merge_only_above_the_configured_count(self) -> None:
        sender = _Sender()
        notifier = DiscordNotifier((_route(merge_threshold=1),), sender)
        deliveries = (
            delivery(id="first", key="first"),
            delivery(id="second", key="second"),
        )

        notifier.send("main", deliveries, lambda _sent: None)

        embeds = sender.payloads[0]["embeds"]
        self.assertEqual(embeds, [{"description": "merged:2"}])

    def test_merge_renderer_must_account_for_every_delivery(self) -> None:
        notifier = DiscordNotifier(
            (_route(merge_threshold=1, merge_count=1),),
            _Sender(),
        )
        deliveries: tuple[Delivery, ...] = (
            delivery(id="first", key="first"),
            delivery(id="second", key="second"),
        )

        with self.assertRaisesRegex(RuntimeError, "every delivery"):
            notifier.send("main", deliveries, lambda _sent: None)

    def test_route_rejects_an_empty_locale_selection(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-empty"):
            replace(_route(), locales=frozenset())

    def test_route_rejects_redundant_wildcard_locales(self) -> None:
        with self.assertRaisesRegex(ValueError, "wildcard"):
            replace(_route(), locales=frozenset({"*", "de"}))

    def test_webhook_sender_rejects_invalid_timeouts(self) -> None:
        session = requests.Session()
        self.addCleanup(session.close)
        for timeout in (0, float("nan"), True):
            with (
                self.subTest(timeout=timeout),
                self.assertRaisesRegex(ValueError, "timeout"),
            ):
                DiscordWebhookSender(session, {}, timeout)

    def test_webhook_sender_rejects_invalid_targets(self) -> None:
        session = requests.Session()
        self.addCleanup(session.close)
        for targets in ({"": "https://example.com"}, {"route": ""}):
            with (
                self.subTest(targets=targets),
                self.assertRaisesRegex(ValueError, "targets"),
            ):
                DiscordWebhookSender(session, targets, 30)

    def test_webhook_sender_rejects_non_json_payloads(self) -> None:
        session = requests.Session()
        self.addCleanup(session.close)
        sender = DiscordWebhookSender(
            session,
            {"route": "https://discord.com/api/webhooks/id/token"},
            30,
        )
        with (
            patch.object(session, "post") as post,
            self.assertRaisesRegex(ValueError, "non-finite"),
        ):
            sender.send("route", {"value": float("nan")})

        post.assert_not_called()

    def test_webhook_sender_resolves_and_sends_a_target(self) -> None:
        session = requests.Session()
        self.addCleanup(session.close)
        sender = DiscordWebhookSender(
            session,
            {"route": "https://discord.com/api/webhooks/id/token"},
            30,
        )
        with patch.object(session, "post", return_value=_response(204)) as post:
            sender.send("route", {"content": "hello"})

        post.assert_called_once_with(
            "https://discord.com/api/webhooks/id/token",
            json={"content": "hello"},
            timeout=30,
        )

    def test_webhook_sender_retries_rate_limits(self) -> None:
        session = requests.Session()
        self.addCleanup(session.close)
        sender = DiscordWebhookSender(
            session,
            {"route": "https://discord.com/api/webhooks/id/token"},
            30,
        )
        rate_limited = _response(429)
        with (
            patch.object(rate_limited, "json", return_value={"retry_after": 0.25}),
            patch.object(
                session,
                "post",
                side_effect=(rate_limited, _response(204)),
            ),
            patch("noctalia_i18n_core.discord.time.sleep") as sleep,
        ):
            sender.send("route", {"content": "hello"})

        sleep.assert_called_once_with(0.25)

    def test_webhook_sender_retries_network_failures(self) -> None:
        session = requests.Session()
        self.addCleanup(session.close)
        sender = DiscordWebhookSender(
            session,
            {"route": "https://discord.com/api/webhooks/id/token"},
            30,
        )
        with (
            patch.object(
                session,
                "post",
                side_effect=(requests.ConnectionError(), _response(204)),
            ),
            patch("noctalia_i18n_core.discord.time.sleep") as sleep,
        ):
            sender.send("route", {"content": "hello"})

        sleep.assert_called_once_with(1)

    def test_webhook_sender_hides_targets_from_http_errors(self) -> None:
        session = requests.Session()
        self.addCleanup(session.close)
        sender = DiscordWebhookSender(
            session,
            {"route": "https://discord.com/api/webhooks/id/token"},
            30,
        )
        with (
            patch.object(session, "post", return_value=_response(404)),
            self.assertRaisesRegex(RuntimeError, "HTTP 404") as raised,
        ):
            sender.send("route", {"content": "hello"})

        self.assertNotIn("token", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
