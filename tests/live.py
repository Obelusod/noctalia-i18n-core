"""Live contract tests for the public Noctalia Translate API."""

from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from typing import override

import requests

from noctalia_i18n_core import SOURCE_LOCALE, NoctaliaClient

_PROJECTS_URL = "https://i18n.noctalia.dev/projects"


class _ProjectLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.slugs: set[str] = set()

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        prefix = "/projects/"
        if not isinstance(href, str) or not href.startswith(prefix):
            return
        slug = href[len(prefix) :]
        if slug and "/" not in slug:
            self.slugs.add(slug)


def _projects() -> tuple[str, ...]:
    response = requests.get(_PROJECTS_URL, timeout=30)
    response.raise_for_status()
    parser = _ProjectLinks()
    parser.feed(response.text)
    if not parser.slugs:
        raise RuntimeError("The public project directory contains no projects")
    return tuple(sorted(parser.slugs))


class LiveTests(unittest.TestCase):
    def _verify_project(self, slug: str) -> None:
        with NoctaliaClient(slug, timeout=30) as client:
            project = client.project()
            keys = client.keys()
            catalog = client.catalog()

            self.assertEqual(client.slug, slug)
            self.assertEqual(project.slug, slug)
            self.assertEqual(project.key_count, len(keys))
            self.assertEqual(set(catalog), set(keys))
            self.assertGreater(len(keys), 0)

            key = keys[0]
            source_value = catalog[key][SOURCE_LOCALE]
            values = client.translations(key, locales=(SOURCE_LOCALE,))
            self.assertEqual(values[key][SOURCE_LOCALE], source_value)
            self.assertIn(
                key,
                client.search_translations(
                    key,
                    search_locale=SOURCE_LOCALE,
                    locales=(SOURCE_LOCALE,),
                ),
            )

            missing_locale = max(
                project.locales, key=lambda locale: locale.missing_count
            )
            reviews_locale = max(
                project.locales, key=lambda locale: locale.review_count
            )
            self.assertEqual(
                len(client.missing(missing_locale.code)),
                missing_locale.missing_count,
            )
            self.assertEqual(
                len(client.reviews(reviews_locale.code)),
                reviews_locale.review_count,
            )

            latest = next(client.iter_changes(), None)
            if latest is not None:
                self.assertIn(
                    latest.id,
                    {
                        change.id
                        for change in client.changes(latest.key, locale=latest.locale)
                    },
                )

    def test_every_public_project(self) -> None:
        slugs = _projects()
        with ThreadPoolExecutor(max_workers=min(4, len(slugs))) as executor:
            pending = {
                executor.submit(self._verify_project, slug): slug for slug in slugs
            }
            for future in as_completed(pending):
                with self.subTest(project=pending[future]):
                    future.result()
