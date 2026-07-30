# Noctalia i18n Core

[简体中文](https://github.com/Obelusod/noctalia-i18n-core/blob/main/README.zh-CN.md)

Noctalia i18n Core collects normalized translation changes from [Noctalia Translate](https://i18n.noctalia.dev/projects) and persists source checkpoints, source-text snapshots, pending deliveries, and delivery receipts in SQLite. Applications filter changes through routes, send mature batches through any synchronous or asynchronous transport, and acknowledge successful requests.

## Features

- Collect normalized additions, modifications, and deletions with project-bound opaque cursors.
- Recover every available change since the stored cursor without an arbitrary page limit.
- Share one source poll across independent routes with their own locale, action, and delivery policies.
- Persist source snapshots, route outboxes, delivery receipts, and baseline receipts in SQLite.
- Fold repeated changes into their net result and delay delivery until activity settles or a maximum wait expires.
- Preview the deliveries produced by a normal cycle or forced flush without changing state.
- Validate source and state boundaries while keeping cursor representations private to their adapters.

Applications provide configuration, credentials, scheduling, logging, HTTP session configuration, routes, rendering, and transport.

## Project structure

```text
noctalia_i18n_core/
├── sources/            # Translation source contracts and adapters
│   └── noctalia.py     # Noctalia Translate adapter
├── models.py           # Shared domain values
├── monitor.py          # Collection, routing, folding, and batch policy
└── state.py            # SQLite checkpoints and delivery state
```

## Installation

Install from PyPI with uv:

```bash
uv add noctalia-i18n-core
```

Or with pip:

```bash
pip install noctalia-i18n-core
```

## Usage

The following example monitors Simplified Chinese changes, prints mature batches to the terminal, and acknowledges the corresponding records after successful output:

```python
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from noctalia_i18n_core import (
    Change,
    DeliveryPolicy,
    Monitor,
    NoctaliaSource,
    SQLiteState,
)


@dataclass(frozen=True, slots=True)
class ChineseRoute:
    id: str = "zh-Hans"
    delivery: DeliveryPolicy = DeliveryPolicy(
        quiet_seconds=60,
        max_wait_seconds=600,
        fold_changes=True,
    )
    notify_baseline: bool = False

    def accepts_locale(self, locale: str) -> bool:
        return locale == "zh-Hans"

    def matches(self, change: Change) -> bool:
        return self.accepts_locale(change.locale)


with (
    closing(NoctaliaSource("noctalia", timeout=30)) as source,
    closing(SQLiteState(Path("state.sqlite3"))) as state,
):
    monitor = Monitor(
        source,
        state,
        (ChineseRoute(),),
        retention_days=180,
    )
    result = monitor.run()

    for route_id in result.baseline_routes:
        print(route_id, result.scanned, result.source_texts)
        state.acknowledge_baseline(route_id)

    for route_id, deliveries in result.deliveries.items():
        for delivery in deliveries:
            change = delivery.change
            print(route_id, change.action, change.key, change.new_value)
        state.acknowledge(route_id, deliveries)
```

The first run establishes the current position as a baseline without replaying existing history. The application then schedules the same workflow repeatedly: the monitor persists the new cursor and matching changes before returning batches whose waiting policy has matured. The example handles a complete batch at once. A real transport should acknowledge only the `Delivery` values contained in each successful external request.

`Monitor` does not schedule itself, render messages, or perform transport calls. A synchronous application can handle results directly. An asynchronous application can briefly open the source and state in a worker thread, run the monitor, send its result in the event loop, and reopen the state in a worker to acknowledge success. Do not use the same `NoctaliaSource` or `SQLiteState` instance across threads, and serialize collection and reset cycles that share a state file.

## Sources

`NoctaliaSource(project, timeout, session=None)` reads structured Recent Changes data and the English export from Noctalia Translate. Project identifiers use lowercase letters, digits, and single hyphens, matching identifiers such as `noctalia`, `official-plugins`, and `community-plugins`.

- `poll(None)` returns the newest cursor and a complete English source snapshot without replaying history.
- `poll(cursor)` returns every available newer change and follows reported history pages until it finds the previous event.
- A cursor from another source or project fails explicitly instead of silently creating a new baseline.
- `history(page)` returns one upstream page in its native newest-first order.
- `close()` closes only a session created by the source; a supplied session remains caller-owned.

Source cursors are JSON values but intentionally opaque. Callers must persist and return them unchanged.

Custom sources implement `Source` and return `PollResult`. An initial poll must include the complete source-language mapping. A later poll may omit it when its normalized English changes are sufficient to advance the stored snapshot.

Call the source directly when durable monitoring is not required:

```python
from contextlib import closing

from noctalia_i18n_core import NoctaliaSource

with closing(NoctaliaSource("noctalia", timeout=30)) as source:
    baseline = source.poll(None)
    result = source.poll(baseline.cursor)

for change in result.changes:
    print(change.locale, change.action, change.key)
```

A real application should persist the cursor and return it unchanged in the next run. Subsequent results contain unique changes ordered oldest first.

## Monitoring

`Monitor` combines a `Source`, state store, and sequence of routes:

```python
result = monitor.run()                    # Collect and return mature batches
result = monitor.run(flush=True)          # Also return immature batches
preview = monitor.preview()               # Preview the cycle without writes
preview = monitor.preview(flush=True)     # Preview a forced flush
result = monitor.reset("baseline")        # Replace the baseline; keep delivery state
result = monitor.reset("full")            # Clear delivery state; set a new baseline
```

`run()` atomically advances the source checkpoint and enqueues matching changes before returning mature batches through `MonitorResult.deliveries`. Pending records survive restarts and transport failures. After a successful request, the application calls `acknowledge(route_id, deliveries)` on the state store so delivery receipts prevent the same changes from being enqueued again for that route.

`preview()` combines stored pending records with newly observed changes and applies the same waiting, filtering, and folding policy as `run()` without modifying state. Pass `flush=True` to preview a forced flush.

`MonitorResult.baseline_routes` lists routes whose baseline notices remain unacknowledged. After sending one successfully, the application calls `acknowledge_baseline(route_id)` on the state store; unacknowledged routes reappear in later cycles. `reset()` treats the new baseline as not requiring a notice by default and returns its routes only when `notify=True` is passed. The `baseline` mode preserves outbox and receipt state; `full` clears all monitoring state before establishing the new baseline.

## Routes and delivery

`Route` is a structural contract with the following members:

| Member | Purpose |
| --- | --- |
| `id` | Stable route identifier |
| `delivery` | Route-local `DeliveryPolicy` |
| `notify_baseline` | Whether the route receives a baseline notification |
| `accepts_locale(locale)` | Whether the route subscribes to a locale |
| `matches(change)` | Whether the route accepts a normalized change |

Route IDs must be non-empty and unique within a monitoring cycle. A route ID identifies a durable subscription rather than a transport address. Use a stable value without credentials, not a mutable display name or Webhook URL.

`Monitor` snapshots its routes at construction. After persisted subscriptions change, create a new monitor from the current routes. The next `run()` removes outbox records and baseline receipts for routes no longer present.

`DeliveryPolicy` controls outbox preparation:

| Field | Purpose |
| --- | --- |
| `quiet_seconds` | Required inactivity before automatic delivery |
| `max_wait_seconds` | Maximum age of the oldest pending record |
| `fold_changes` | Whether to fold each locale and key into its net change |

`max_wait_seconds` must not be less than `quiet_seconds`. A value of zero makes the corresponding delivery condition immediate.

Core defines no transport protocol and has no event-loop dependency. An application may split a route batch across multiple external requests and call `acknowledge()` after each success with exactly the `Delivery` values included in that request. If a later request fails, the state store retains every unacknowledged record. A process exit after a successful send but before its acknowledgement may repeat that request on the next run. These at-least-once semantics let Webhooks, bot channels, threads, message queues, and other transports share the same collection and delivery state.

## State

`SQLiteState(path, read_only=False)` persists:

- the opaque source cursor;
- the matching complete English snapshot;
- pending deliveries by route;
- delivery receipts;
- baseline receipts.

State updates use SQLite transactions. An incompatible schema is rejected without modification. Read-only state opens an existing database without writes and uses an in-memory empty state when the file does not exist.

`SQLiteState.summary()` returns initialization status, update time, source-snapshot size, delivery and baseline receipt counts, and pending delivery and route counts. Delivery receipts older than the monitor's configured retention period are pruned after successful cycles.

## API contracts

Supported caller-facing names are exported directly from `noctalia_i18n_core`; submodules organize implementation and are not required for normal imports.

`JsonValue` describes opaque JSON-shaped cursors. JSON validation and normalization remain internal. Invalid constructor arguments raise `ValueError`; source and SQLite failures raise `RuntimeError`. The package does not define a custom exception hierarchy.

`SQLiteState` owns its database connection until `close()` is called. `NoctaliaSource` closes only a session it creates.

## Development

Install the locked development environment:

```bash
uv sync --locked
```

Check formatting, code rules, static types, and all tests:

```bash
uv run ruff format --check .
uv run ruff check .
uv run basedpyright
uv run python -m unittest discover -v
```

Run one test module:

```bash
uv run python -m unittest tests.test_noctalia -v
```

## Build

Build the wheel and source distribution, then validate their package metadata:

```bash
uv build --no-sources --clear
uvx twine check --strict dist/*
```

## License

[MIT](https://github.com/Obelusod/noctalia-i18n-core/blob/main/LICENSE)
