"""End-to-end source, state, rendering, and sender tests."""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path

from noctalia_i18n_core.discord import DiscordNotifier, DiscordRoute
from noctalia_i18n_core.messages import Embed, MergePage, MessageValues
from noctalia_i18n_core.models import (
    ACTIONS,
    Action,
    Checkpoint,
    DeliveryPolicy,
    JsonValue,
    PollResult,
)
from noctalia_i18n_core.monitor import Monitor
from noctalia_i18n_core.state import SQLiteState

from .fixtures import RECORDED_SOURCE, RUN_AT, recorded_change


class _Source:
    def __init__(self, result: PollResult) -> None:
        self.result = result

    def poll(self, _cursor: JsonValue | None, /) -> PollResult:
        return self.result


class _Message:
    def render(self, action: Action, values: Mapping[str, object], /) -> Embed:
        return {"description": f"{values['key']}:{action}"}


class _Merge:
    def render(self, values: Sequence[MessageValues], /) -> tuple[MergePage, ...]:
        return (MergePage(len(values), {"description": str(len(values))}),)


class _Sender:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.payloads: list[tuple[str, Mapping[str, object]]] = []

    def send(self, target_ref: str, payload: Mapping[str, object], /) -> None:
        self.payloads.append((target_ref, payload))
        if self.fail:
            raise RuntimeError("fixture delivery failed")


def _route() -> DiscordRoute:
    return DiscordRoute(
        id="main",
        target_ref="webhook-main",
        monitor_id="noctalia",
        project="noctalia",
        locales=frozenset({"*"}),
        actions=frozenset(ACTIONS),
        delivery=DeliveryPolicy(
            quiet_seconds=0,
            max_wait_seconds=0,
            fold_changes=True,
            merge_threshold=5,
        ),
        source_renderer=_Message(),
        target_renderer=_Message(),
        merge_renderer=_Merge(),
        baseline_renderer=lambda changes, source_texts: {
            "description": f"{changes}:{source_texts}"
        },
    )


class PipelineTests(unittest.TestCase):
    def test_success_acknowledges_and_failure_retains_the_outbox(self) -> None:
        change = recorded_change()
        result = PollResult(
            (change,),
            "next",
            1,
            {change.key: RECORDED_SOURCE},
        )
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                sender = _Sender(fail=fail)
                route = _route()
                with closing(SQLiteState(Path(directory) / "state.db")) as state:
                    state.save(Checkpoint("previous", {change.key: RECORDED_SOURCE}))
                    state.record_baseline(route.id)
                    monitor = Monitor(
                        _Source(result),
                        state,
                        DiscordNotifier((route,), sender),
                        retention_days=30,
                        clock=lambda: RUN_AT,
                    )
                    if fail:
                        with self.assertRaises(RuntimeError):
                            monitor.run(flush=True)
                    else:
                        monitor.run(flush=True)
                    pending = state.pending(route.id)

                self.assertEqual(bool(pending), fail)
                self.assertEqual(sender.payloads[0][0], "webhook-main")


if __name__ == "__main__":
    unittest.main()
