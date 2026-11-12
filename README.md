# Noctalia i18n Core

[简体中文](README.zh-CN.md)

An unofficial, independently maintained Python library for querying translation projects on [Noctalia Translate](https://i18n.noctalia.dev/). Read project progress, translations, review items, and revision history through a website client or HTTP data service.

## Installation

```bash
uv add noctalia-i18n-core
```

```bash
pip install noctalia-i18n-core
```

Requires Python 3.12 or later.

## Clients

`WebClient` reads the website, and `DataClient` reads a data service. Both share the parameters and return contracts of `project()`, `keys()`, `catalog()`, `translations()`, `search()`, `missing()`, `reviews()`, `changes()`, and `translation_url()`.

The project identifier is the first argument, for example `WebClient("noctalia")` or `DataClient("noctalia", url="https://i18n.obelus.cc")`. Both accept `timeout=30`, `session=None`, and `proxy=None`.

The website client additionally provides `iter_changes()`, `change_pages()`, and `revision_pages()`. The data client provides `history()`, `revision()`, `contributors()`, `status()`, and `updates()`. Its query range follows the service's collection progress.

## Website queries

```python
from noctalia_i18n_core import WebClient

with WebClient("noctalia", timeout=30) as client:
    project = client.project()
    translations = client.translations(
        "bar.annotate.capture-new-region", locales=("en", "zh-Hans")
    )
    print(project.name, project.progress)
    print(translations)
```

`WebClient` selects a website project by its URL identifier, such as `noctalia` or `official-plugins`. Query results use typed, immutable values. Translation mappings use `{key: {locale: value}}` form and preserve empty strings.

| Method | Purpose |
| --- | --- |
| `project()` | Project metadata and per-language progress |
| `keys(prefix=None)` | Keys, optionally restricted to a dot-separated hierarchy |
| `catalog(locale=None)` | Current translation export, optionally restricted to one language |
| `translations(key, *keys, locales=None)` | Current translations for exact keys |
| `search(query, search_locale=..., locales=None)` | Search key names and text in a selected language |
| `missing(locale, query=None)` | Untranslated entries and available English source text |
| `reviews(locale, query=None)` | Translations marked as needing review |
| `changes(key, *keys, locale=...)` | Complete website history for selected keys and language |
| `iter_changes(...)` | Lazy project-wide changes with language, time, and revision filters |
| `change_pages(...)` | Normalized pages with raw continuation identifiers |
| `revision_pages(...)` | Raw website pages, preserving empty and null values |

### History filters and pagination

```python
from datetime import UTC, datetime

with WebClient("noctalia", timeout=30) as client:
    for change in client.iter_changes(
        locale="zh-Hans",
        since=datetime(2026, 1, 1, tzinfo=UTC),
    ):
        print(change.key, change.action, change.new_value)
```

`since` and `until` are inclusive, timezone-aware bounds. `after` excludes the selected raw revision and all older records. `change_pages()` and `revision_pages()` accept `start_page` and `cursor` for resumable scans. A cursor follows website identifiers as pages shift; an unavailable cursor raises `RuntimeError`. Source language is English (`en`), and other locale identifiers are matched exactly.

### HTTP sessions, timeouts, and proxies

`timeout=30` sets the connection and read timeout for each attempt. Internally created sessions retry transient transport failures, rate limits, and server errors three times with backoff. `proxy` selects a proxy, `proxy=""` connects directly, and an omitted proxy retains environment settings. Supply `session` to own authentication, proxies, or retry behavior. Caller-owned sessions are closed by the caller.

```python
import requests

with requests.Session() as session:
    session.trust_env = False
    with WebClient("noctalia", timeout=30, session=session) as client:
        print(client.project().name)
```

## Data service queries

```python
from noctalia_i18n_core import DataClient

with DataClient("noctalia", url="https://i18n.obelus.cc") as client:
    print(client.status())
    changes = client.history(locale="zh-Hans", action="modified", limit=25)
    print(client.translations("bar.annotate.capture-new-region"))
```

`DataClient` queries collected data over HTTP(S), using the same interface for localhost and remote deployments. Select the service address and project separately, for example `http://127.0.0.1:8088` and `noctalia`. Noctalia i18n Server owns collection, database storage, and HTTP serving.

Available methods include `project()`, `keys()`, `catalog(locale=None)`, `translations()`, `search()`, `missing()`, `reviews()`, `revision()`, `changes()`, and `contributors()`. `history()` filters by language, time, actor, action, and exact keys, with at most 1000 entries per page. Use `before=previous_page[-1].id` for stable pagination while new records arrive.

### Incremental reads

```python
with DataClient("noctalia", url="http://127.0.0.1:8088") as client:
    cursor = client.updates().cursor
    batch = client.updates(cursor)
    for change in batch.changes:
        print(change.key, change.new_value)
    cursor = batch.cursor
```

`updates()` establishes the current processing position. Passing a saved cursor returns oldest-first changes and the matching raw-record position. Continue while `has_more` is true, and save the returned cursor after processing succeeds. Records without text changes also advance the cursor. Historical backfill precedes existing positions. The caller owns cursor persistence and application operations.

The client retains the last response per endpoint and revalidates it through ETags. Each request reads a consistent committed view; data can change between requests. Use `session` to configure connection policy. Supplied sessions remain owned by the caller.

## Returned values

| Type | Contract |
| --- | --- |
| `Project`, `Locale` | Project metadata and integer completion percentages matching the website |
| `Missing`, `Review` | Entry text, language, and website editor URL |
| `Revision` | Raw identifier, actor, old/new values, and UTC timestamp |
| `Change` | Added, modified, or deleted text with optional English source context |
| `RevisionPage`, `ChangePage` | Page number, total pages, ordered records, and continuation identifier |
| `ChangeBatch` | Consistent change batch, processing cursor, and continuation flag |
| `CollectionStatus` | Published history range, catalog size, collection times, and pending work |

Raw `Revision` values distinguish empty strings from nulls. Normalized `Change` values use `None` for absent old/new text and `API` for an unattributed actor. Data service history supplements available English context from collected translations and revisions. Source deletions retain the old English text. All returned timestamps are timezone-aware UTC values.

## Errors and resource ownership

Invalid query arguments raise `ValueError`. Transport failures, invalid responses, and unavailable collected data raise `RuntimeError` with query context. Use context managers to close clients.

API parameters, return values, and exceptions are documented on [`WebClient`](noctalia_i18n_core/web.py), [`DataClient`](noctalia_i18n_core/data.py), and the [returned values](noctalia_i18n_core/models.py).

## Development

```bash
uv sync --locked
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked pyrefly check
uv run --locked python -m unittest discover -v
```

Run live website checks explicitly with `uv run --locked python -m unittest tests.live -v`.

## License

[MIT](LICENSE)
