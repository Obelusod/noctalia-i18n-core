"""HTTP data client validation and conditional requests."""

from __future__ import annotations

import io
import unittest
from unittest.mock import patch

import requests
from urllib3.response import HTTPResponse

from noctalia_i18n_core import DataClient, WebClient

from .fixtures import Session


class DataClientTests(unittest.TestCase):
    def test_url_validation(self) -> None:
        for url in (
            "/data",
            "file:///data",
            "https://user:secret@example.org",
            "https://example.org?key=value",
            "https://example.org#fragment",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                DataClient("noctalia", url=url)

    def test_conditional_requests_cache_only_valid_responses(self) -> None:
        responses: list[requests.Response] = []
        for status, body, etag in (
            (200, b'["key"]', '"one"'),
            (200, b'{"invalid":true}', '"bad"'),
            (304, b"", '"one"'),
        ):
            response = requests.Response()
            response.status_code = status
            response.raw = HTTPResponse(body=io.BytesIO(body), preload_content=False)
            response.headers["ETag"] = etag
            responses.append(response)
        session = requests.Session()
        self.addCleanup(session.close)
        with (
            patch.object(session, "get", side_effect=responses) as get,
            patch.object(session, "close") as close,
            DataClient(
                "noctalia", url="https://example.org", session=session
            ) as client,
        ):
            self.assertEqual(client.keys(), ("key",))
            with self.assertRaises(RuntimeError):
                client.keys()
            self.assertEqual(client.keys(), ("key",))
        self.assertEqual(get.call_args.kwargs["headers"]["If-None-Match"], '"one"')
        close.assert_not_called()

    def test_languages_validate_before_request(self) -> None:
        with DataClient("noctalia", url="https://example.org") as client:
            for locales in ((), "fr"):
                with self.subTest(locales=locales), self.assertRaises(ValueError):
                    client.translations("key", locales=locales)

    def test_common_queries_preserve_language_filtering_and_empty_values(self) -> None:
        website_session = Session(
            get_responses=(
                {
                    "en": {"nested": {"key": "English"}},
                    "fr": {"nested": {"key": ""}},
                },
            )
        )
        response = requests.Response()
        response.status_code = 200
        response.raw = HTTPResponse(
            body=io.BytesIO(b'{"nested.key":{"fr":""}}'), preload_content=False
        )
        session = requests.Session()
        self.addCleanup(session.close)
        with (
            patch.object(session, "get", return_value=response),
            WebClient("noctalia", session=website_session) as website,
            DataClient("noctalia", url="https://example.org", session=session) as data,
        ):
            clients: tuple[WebClient | DataClient, ...] = (website, data)
            for client in clients:
                with self.subTest(client=type(client).__name__):
                    self.assertEqual(
                        client.catalog(locale="fr"), {"nested.key": {"fr": ""}}
                    )
                    self.assertEqual(
                        client.translation_url("nested.key", "fr"),
                        "https://i18n.noctalia.dev/projects/noctalia/translate?search=nested.key&locale=fr",
                    )
