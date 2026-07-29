"""Format multilingual value differences for Discord ANSI code blocks."""

from __future__ import annotations

import difflib
import unicodedata
from dataclasses import dataclass

_ESCAPE = "\x1b["
_RESET = f"{_ESCAPE}0m"
_ZERO_WIDTH_SPACE = "\u200b"
_ANSI_COLORS = {
    "gray": "30",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "white": "37",
}


@dataclass(frozen=True, slots=True)
class AnsiStyle:
    """Validated emphasis and foreground color for changed text."""

    color: str | None
    bold: bool
    underline: bool

    def __post_init__(self) -> None:
        if self.color is not None and self.color not in _ANSI_COLORS:
            raise ValueError(f"Unsupported ANSI foreground color: {self.color!r}")
        if type(self.bold) is not bool or type(self.underline) is not bool:
            raise ValueError("ANSI bold and underline settings must be booleans")


def _ansi_sequence(style: AnsiStyle) -> str:
    codes: list[str] = []
    if style.bold:
        codes.append("1")
    if style.underline:
        codes.append("4")
    if style.color is not None:
        codes.append(_ANSI_COLORS[style.color])
    return f"{_ESCAPE}{';'.join(codes)}m" if codes else ""


def _is_dense_script(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x0E00 <= codepoint <= 0x0E7F  # Thai
        or 0x0E80 <= codepoint <= 0x0EFF  # Lao
        or 0x1000 <= codepoint <= 0x109F  # Myanmar
        or 0x1780 <= codepoint <= 0x17FF  # Khmer
        or 0x3040 <= codepoint <= 0x30FF  # Japanese kana
        or 0x3400 <= codepoint <= 0x9FFF  # CJK ideographs
        or 0xAC00 <= codepoint <= 0xD7AF  # Hangul
        or 0xF900 <= codepoint <= 0xFAFF  # CJK compatibility ideographs
    )


def _is_modifier(character: str) -> bool:
    codepoint = ord(character)
    return (
        bool(unicodedata.combining(character))
        or codepoint in {0xFE0E, 0xFE0F}
        or 0x1F3FB <= codepoint <= 0x1F3FF
    )


def _clusters(value: str) -> list[str]:
    clusters: list[str] = []
    for character in value:
        # Preserve modifiers and zero-width-joiner sequences as one visible unit.
        if clusters and (
            _is_modifier(character)
            or character == "\u200d"
            or clusters[-1].endswith("\u200d")
        ):
            clusters[-1] += character
        else:
            clusters.append(character)
    return clusters


def _tokens(value: str) -> list[str]:
    tokens: list[str] = []
    current = ""
    current_kind = ""
    for cluster in _clusters(value):
        character = cluster[0]
        if cluster.isspace():
            kind = "space"
        elif _is_dense_script(character):
            kind = "dense"
        elif character.isalnum() or character in {"_", "'"}:
            kind = "word"
        else:
            kind = "punctuation"

        if kind in {"dense", "punctuation"}:
            if current:
                tokens.append(current)
                current = ""
                current_kind = ""
            tokens.append(cluster)
        elif current and kind == current_kind:
            current += cluster
        else:
            if current:
                tokens.append(current)
            current = cluster
            current_kind = kind
    if current:
        tokens.append(current)
    return tokens


def _safe_value(value: str) -> str:
    return value.replace("\x1b", "\\x1b").replace("```", f"``{_ZERO_WIDTH_SPACE}`")


def _change_masks(old: list[str], new: list[str]) -> tuple[list[bool], list[bool]]:
    old_changed = [False] * len(old)
    new_changed = [False] * len(new)
    old_keys = [unicodedata.normalize("NFC", token) for token in old]
    new_keys = [unicodedata.normalize("NFC", token) for token in new]
    matcher = difflib.SequenceMatcher(None, old_keys, new_keys, autojunk=False)
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        old_changed[old_start:old_end] = [True] * (old_end - old_start)
        new_changed[new_start:new_end] = [True] * (new_end - new_start)

    for tokens, changed in ((old, old_changed), (new, new_changed)):
        for index in range(1, len(tokens) - 1):
            if tokens[index] == " " and changed[index - 1] and changed[index + 1]:
                changed[index] = True
    return old_changed, new_changed


def _style(tokens: list[str], changed: list[bool], style: AnsiStyle) -> str:
    sequence = _ansi_sequence(style)
    if not sequence:
        return "".join(tokens)
    output: list[str] = []
    active = False
    for token, different in zip(tokens, changed, strict=True):
        if different != active:
            output.append(sequence if different else _RESET)
            active = different
        output.append(token)
    if active:
        output.append(_RESET)
    return "".join(output)


def format_ansi_diff(
    old: str | None,
    new: str | None,
    /,
    *,
    old_style: AnsiStyle,
    new_style: AnsiStyle,
) -> tuple[str, str]:
    """Return old and new values with changed tokens styled for Discord ANSI."""

    old_tokens = _tokens(_safe_value(old)) if old is not None else []
    new_tokens = _tokens(_safe_value(new)) if new is not None else []
    old_changed, new_changed = _change_masks(old_tokens, new_tokens)
    return (
        _style(old_tokens, old_changed, old_style),
        _style(new_tokens, new_changed, new_style),
    )
