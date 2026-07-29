"""Reusable black-box contracts for source tests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from unittest import TestCase

from noctalia_i18n_core.models import Change
from noctalia_i18n_core.sources import Source


@dataclass(frozen=True, slots=True)
class PollScenario:
    """Expected results for one baseline, changed, and unchanged source trace."""

    source_texts: Mapping[str, str]
    changes: tuple[Change, ...]
    baseline_scanned: int
    changed_scanned: int
    unchanged_scanned: int


def assert_poll_contract(
    case: TestCase,
    source: Source,
    scenario: PollScenario,
) -> None:
    """Verify the public polling sequence every source must support."""

    baseline = source.poll(None)
    case.assertEqual(baseline.changes, ())
    case.assertEqual(baseline.scanned, scenario.baseline_scanned)
    case.assertEqual(baseline.source_texts, dict(scenario.source_texts))

    changed = source.poll(baseline.cursor)
    case.assertEqual(changed.changes, scenario.changes)
    case.assertEqual(changed.scanned, scenario.changed_scanned)
    case.assertIsNone(changed.source_texts)

    unchanged = source.poll(changed.cursor)
    case.assertEqual(unchanged.changes, ())
    case.assertEqual(unchanged.scanned, scenario.unchanged_scanned)
    case.assertIsNone(unchanged.source_texts)
