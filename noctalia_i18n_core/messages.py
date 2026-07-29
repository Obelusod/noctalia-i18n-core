"""Load Discord embeds from named YAML message files."""

from __future__ import annotations

import re
import string
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Required, TypedDict, cast, override

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode, Node
from yaml.resolver import BaseResolver

from .diff import AnsiStyle, format_ansi_diff
from .models import (
    ACTIONS,
    SOURCE_LOCALE,
    Action,
    JsonValue,
    normalize_json,
)


class EmbedFooter(TypedDict, total=False):
    text: Required[str]
    icon_url: str


class EmbedMedia(TypedDict):
    url: str


class EmbedAuthor(TypedDict, total=False):
    name: Required[str]
    url: str
    icon_url: str


class EmbedField(TypedDict, total=False):
    name: Required[str]
    value: Required[str]
    inline: bool


class Embed(TypedDict, total=False):
    title: str
    description: str
    url: str
    timestamp: str
    color: int
    footer: EmbedFooter
    image: EmbedMedia
    thumbnail: EmbedMedia
    author: EmbedAuthor
    fields: list[EmbedField]


class MessageValues(TypedDict):
    """Values available to every message template."""

    monitor_id: str
    project: str
    key: str
    source: str | None
    old_value: str | None
    new_value: str | None
    locale: str
    actor: str
    actor_url: str | None
    actor_avatar_url: str | None
    action: Action
    change_url: str | None
    timestamp: str
    unix_time: int


class MergeValues(TypedDict):
    """Batch values available to a merged embed template."""

    monitor_id: str
    project: str
    locale: str
    count: int
    actors: str
    actor_count: int
    actor_avatar_url: str | None
    added_count: int
    modified_count: int
    deleted_count: int
    first_timestamp: str
    last_timestamp: str
    first_unix_time: int
    last_unix_time: int
    entries: str


type _DiffStyles = tuple[AnsiStyle, AnsiStyle]


@dataclass(frozen=True, slots=True)
class _TemplateScope:
    placeholders: frozenset[str]
    truncatable: frozenset[str]
    fallbackable: frozenset[str]


_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TRUNCATE_SPEC = re.compile(r"^truncate=([1-9]\d*)$")
_FALLBACK_SPEC = re.compile(r"^fallback=([^{}]+)$")
_DIFF_PLACEHOLDERS = frozenset({"old_diff", "new_diff"})
_DERIVED_PLACEHOLDERS = _DIFF_PLACEHOLDERS | {"key_link"}
_MESSAGE_PLACEHOLDERS = (
    frozenset(MessageValues.__required_keys__) | _DERIVED_PLACEHOLDERS
)
_MERGE_PLACEHOLDERS = frozenset(MergeValues.__required_keys__)
_MESSAGE_SCOPE = _TemplateScope(
    _MESSAGE_PLACEHOLDERS,
    frozenset({"key", "source", "old_value", "new_value", "actor"}),
    _MESSAGE_PLACEHOLDERS - {"unix_time"},
)
_MERGE_SCOPE = _TemplateScope(
    _MERGE_PLACEHOLDERS,
    frozenset({"actors", "entries"}),
    _MERGE_PLACEHOLDERS
    - {
        "count",
        "actor_count",
        "added_count",
        "modified_count",
        "deleted_count",
        "first_unix_time",
        "last_unix_time",
    },
)

_EMBED_KEYS = frozenset(
    {
        "title",
        "description",
        "url",
        "timestamp",
        "color",
        "footer",
        "image",
        "thumbnail",
        "author",
        "fields",
    }
)
_TEXT_LIMITS = {"title": 256, "description": 4096}
_MAX_COLOR = 0xFFFFFF
_MAX_EMBED_CHARACTERS = 6000
_MAX_FIELDS = 25
_OBJECT_SCHEMAS = {
    "footer": (frozenset({"text", "icon_url"}), frozenset({"text"})),
    "image": (frozenset({"url"}), frozenset({"url"})),
    "thumbnail": (frozenset({"url"}), frozenset({"url"})),
    "author": (frozenset({"name", "url", "icon_url"}), frozenset({"name"})),
}


def _truncate(value: str, limit: int) -> str:
    widths = tuple(
        0
        if unicodedata.combining(character)
        else 2
        if unicodedata.east_asian_width(character) in {"F", "W"}
        else 1
        for character in value
    )
    if sum(widths) <= limit:
        return value
    width = 0
    end = 0
    for character_width in widths:
        if width + character_width > limit - 1:
            break
        width += character_width
        end += 1
    return value[:end].rstrip() + "…"


class _TemplateFormatter(string.Formatter):
    @override
    def format_field(self, value: object, format_spec: str) -> str:
        if not format_spec:
            return super().format_field(value, format_spec)
        fallback = _FALLBACK_SPEC.fullmatch(format_spec)
        if fallback is not None and isinstance(value, str):
            return value or fallback.group(1)
        matched = _TRUNCATE_SPEC.fullmatch(format_spec)
        if matched is None or not isinstance(value, str):
            raise ValueError(f"Invalid template format: {format_spec!r}")
        return _truncate(value, int(matched.group(1)))


_FORMATTER = _TemplateFormatter()


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that also rejects silently overwritten keys."""

    @override
    def construct_object(self, node: Node, deep: bool = False) -> object:
        return cast(
            object,
            super().construct_object(  # pyright: ignore[reportUnknownMemberType]
                node, deep=deep
            ),
        )


def _construct_mapping(
    loader: _UniqueKeyLoader, node: MappingNode, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "mapping keys must be scalar values",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key: {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


@dataclass(frozen=True, slots=True)
class MessageTemplate:
    """One selectable file containing detailed message templates."""

    name: str
    templates: Mapping[Action, Embed]
    diff_styles: _DiffStyles | None

    def render(self, action: Action, values: Mapping[str, object]) -> Embed:
        """Render and validate one detailed embed from external template values."""

        try:
            template = self.templates[action]
        except KeyError:
            raise ValueError(
                f"Message {self.name!r} has no template for action {action!r}"
            ) from None
        label = f"message {self.name!r} action {action!r}"
        normalized = normalize_json(template, label)
        scope = _render_values(values, f"{label} values", self.diff_styles)
        rendered = _render_value(normalized, scope)
        _omit_empty_urls(rendered)
        return _validate_embed(rendered, label, rendered=True)


@dataclass(frozen=True, slots=True)
class MergePage:
    """One complete merged embed and the number of entries it contains."""

    count: int
    embed: Embed

    def __post_init__(self) -> None:
        if type(self.count) is not int or self.count < 1:
            raise ValueError("Merge page count must be a positive integer")


@dataclass(frozen=True, slots=True)
class MergeTemplate:
    """One selectable merge message template."""

    name: str
    source: Embed
    target: Embed
    entries: Mapping[Action, str]
    separator: str
    diff_styles: _DiffStyles | None

    def render(self, values: Sequence[MessageValues]) -> tuple[MergePage, ...]:
        """Render every entry, splitting the locale batch at Discord limits."""

        if not values:
            raise ValueError("A merge requires at least one delivery")
        if len({item["locale"] for item in values}) != 1:
            raise ValueError("A merge cannot mix locales")
        if len({(item["monitor_id"], item["project"]) for item in values}) != 1:
            raise ValueError("A merge cannot mix monitor contexts")

        pages: list[MergePage] = []
        current: list[MessageValues] = []
        rendered: Embed | None = None
        for item in values:
            candidate = (*current, item)
            # Retain the last valid page so a Discord limit starts a new one.
            try:
                candidate_embed = self._render_page(candidate)
            except ValueError as exc:
                if not current or rendered is None:
                    raise ValueError(
                        f"Merged change cannot fit in one Discord embed: {exc}"
                    ) from exc
                pages.append(MergePage(len(current), rendered))
                current = [item]
                try:
                    rendered = self._render_page((item,))
                except ValueError as single_exc:
                    raise ValueError(
                        f"Merged change cannot fit in one Discord embed: {single_exc}"
                    ) from single_exc
            else:
                current.append(item)
                rendered = candidate_embed

        if current and rendered is not None:
            pages.append(MergePage(len(current), rendered))
        return tuple(pages)

    def _render_page(self, values: Sequence[MessageValues]) -> Embed:
        label = f"message {self.name!r} merge"
        entries: list[str] = []
        for index, item in enumerate(values):
            entry_scope = _render_values(
                item,
                f"{label} entry {index} values",
                self.diff_styles,
            )
            rendered = _render_string(self.entries[item["action"]], entry_scope)
            if not rendered:
                raise ValueError(f"{label} entry {index} must render as non-empty text")
            entries.append(rendered)

        actors = tuple(dict.fromkeys(item["actor"] for item in values))
        actor_avatar_url = (
            next(
                (
                    item["actor_avatar_url"]
                    for item in reversed(values)
                    if item["actor_avatar_url"]
                ),
                None,
            )
            if len(actors) == 1
            else None
        )
        first, last = values[0], values[-1]
        scope: MergeValues = {
            "monitor_id": last["monitor_id"],
            "project": last["project"],
            "locale": last["locale"],
            "count": len(values),
            "actors": ", ".join(actors),
            "actor_count": len(actors),
            "actor_avatar_url": actor_avatar_url,
            "added_count": sum(item["action"] == "added" for item in values),
            "modified_count": sum(item["action"] == "modified" for item in values),
            "deleted_count": sum(item["action"] == "deleted" for item in values),
            "first_timestamp": first["timestamp"],
            "last_timestamp": last["timestamp"],
            "first_unix_time": first["unix_time"],
            "last_unix_time": last["unix_time"],
            "entries": self.separator.join(entries),
        }
        selected_template = (
            self.source if last["locale"] == SOURCE_LOCALE else self.target
        )
        template = normalize_json(selected_template, label)
        merge_scope = normalize_json(scope, f"{label} values")
        if not isinstance(merge_scope, dict):
            raise TypeError("Merge values must be a JSON object")
        rendered = _render_value(template, merge_scope)
        _omit_empty_urls(rendered)
        return _validate_embed(rendered, label, rendered=True)


def _read_yaml(path: Path, label: str) -> dict[str, JsonValue]:
    try:
        with path.open(encoding="utf-8") as handle:
            payload: object = yaml.load(handle, Loader=_UniqueKeyLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Cannot load {label} from {path}: {exc}") from exc
    value = normalize_json(payload, label)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a YAML mapping")
    return value


def _exact_keys(
    value: dict[str, JsonValue],
    allowed: frozenset[str],
    required: frozenset[str],
    label: str,
) -> None:
    problems: list[str] = []
    if missing := required - value.keys():
        problems.append(f"missing required fields: {sorted(missing)}")
    if unknown := value.keys() - allowed:
        problems.append(f"unknown fields: {sorted(unknown)}")
    if problems:
        raise ValueError(f"{label} is invalid: {'; '.join(problems)}")


def _validate_placeholder_format(
    field_name: str | None,
    format_spec: str,
    label: str,
    scope: _TemplateScope,
) -> None:
    fallback = _FALLBACK_SPEC.fullmatch(format_spec)
    if fallback is not None:
        if field_name not in scope.fallbackable:
            raise ValueError(f"{label} does not support fallback for {field_name!r}")
        return
    matched = _TRUNCATE_SPEC.fullmatch(format_spec)
    if field_name not in scope.truncatable or matched is None:
        raise ValueError(
            f"{label} contains an unsupported format specification: {format_spec!r}"
        )
    if int(matched.group(1)) > _MAX_EMBED_CHARACTERS:
        raise ValueError(
            f"{label} truncate length must not exceed {_MAX_EMBED_CHARACTERS}"
        )


def _validate_template(
    template: str,
    label: str,
    scope: _TemplateScope,
) -> None:
    """Validate placeholders without restricting ordinary Discord text."""

    try:
        for _, field_name, format_spec, conversion in _FORMATTER.parse(template):
            if field_name is not None and field_name not in scope.placeholders:
                raise ValueError(
                    f"{label} contains an unknown placeholder: {field_name!r}"
                )
            if conversion:
                raise ValueError(f"{label} does not support placeholder conversions")
            if format_spec:
                _validate_placeholder_format(field_name, format_spec, label, scope)
    except ValueError as exc:
        if str(exc).startswith(label):
            raise
        raise ValueError(f"{label} is not a valid template: {exc}") from exc


def _text(
    value: object,
    label: str,
    *,
    rendered: bool,
    limit: int | None = None,
    scope: _TemplateScope = _MESSAGE_SCOPE,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if not value:
        raise ValueError(f"{label} must not be empty")
    if rendered:
        if limit is not None and len(value) > limit:
            raise ValueError(f"{label} must not exceed {limit} characters")
    else:
        _validate_template(value, label, scope)
    return value


def _object(value: JsonValue, label: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping with string keys")
    return value


def _validate_color(embed: dict[str, JsonValue], label: str) -> None:
    if "color" not in embed:
        return
    color = embed["color"]
    if (
        isinstance(color, bool)
        or not isinstance(color, int)
        or not 0 <= color <= _MAX_COLOR
    ):
        raise ValueError(f"{label}.color must be an integer from 0 to {_MAX_COLOR}")


def _validate_nested_objects(
    embed: dict[str, JsonValue],
    label: str,
    rendered: bool,
    scope: _TemplateScope,
) -> None:
    for key, (allowed, required) in _OBJECT_SCHEMAS.items():
        if key not in embed:
            continue
        nested = _object(embed[key], f"{label}.{key}")
        _exact_keys(nested, allowed, required, f"{label}.{key}")
        for child, child_value in nested.items():
            limit = None
            if key == "footer" and child == "text":
                limit = 2048
            elif key == "author" and child == "name":
                limit = 256
            _text(
                child_value,
                f"{label}.{key}.{child}",
                rendered=rendered,
                limit=limit,
                scope=scope,
            )


def _validate_fields(
    embed: dict[str, JsonValue],
    label: str,
    rendered: bool,
    scope: _TemplateScope,
) -> None:
    if "fields" not in embed:
        return
    fields = embed["fields"]
    if not isinstance(fields, list) or not fields or len(fields) > _MAX_FIELDS:
        raise ValueError(
            f"{label}.fields must contain between 1 and {_MAX_FIELDS} fields"
        )
    for index, item in enumerate(fields):
        field_label = f"{label}.fields[{index}]"
        field = _object(item, field_label)
        _exact_keys(
            field,
            frozenset({"name", "value", "inline"}),
            frozenset({"name", "value"}),
            field_label,
        )
        _text(
            field["name"],
            f"{field_label}.name",
            rendered=rendered,
            limit=256,
            scope=scope,
        )
        _text(
            field["value"],
            f"{field_label}.value",
            rendered=rendered,
            limit=1024,
            scope=scope,
        )
        if "inline" in field and not isinstance(field["inline"], bool):
            raise ValueError(f"{field_label}.inline must be a boolean")


def _validate_embed(
    value: JsonValue,
    label: str,
    *,
    rendered: bool,
    scope: _TemplateScope = _MESSAGE_SCOPE,
) -> Embed:
    embed = _object(value, label)
    _exact_keys(embed, _EMBED_KEYS, frozenset(), label)
    if not embed:
        raise ValueError(f"{label} must contain at least one Discord embed component")

    for key in ("title", "description", "url", "timestamp"):
        if key in embed:
            _text(
                embed[key],
                f"{label}.{key}",
                rendered=rendered,
                limit=_TEXT_LIMITS.get(key),
                scope=scope,
            )

    _validate_color(embed, label)
    _validate_nested_objects(embed, label, rendered, scope)
    _validate_fields(embed, label, rendered, scope)
    # The checks above establish the TypedDict structure at this YAML boundary.
    typed = cast(Embed, embed)
    if rendered and embed_size(typed) > _MAX_EMBED_CHARACTERS:
        raise ValueError(
            f"{label} must not exceed Discord's "
            f"{_MAX_EMBED_CHARACTERS}-character aggregate limit"
        )
    return typed


def _render_string(template: str, values: Mapping[str, JsonValue]) -> str:
    scope = {key: "" if value is None else value for key, value in values.items()}
    return _FORMATTER.vformat(template, (), scope)


def _render_values(
    values: Mapping[str, object],
    label: str,
    diff_styles: _DiffStyles | None,
) -> dict[str, JsonValue]:
    scope = normalize_json(dict(values), label)
    if not isinstance(scope, dict):
        raise TypeError("Message values must be a JSON object")
    old = scope.get("old_value")
    new = scope.get("new_value")
    if old is not None and not isinstance(old, str):
        raise ValueError(f"{label}.old_value must be a string or null")
    if new is not None and not isinstance(new, str):
        raise ValueError(f"{label}.new_value must be a string or null")
    key = scope.get("key")
    change_url = scope.get("change_url")
    if key is not None:
        if not isinstance(key, str) or not key:
            raise ValueError(f"{label}.key must be a non-empty string")
        if change_url is not None and not isinstance(change_url, str):
            raise ValueError(f"{label}.change_url must be a string or null")
        scope["key_link"] = f"[{key}]({change_url})" if change_url else f"`{key}`"
    if diff_styles is not None:
        old_style, new_style = diff_styles
        old_diff, new_diff = format_ansi_diff(
            old,
            new,
            old_style=old_style,
            new_style=new_style,
        )
        scope["old_diff"] = old_diff
        scope["new_diff"] = new_diff
    return scope


def _render_value(value: JsonValue, values: Mapping[str, JsonValue]) -> JsonValue:
    if isinstance(value, str):
        return _render_string(value, values)
    if isinstance(value, list):
        return [_render_value(item, values) for item in value]
    if isinstance(value, dict):
        return {key: _render_value(item, values) for key, item in value.items()}
    return value


def _omit_empty_urls(value: JsonValue) -> None:
    if not isinstance(value, dict):
        return
    if value.get("url") == "":
        del value["url"]
    author = value.get("author")
    if not isinstance(author, dict):
        return
    for key in ("url", "icon_url"):
        if author.get(key) == "":
            del author[key]


def _uses_diff(value: JsonValue) -> bool:
    if isinstance(value, str):
        return any(
            field_name in _DIFF_PLACEHOLDERS
            for _, field_name, _, _ in _FORMATTER.parse(value)
        )
    if isinstance(value, list):
        return any(_uses_diff(item) for item in value)
    if isinstance(value, dict):
        return any(_uses_diff(item) for item in value.values())
    return False


def _load_diff_styles(
    value: JsonValue | None,
    label: str,
    *,
    required: bool,
) -> _DiffStyles | None:
    if value is None:
        if required:
            raise ValueError(
                f"{label}.diff is required when diff placeholders are used"
            )
        return None

    raw = _object(value, f"{label}.diff")
    sides = frozenset({"old", "new"})
    _exact_keys(raw, sides, sides, f"{label}.diff")
    styles: list[AnsiStyle] = []
    for side in ("old", "new"):
        style_label = f"{label}.diff.{side}"
        style = _object(raw[side], style_label)
        fields = frozenset({"color", "bold", "underline"})
        _exact_keys(style, fields, fields, style_label)
        color = style["color"]
        bold = style["bold"]
        underline = style["underline"]
        if color is not None and not isinstance(color, str):
            raise ValueError(f"{style_label}.color must be a string or null")
        if not isinstance(bold, bool) or not isinstance(underline, bool):
            raise ValueError(f"{style_label} emphasis settings must be booleans")
        try:
            styles.append(AnsiStyle(color, bold, underline))
        except ValueError as exc:
            raise ValueError(f"{style_label}: {exc}") from exc
    return styles[0], styles[1]


def _load_merge(path: Path, name: str) -> MergeTemplate:
    label = f"message {name!r} merge"
    raw = _read_yaml(path, label)
    _exact_keys(
        raw,
        frozenset({"source", "target", "diff", "entries"}),
        frozenset({"source", "target", "entries"}),
        label,
    )
    source = _validate_embed(
        raw["source"],
        f"{label}.source",
        rendered=False,
        scope=_MERGE_SCOPE,
    )
    target = _validate_embed(
        raw["target"],
        f"{label}.target",
        rendered=False,
        scope=_MERGE_SCOPE,
    )
    entries = _object(raw["entries"], f"{label}.entries")
    required = frozenset({*ACTIONS, "separator"})
    _exact_keys(entries, required, required, f"{label}.entries")
    separator = entries["separator"]
    if not isinstance(separator, str):
        raise ValueError(f"{label}.entries.separator must be a string")
    templates: dict[Action, str] = {}
    for action in ACTIONS:
        templates[action] = _text(
            entries[action],
            f"{label}.entries.{action}",
            rendered=False,
            scope=_MESSAGE_SCOPE,
        )
    diff_styles = _load_diff_styles(
        raw.get("diff"),
        label,
        required=any(_uses_diff(template) for template in templates.values()),
    )
    return MergeTemplate(
        name=name,
        source=source,
        target=target,
        entries=templates,
        separator=separator,
        diff_styles=diff_styles,
    )


def _load_message(path: Path, name: str) -> MessageTemplate:
    label = f"message {name!r}"
    raw = _read_yaml(path, label)
    actions = frozenset(ACTIONS)
    _exact_keys(raw, actions | {"diff"}, actions, label)
    uses_diff = any(_uses_diff(raw[action]) for action in ACTIONS)
    templates: dict[Action, Embed] = {}
    for action in ACTIONS:
        action_label = f"{label} action {action!r}"
        templates[action] = _validate_embed(raw[action], action_label, rendered=False)
    diff_styles = _load_diff_styles(
        raw.get("diff"),
        label,
        required=uses_diff,
    )
    return MessageTemplate(
        name=name,
        templates=templates,
        diff_styles=diff_styles,
    )


def load_message(name: str, directory: Path) -> MessageTemplate:
    """Load and validate one named message file."""

    if not _NAME_PATTERN.fullmatch(name):
        raise ValueError(f"Message name must match {_NAME_PATTERN.pattern!r}")
    path = directory / f"{name}.yaml"
    if not path.is_file():
        raise ValueError(f"Unknown message: {name!r}")
    return _load_message(path, name)


def load_merge(name: str, directory: Path) -> MergeTemplate:
    """Load and validate one named merge message file."""

    if not _NAME_PATTERN.fullmatch(name):
        raise ValueError(f"Message name must match {_NAME_PATTERN.pattern!r}")
    path = directory / f"{name}.yaml"
    if not path.is_file():
        raise ValueError(f"Unknown merge message: {name!r}")
    return _load_merge(path, name)


def embed_size(embed: Embed) -> int:
    """Return the text characters counted toward Discord's per-message limit."""

    total = len(embed.get("title", "")) + len(embed.get("description", ""))
    if footer := embed.get("footer"):
        total += len(footer["text"])
    if author := embed.get("author"):
        total += len(author["name"])
    return total + sum(
        len(field["name"]) + len(field["value"]) for field in embed.get("fields", [])
    )
