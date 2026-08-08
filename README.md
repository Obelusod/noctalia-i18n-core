# Noctalia i18n Core

[简体中文](https://github.com/Obelusod/noctalia-i18n-core/blob/main/README.zh-CN.md)

Noctalia i18n Core is a read-only Python client for project progress, current translations, and changes from [Noctalia Translate](https://i18n.noctalia.dev/). Storage, scheduling, delivery, and application policy belong to the caller.

## Core capabilities

- Read project details, translation context, and project-wide and per-locale progress.
- Read current translations as a complete catalog, exact-key results, or text-search results.
- Query missing keys and translations that need review in a selected locale.
- List dot-separated translation keys and filter them by hierarchy prefix.
- Read complete changes for selected keys in one locale.
- Lazily read the project-wide change stream with locale, time, and anchor boundaries.
- Use a caller-owned `requests.Session` when connection policy must be configured by the application.

## Project structure

```text
noctalia_i18n_core/
├── client.py   # Website requests and response validation
└── models.py   # Normalized project and change values
```

## Installation

```bash
uv add noctalia-i18n-core
```

Or with pip:

```bash
pip install noctalia-i18n-core
```

## Usage

### Create a client

```python
from noctalia_i18n_core import NoctaliaClient

with NoctaliaClient("noctalia", timeout=30) as client:
    key = "settings.widgets.settings.scroll-repeat.label"
    value = client.translations(key, locales=("zh-Hans",))[key]["zh-Hans"]
    changes = client.changes(key, locale="zh-Hans")

    print(value)
    print(changes[0].occurred_at if changes else "No changes")
```

The client closes only the session it creates. Pass a configured `requests.Session` when the application owns connection settings such as proxies or retries.

### Project details and progress

`project()` makes one project request and returns the public name, description, translation context, and current progress. Project and locale progress use the integer percentage shown by the website. The review count identifies translations whose English source changed after that translation was last saved.

```python
project = client.project()

print(project.name, project.key_count, project.language_count, project.progress)
for locale in project.locales:
    print(
        locale.code,
        locale.progress,
        locale.missing_count,
        locale.review_count,
    )
```

### Current translations

`translations()` reads one or more exact keys in one batch request. Its key-first result preserves the requested key and locale order. A requested key remains present with an empty mapping when no selected locale has a value.

```python
values = client.translations(
    "settings.widgets.settings.scroll-repeat.description",
    "settings.widgets.settings.scroll-repeat.label",
    locales=("zh-Hans", "en"),
)
```

`catalog()` makes one complete export request and returns `{key: {locale: value}}`. It is intended for callers that need every current translation.

```python
catalog = client.catalog()
source_text = catalog["settings.widgets.settings.scroll-repeat.description"]["en"]
```

### Text search

`search_translations()` searches the key and current value in `search_locale`, follows all result pages, and reads the requested result locales for the matching keys. If `locales` is omitted, the result contains only `search_locale`.

```python
matches = client.search_translations(
    "确认",
    search_locale="zh-Hans",
    locales=("zh-Hans", "en"),
)
```

### Missing translations and reviews

`missing_keys()` returns keys without a current translation in one locale. `reviews()` returns translations whose English source changed after the translation was last saved, including the previous and current English source. Both queries accept `query` to filter their results.

```python
missing = client.missing_keys("fr", query="scroll")
reviews = client.reviews("fr")

for review in reviews:
    print(review.key, review.old_source, review.new_source)
```

### Translation keys

`keys()` makes one key-tree request and returns the complete key list. A `prefix` matches a complete dot-separated hierarchy rather than an arbitrary string prefix.

```python
widget_keys = client.keys(prefix="settings.widgets")
```

### Changes for selected keys

`changes()` reads the complete changes for one or more exact keys in one locale. The returned tuple is ordered from newest to oldest; multiple key results are merged in that order.

```python
changes = client.changes(
    "settings.widgets.settings.scroll-repeat.description",
    locale="zh-Hans",
)
for change in changes:
    print(change.occurred_at, change.action, change.new_value)
```

### Project change stream

`iter_changes()` lazily reads the project-wide change stream in the order supplied by the website. `since` and `until` are inclusive, timezone-aware boundaries and are normalized to UTC. `locale` restricts the stream to one locale.

```python
from datetime import UTC, datetime

recent = client.iter_changes(
    locale="zh-Hans",
    since=datetime(2026, 8, 1, tzinfo=UTC),
)
for change in recent:
    print(change.key, change.action)
```

`after` is a website change ID. The client confirms the anchor before yielding a result and raises `RuntimeError` when it is absent. `on_progress` receives the completed page number and current total page count after each request.

## API reference

### `NoctaliaClient`

```python
NoctaliaClient(project, *, timeout, session=None)
```

`project` is the website project identifier. `timeout` is a positive finite number of seconds. `session` may be a caller-owned `requests.Session`; the client closes it only when it created the session itself.

| Member | Result |
| --- | --- |
| `slug` | The project identifier passed to the constructor |
| `project()` | `Project` details and current translation progress |
| `keys(*, prefix=None)` | `tuple[str, ...]` of complete or prefix-matched keys |
| `catalog()` | `dict[str, dict[str, str]]` containing the complete current catalog |
| `translations(key, *keys, locales=None)` | Key-first exact translations |
| `search_translations(query, *, search_locale, locales=None)` | Key-first translations for text matches |
| `missing_keys(locale, *, query=None)` | Missing translation keys in one locale |
| `reviews(locale, *, query=None)` | Translations needing review in one locale |
| `changes(key, *keys, locale)` | Newest-first exact-key changes |
| `iter_changes(*, locale=None, since=None, until=None, after=None, on_progress=None)` | Lazy project-wide change iterator |
| `close()` | Closes an owned HTTP session |

`translations()` and `changes()` require at least one positional key. `translations()` returns requested keys even when their result is empty. `changes()` returns a tuple and requests each selected key from the website. `iter_changes()` never accepts keys and remains lazy until iteration begins.

Invalid arguments raise `ValueError`. Network failures and responses that do not match the website contract raise `RuntimeError`.

### `Project` and `Locale`

`Project` contains public project details and progress aggregated from the current locale states:

| Field or property | Description |
| --- | --- |
| `slug` | Website project identifier |
| `name` | Project name |
| `description` | Project description, or `None` when absent |
| `translation_context` | Translation guidance and terminology, or `None` when absent |
| `key_count` | Current translation key count |
| `locales` | Website-ordered `tuple[Locale, ...]` |
| `language_count` | Configured locale count |
| `translation_count` | Current translation count |
| `missing_count` | Current missing translation count |
| `review_count` | Current translations that need review |
| `progress` | Integer completion percentage shown by the website |

Each `Locale` contains `code`, `translation_count`, `missing_count`, `review_count`, and the derived `progress`. Locales without any current translations remain present in the project result.

### `Review`

Each review contains `key`, `locale`, `old_source`, `new_source`, and `translation_url`. `old_source` is `None` when no previous source text exists. The source difference comes from the website's Needs review query and is distinct from changes to the translation itself.

### `Change`

Each change has the following fields:

| Field | Description |
| --- | --- |
| `id` | Website change identifier |
| `key` | Translation key |
| `locale` | Locale changed by the record |
| `action` | `added`, `modified`, or `deleted` |
| `old_value` | Previous text, or `None` for an addition |
| `new_value` | New text, or `None` for a deletion |
| `occurred_at` | UTC-aware `datetime` |
| `translation_url` | Translation page for the key and locale |
| `actor_login` | Contributor login, or `None` for an automated change |
| `actor_url` | Contributor profile URL, when available |
| `actor_avatar_url` | Contributor avatar URL, when available |

`ChangeAction` is the literal type `"added" | "modified" | "deleted"`. `SOURCE_LOCALE` is `"en"`.

## Performance reference

These reference measurements were taken from the public `noctalia` project in August 2026. They describe website requests without a local cache.

| Operation | Requests | Reference result |
| --- | --- | --- |
| `project()` | One project page request | Progress for 25 locales, about 23 KB |
| `catalog()` | One complete export | 42,857 strings, about 2.23 MB, 4.4–4.8 s |
| `translations()` | One batch request | Two keys and two locales, about 1.3 KB, about 1.4 s |
| `search_translations()` | Search pages plus one batch request | 19 Simplified Chinese matches, about 1.7 s |
| `missing_keys()` | One missing query | All matching keys for the selected locale |
| `reviews()` | One review query | Matching keys and their English source difference |
| `keys()` | One key-tree page | About 2,474 keys and 147 KB |
| `changes()` | One request per selected key | Example key, about 1.3 KB and 1.4 s |
| `iter_changes()` | Recent Changes pages until the boundary | Depends on the requested range |

Latency, server load, and project growth affect these values. Exact queries and text searches do not download the complete catalog.

## Development

Install the locked development environment:

```bash
uv sync --locked
```

Run formatting, linting, type checking, and tests:

```bash
uv run ruff format --check .
uv run ruff check .
uv run basedpyright
uv run python -m unittest discover -v
```

Run the live contract tests explicitly to verify every public operation against the current public projects on Noctalia Translate:

```bash
uv run python -m unittest tests.live -v
```

Build and inspect both distributions:

```bash
uv build --no-sources --clear
uvx twine check --strict dist/*
```

## License

[MIT](LICENSE)
