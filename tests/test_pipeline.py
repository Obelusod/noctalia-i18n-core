"""End-to-end source, monitor, and SQLite state tests."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from noctalia_i18n_core import (
    Change,
    Checkpoint,
    DeliveryPolicy,
    JsonValue,
    Monitor,
    PollResult,
    SQLiteState,
)

from .fixtures import RECORDED_SOURCE, RUN_AT, recorded_change


class _Source:
    def __init__(self, result: PollResult) -> None:
        self.result = result

    def poll(self, _cursor: JsonValue | None, /) -> PollResult:
        return self.result


@dataclass(frozen=True, slots=True)
class _Route:
    id: str = "main"
    delivery: DeliveryPolicy = DeliveryPolicy(0, 0, True)
    notify_baseline: bool = False

    def accepts_locale(self, _locale: str, /) -> bool:
        return True

    def matches(self, _change: Change, /) -> bool:
        return True


class PipelineTests(unittest.TestCase):
    def test_external_delivery_acknowledges_only_completed_requests(self) -> None:
        change = recorded_change()
        result = PollResult(
            (change,),
            "next",
            1,
            {change.key: RECORDED_SOURCE},
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with closing(SQLiteState(path)) as state:
                state.save(Checkpoint("previous", {change.key: RECORDED_SOURCE}))
                monitor = Monitor(
                    _Source(result),
                    state,
                    (_Route(),),
                    retention_days=30,
                    clock=lambda: RUN_AT,
                )
                batch = monitor.run(flush=True).deliveries["main"]

            with closing(SQLiteState(path)) as state:
                self.assertEqual(
                    [item.delivery.change.id for item in state.pending("main")],
                    [change.id],
                )
                state.acknowledge("main", batch)

            with closing(SQLiteState(path)) as state:
                self.assertEqual(state.pending("main"), ())


if __name__ == "__main__":
    unittest.main()
