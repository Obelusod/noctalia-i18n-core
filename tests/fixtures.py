"""Small deterministic responses for the public client tests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Unpack, override

import requests

if TYPE_CHECKING:
    from requests._types import (
        DataType,
        GetKwargs,
        JsonType,
        ParamsType,
        PostKwargs,
        UriType,
    )

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)


class Response(requests.Response):
    def __init__(self, payload: object, status: int = 200) -> None:
        super().__init__()
        self._payload = payload
        self.status_code = status

    @override
    def json(self, **kwargs: object) -> object:
        return self._payload


class Session(requests.Session):
    def __init__(
        self,
        get_responses: tuple[object, ...] = (),
        post_responses: tuple[object, ...] = (),
    ) -> None:
        super().__init__()
        self._get_responses = list(get_responses)
        self._post_responses = list(post_responses)
        self.get_calls: list[tuple[str, dict[str, object]]] = []
        self.post_calls: list[tuple[str, dict[str, object]]] = []
        self.closed_by_client = False

    @override
    def get(
        self,
        url: UriType,
        params: ParamsType = None,
        **kwargs: Unpack[GetKwargs],
    ) -> requests.Response:
        self.get_calls.append((str(url), {"params": params, **kwargs}))
        if not self._get_responses:
            raise AssertionError(f"Unexpected GET request: {url}")
        response = self._get_responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return (
            response if isinstance(response, requests.Response) else Response(response)
        )

    @override
    def post(
        self,
        url: UriType,
        data: DataType = None,
        json: JsonType = None,
        **kwargs: Unpack[PostKwargs],
    ) -> requests.Response:
        self.post_calls.append((str(url), {"data": data, "json": json, **kwargs}))
        if not self._post_responses:
            raise AssertionError(f"Unexpected POST request: {url}")
        response = self._post_responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return (
            response if isinstance(response, requests.Response) else Response(response)
        )

    @override
    def close(self) -> None:
        self.closed_by_client = True


def indexed_page(root: JsonValue) -> dict[str, JsonValue]:
    values: list[JsonValue] = []

    def reference(value: JsonValue) -> int:
        index = len(values)
        values.append(None)
        if isinstance(value, dict):
            encoded: JsonValue = {
                name: reference(child) for name, child in value.items()
            }
        elif isinstance(value, list):
            encoded = [reference(child) for child in value]
        else:
            encoded = value
        values[index] = encoded
        return index

    reference(root)
    return {"type": "data", "nodes": [None, {"type": "data", "data": values}]}


def translation_record(key: str, locale: str, text: str) -> dict[str, JsonValue]:
    return {
        "translation_key": key,
        "locale": locale,
        "text": text,
    }


def changes_page(
    *changes: tuple[str, str, str, str | None, int, str | None, str | None],
    page: int = 1,
    total_pages: int = 1,
    locale: str = "",
) -> dict[str, JsonValue]:
    records: list[JsonValue] = [
        {
            "id": change_id,
            "project_id": "project-noctalia",
            "translation_key": key,
            "locale": record_locale,
            "created_by_login": actor,
            "created_at": created_at,
            "text": old_value,
            "new_text": new_value,
        }
        for change_id, key, record_locale, actor, created_at, old_value, new_value in changes
    ]
    return indexed_page(
        {
            "project": {"id": "project-noctalia", "slug": "noctalia"},
            "recentChanges": records,
            "changesPage": page,
            "changesLocale": locale,
            "changesTotalPages": total_pages,
        }
    )
