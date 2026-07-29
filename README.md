# Noctalia i18n Core

[简体中文](https://github.com/Obelusod/noctalia-i18n-core/blob/main/README.zh-CN.md)

Noctalia i18n Core is an unofficial typed Python library for collecting translation changes from [Noctalia Translate](https://i18n.noctalia.dev/projects), persisting delivery state, rendering caller-owned Discord messages, and delivering them reliably. It requires Python 3.12 or newer.

## Features

- Collect normalized additions, modifications, and deletions with project-bound opaque cursors.
- Recover every available change since the stored cursor without an arbitrary page limit.
- Share one source poll across independent routes with their own locales, actions, messages, and delivery policies.
- Persist source snapshots, route outboxes, delivery receipts, and baseline-notification state in SQLite.
- Fold repeated changes into their net result and merge large locale batches at caller-selected thresholds.
- Wait for activity to settle while enforcing a maximum delivery delay, or flush pending work explicitly.
- Preview rendered messages without changing state or contacting Discord.
- Validate external YAML templates, Discord Embed limits, and JSON-shaped source and state boundaries.
- Retry Discord rate limits and transient network or server failures without exposing webhook URLs in errors.

Applications provide configuration, credentials, scheduling, logging, HTTP session configuration, and message files.

## Project structure

```text
noctalia_i18n_core/
├── sources/            # Translation change source adapters
│   └── noctalia.py     # Noctalia Translate source adapter
├── diff.py             # Multilingual ANSI diff rendering
├── discord.py          # Discord routing, rendering, and delivery
├── messages.py         # YAML message loading and rendering
├── models.py           # Shared monitoring domain values
├── monitor.py          # Monitoring workflow and delivery policy
└── state.py            # SQLite checkpoints and delivery state
```

## Installation

Install the package from PyPI:

```bash
pip install noctalia-i18n-core
```

With uv:

```bash
uv add noctalia-i18n-core
```

## Quick start

The package does not include message files. This example loads caller-owned templates and runs one Discord route:

```python
from contextlib import closing
from pathlib import Path

import requests

from noctalia_i18n_core import (
    DeliveryPolicy,
    DiscordNotifier,
    DiscordRoute,
    DiscordWebhookSender,
    Monitor,
    NoctaliaSource,
    SQLiteState,
    load_merge,
    load_message,
)

message_root = Path("/etc/my-app/messages")
source_message = load_message("english", message_root / "source")
target_message = load_message("english", message_root / "target")
merge_message = load_merge("english", message_root / "merge")

route = DiscordRoute(
    id="default",
    target_ref="default",
    monitor_id="noctalia",
    project="noctalia",
    locales=frozenset({"en", "zh-Hans"}),
    actions=frozenset({"added", "modified", "deleted"}),
    delivery=DeliveryPolicy(
        quiet_seconds=240,
        max_wait_seconds=900,
        fold_changes=True,
        merge_threshold=5,
    ),
    source_renderer=source_message,
    target_renderer=target_message,
    merge_renderer=merge_message,
)

with (
    requests.Session() as session,
    closing(SQLiteState(Path("state.sqlite3"))) as state,
):
    source = NoctaliaSource("noctalia", timeout=30, session=session)
    sender = DiscordWebhookSender(
        session,
        {"default": "https://discord.com/api/webhooks/..."},
        timeout=30,
    )
    monitor = Monitor(
        source,
        state,
        DiscordNotifier((route,), sender),
        retention_days=180,
    )
    monitor.run()
```

The first run establishes a baseline without replaying existing history. Later runs atomically advance the cursor and enqueue matching changes before delivery. Each successful Discord request acknowledges only the records it contains, so a later failure leaves the remaining outbox intact.

## Monitoring lifecycle

```python
monitor.run()                 # Collect and deliver mature batches
monitor.run(flush=True)       # Also deliver pending immature batches
preview = monitor.preview()   # Collect and render without writes or sends
monitor.reset("baseline")     # Replace the source baseline; preserve delivery state
monitor.reset("full")         # Clear all monitoring state; establish a new baseline
```

`run()` persists newly observed changes before attempting delivery. Pending records therefore survive restarts and transport failures. Delivery receipts prevent a successful request from being repeated.

`preview()` reads the selected source but does not write SQLite state, invoke the sender, or establish a missing baseline. `reset()` suppresses a new baseline notification unless `notify=True` is passed.

`SQLiteState.summary()` returns initialization status, update time, source-snapshot size, receipt and baseline-notification counts, and pending delivery and route counts.

## Sources

`NoctaliaSource(project, timeout, session=None)` reads structured Recent Changes data and the English export from Noctalia Translate. Project identifiers use lowercase letters, digits, and single hyphens, matching identifiers such as `noctalia`, `official-plugins`, and `community-plugins`.

- `poll(None)` returns the newest cursor and a complete English source snapshot without replaying history.
- `poll(cursor)` returns unique changes ordered oldest first and follows all reported history pages until it finds the previous event.
- A cursor from another source or project fails explicitly instead of silently creating a new baseline.
- `history(page)` returns one upstream page in its native newest-first order.
- `close()` closes only a session created by the source; a supplied session remains caller-owned.

Source cursors are JSON values but intentionally opaque. Callers must persist and return them unchanged.

Custom sources implement `Source` and return `PollResult`, both imported from `noctalia_i18n_core`. An initial poll must include the complete source-language mapping. A later poll may omit it when its normalized English changes are sufficient to advance the stored snapshot.

## Routes and delivery

`DiscordRoute` binds a change subscription, renderers, delivery policy, and opaque `target_ref`. `DiscordWebhookSender` resolves the reference through a caller-supplied target mapping, keeping credentials out of persisted state and previews.

| Field | Purpose |
| --- | --- |
| `id` | Stable route identifier, unique within one notifier |
| `target_ref` | Opaque key resolved by the sender |
| `monitor_id` | Caller-defined monitor identifier exposed to templates |
| `project` | Caller-defined project identifier exposed to templates |
| `locales` | Exact locale set, or `frozenset({"*"})` for every locale |
| `actions` | Non-empty subset of `added`, `modified`, and `deleted` |
| `delivery` | Route-local accumulation and merge policy |
| `source_renderer` | Renderer for English source changes |
| `target_renderer` | Renderer for target-locale changes |
| `merge_renderer` | Optional renderer for merged locale batches |
| `baseline_renderer` | Optional renderer enabling baseline notifications |
| `username` | Optional per-send Discord username override |
| `avatar_url` | Optional per-send Discord avatar override |

The locale wildcard must be used alone. A route accepting English requires `source_renderer`; a route accepting any target locale requires `target_renderer`. `merge_renderer` is required exactly when merging is enabled. When supplied, `baseline_renderer` receives the recent-change count and source-text count.

`DeliveryPolicy` controls when and how each route processes its outbox:

| Field | Purpose |
| --- | --- |
| `quiet_seconds` | Required inactivity before automatic delivery |
| `max_wait_seconds` | Maximum age of the oldest pending record |
| `fold_changes` | Whether to fold each locale and key into its net change |
| `merge_threshold` | Merge a locale batch when its size exceeds this value; `None` disables merging |

A `merge_threshold` of `0` merges every non-empty batch. For a custom transport, implement `DiscordSender`; its `send(target_ref, payload)` method receives a complete JSON-compatible Discord payload.

## Message templates

The package never bundles or selects message files. `load_message(name, directory)` and `load_merge(name, directory)` resolve `<directory>/<name>.yaml`. They reject unsafe names, duplicate YAML keys, missing or unknown fields, invalid placeholders, and content exceeding Discord Embed limits.

A detailed message file requires `added`, `modified`, and `deleted` embeds and may define `diff`:

````yaml
diff:
  old: {color: red, bold: true, underline: false}
  new: {color: green, bold: true, underline: false}
added:
  title: "[{locale}] Added"
  description: |-
    {key_link}
    `{new_value:truncate=1000}`
modified:
  title: "[{locale}] Modified"
  url: "{change_url}"
  description: |-
    ```ansi
    − {old_diff}
    + {new_diff}
    ```
deleted:
  title: "[{locale}] Deleted"
  description: "{key_link}"
````

A merge file requires `source`, `target`, and `entries`. `entries` requires `separator`, `added`, `modified`, and `deleted`; an optional `diff` uses the same schema as a detailed message. Oversized merges split only at entry boundaries.

Embed templates support `title`, `description`, `url`, `timestamp`, `color`, `footer`, `image`, `thumbnail`, `author`, and `fields`. A detailed embed may set `url: "{change_url}"` to link its title to the individual change. Merged embeds do not expose `change_url` because one batch may contain multiple changes.

Detailed embeds and merge entries support these placeholders:

| Placeholder | Value |
| --- | --- |
| `{monitor_id}` | Caller-defined monitor identifier |
| `{project}` | Caller-defined project identifier |
| `{key}` | Translation key |
| `{key_link}` | Key linked to the change URL, or code-styled when the URL is unavailable |
| `{source}` | Current English source text |
| `{old_value}` | Value before a modification or deletion |
| `{new_value}` | Value after an addition or modification |
| `{old_diff}` | Previous value with changed tokens using the configured ANSI style |
| `{new_diff}` | New value with changed tokens using the configured ANSI style |
| `{locale}` | Normalized locale identifier |
| `{actor}` | Editor name |
| `{actor_url}` | Editor profile URL, when available |
| `{actor_avatar_url}` | Editor avatar URL, when available |
| `{action}` | `added`, `modified`, or `deleted` |
| `{change_url}` | Translation editor URL, when available |
| `{timestamp}` | UTC ISO 8601 change time |
| `{unix_time}` | Unix seconds for Discord timestamp markup |

Merged embeds support these batch placeholders:

| Placeholder | Value |
| --- | --- |
| `{monitor_id}` | Caller-defined monitor identifier |
| `{project}` | Caller-defined project identifier |
| `{locale}` | Locale shared by the batch |
| `{count}` | Number of changes in the current message |
| `{actors}` | Unique editor names |
| `{actor_count}` | Number of unique editors |
| `{actor_avatar_url}` | Editor avatar URL when the batch has one editor and an avatar is available |
| `{added_count}` | Number of additions |
| `{modified_count}` | Number of modifications |
| `{deleted_count}` | Number of deletions |
| `{first_timestamp}` | UTC ISO 8601 time of the first change |
| `{last_timestamp}` | UTC ISO 8601 time of the last change |
| `{first_unix_time}` | Unix seconds for the first change |
| `{last_unix_time}` | Unix seconds for the last change |
| `{entries}` | Rendered entries joined by `entries.separator` |

Use `truncate=N` to limit `{key}`, `{source}`, `{old_value}`, `{new_value}`, and `{actor}`. Merged embeds also allow it on `{actors}` and `{entries}`. `N` includes the ellipsis, and wide characters count as two columns:

```yaml
value: "{source:truncate=1024}"
```

Use `fallback=...` to replace an unavailable string value:

```yaml
icon_url: "{actor_avatar_url:fallback=https://github.com/noctalia-dev.png}"
```

`diff` is required only when `{old_diff}` or `{new_diff}` is used. Supported ANSI colors are `gray`, `red`, `green`, `yellow`, `blue`, `magenta`, `cyan`, `white`, and `null`; `bold` and `underline` control emphasis independently.

## API contracts

Supported caller-facing names are exported directly from `noctalia_i18n_core`; submodules organize implementation and are not required for normal imports. `JsonValue` describes opaque JSON-shaped cursors and preview data. JSON validation and normalization remain internal.

Invalid constructor arguments and templates raise `ValueError`. Source, SQLite, rendering, and transport failures raise `RuntimeError`. The package does not define a custom exception hierarchy.

`SQLiteState` owns its database connection until `close()` is called. `NoctaliaSource` closes only a session it creates; `DiscordWebhookSender` never closes or reconfigures its supplied session.

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
