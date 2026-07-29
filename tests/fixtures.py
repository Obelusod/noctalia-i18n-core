"""Deterministic source and domain fixtures."""

from __future__ import annotations

from datetime import UTC, datetime

from noctalia_i18n_core import JsonValue
from noctalia_i18n_core.models import Action, Change, Delivery

RUN_AT = datetime(2026, 7, 17, 9, 51, 4, 838000, tzinfo=UTC)

RECORDED_ID = "fb14adc9-7b0b-4261-92c3-e9366ee7a1f1"
RECORDED_KEY = "settings.schema.notifications.border.description"
RECORDED_SOURCE = "Draw an outline around notification toasts"
RECORDED_OLD_VALUE = "在 Toast 通知周围绘制边框"
RECORDED_NEW_VALUE = "在 Toast 通知周围绘制轮廓"
RECORDED_ACTOR = "Obelusod"

FIXTURE_KEY = "fixture.translation.description"
FIXTURE_SOURCE = "Fixture source text"
FIXTURE_OLD_VALUE = "Fixture original value"
FIXTURE_NEW_VALUE = "Fixture modified value"
FIXTURE_ACTOR = "fixture-editor"
FIXTURE_URL = "https://fixtures.invalid/changes/fixture.translation.description"


def page_data(
    *changes: tuple[str, str, str, str | None, int, str | None, str | None],
    project: str = "noctalia",
    page: int = 1,
    total_pages: int = 1,
    changes_locale: str = "",
) -> dict[str, JsonValue]:
    """Build data matching the live SvelteKit page response."""

    project_id = f"project-{project}"
    records: list[JsonValue] = []
    for change_id, key, locale, actor, created_at, old_value, new_value in changes:
        records.append(
            {
                "id": change_id,
                "translation_id": f"translation-{change_id}",
                "project_id": project_id,
                "translation_key": key,
                "locale": locale,
                "text": old_value,
                "status": "published",
                "created_by": "user" if actor else None,
                "created_at": created_at,
                "new_text": new_value,
                "created_by_login": actor,
            }
        )

    values: list[JsonValue] = []

    def reference(value: JsonValue) -> int:
        index = len(values)
        values.append(None)
        if isinstance(value, dict):
            encoded: JsonValue = {key: reference(child) for key, child in value.items()}
        elif isinstance(value, list):
            encoded = [reference(child) for child in value]
        else:
            encoded = value
        values[index] = encoded
        return index

    reference(
        {
            "project": {
                "id": project_id,
                "slug": project,
            },
            "recentChanges": records,
            "changesPage": page,
            "changesLocale": changes_locale,
            "changesTotalPages": total_pages,
        }
    )
    return {
        "type": "data",
        "nodes": [None, {"type": "data", "data": values}],
    }


# Real records captured from Recent Changes page 3 on 2026-07-18.
RECENT_CHANGES_DATA = page_data(
    (
        RECORDED_ID,
        RECORDED_KEY,
        "zh-Hans",
        RECORDED_ACTOR,
        1784281864838,
        RECORDED_OLD_VALUE,
        RECORDED_NEW_VALUE,
    ),
    (
        "60edf3d8-157e-438c-9c39-0ffbd7bb73a2",
        RECORDED_KEY,
        "de",
        "notiant",
        1784266807486,
        None,
        "Zeichne einen Umriss um Benachrichtigungs-Toasts",
    ),
)


def recorded_change() -> Change:
    return Change(
        id=RECORDED_ID,
        key=RECORDED_KEY,
        locale="zh-Hans",
        actor=RECORDED_ACTOR,
        old_value=RECORDED_OLD_VALUE,
        new_value=RECORDED_NEW_VALUE,
        action="modified",
        occurred_at=RUN_AT,
        url=(
            "https://i18n.noctalia.dev/projects/noctalia/translate?search="
            "settings.schema.notifications.border.description&locale=zh-Hans"
        ),
        actor_url="https://github.com/Obelusod",
        actor_avatar_url="https://github.com/Obelusod.png",
    )


def fixture_change(
    *,
    id: str | None = None,
    key: str = FIXTURE_KEY,
    locale: str = "zh-Hans",
    actor: str = FIXTURE_ACTOR,
    old_value: str | None = FIXTURE_OLD_VALUE,
    new_value: str | None = FIXTURE_NEW_VALUE,
    action: Action = "modified",
    occurred_at: datetime = RUN_AT,
    url: str | None = FIXTURE_URL,
    actor_url: str | None = None,
    actor_avatar_url: str | None = None,
) -> Change:
    """Build an explicitly synthetic change for behavior-only tests."""

    return Change(
        id=f"{key}@{occurred_at.isoformat()}" if id is None else id,
        key=key,
        locale=locale,
        actor=actor,
        old_value=old_value,
        new_value=new_value,
        action=action,
        occurred_at=occurred_at,
        url=url,
        actor_url=actor_url,
        actor_avatar_url=actor_avatar_url,
    )


def delivery(*, id: str | None = None, key: str = FIXTURE_KEY) -> Delivery:
    """Build a delivery from one synthetic change."""

    change = fixture_change(id=id, key=key)
    return Delivery.from_change(change, {change.key: FIXTURE_SOURCE})
