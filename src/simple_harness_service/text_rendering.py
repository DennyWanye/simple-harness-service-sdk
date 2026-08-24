from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from typing import TypeAlias

TextFragment: TypeAlias = tuple[str, str]

_BIDI_CONTROLS = frozenset(
    {
        "\u061c",
        "\u200e",
        "\u200f",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
    }
)
_FENCE = re.compile(r"^\s*```(?P<label>[^`]*)$")
_LIST_ITEM = re.compile(r"^(?P<indent> *)(?P<marker>[-+*]|[0-9]+\.)(?P<space> +)(?P<body>.*)$")


def _visible_control(character: str) -> str:
    codepoint = ord(character)
    if character == "\x7f":
        return "\u2421"
    if codepoint < 0x20:
        return chr(0x2400 + codepoint)
    return f"<U+{codepoint:04X}>"


def _sanitize_characters(text: str) -> str:
    output: list[str] = []
    for character in text:
        if character in {"\n", "\t"}:
            output.append(character)
        elif (
            character in _BIDI_CONTROLS
            or character == "\x1b"
            or unicodedata.category(character) == "Cc"
        ):
            output.append(_visible_control(character))
        else:
            output.append(character)
    return "".join(output)


def _sanitize_tabs(line: str, *, fenced: bool) -> str:
    if "\t" not in line:
        return line
    if fenced or line.startswith(("\t", "    ")):
        return line.expandtabs(4)

    output: list[str] = []
    inline_code = False
    for character in line:
        if character == "`":
            inline_code = not inline_code
            output.append(character)
        elif character == "\t":
            if inline_code:
                padding = 4 - (len("".join(output)) % 4)
                output.append(" " * padding)
            else:
                output.append("\u2409")
        else:
            output.append(character)
    return "".join(output)


def sanitize_untrusted_text(text: str) -> str:
    """Return terminal-safe text after inspecting the complete input string."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    safe = _sanitize_characters(normalized)
    fenced = False
    lines = safe.split("\n")
    sanitized: list[str] = []
    for line in lines:
        is_fence = _FENCE.fullmatch(line) is not None
        sanitized.append(_sanitize_tabs(line, fenced=fenced and not is_fence))
        if is_fence:
            fenced = not fenced
    return "\n".join(sanitized)


def _inline_fragments(text: str, *, base_style: str = "") -> Iterator[TextFragment]:
    cursor = 0
    while cursor < len(text):
        opening = text.find("`", cursor)
        if opening < 0:
            yield base_style, text[cursor:]
            return
        closing = text.find("`", opening + 1)
        if closing < 0:
            yield base_style, text[cursor:]
            return
        if opening > cursor:
            yield base_style, text[cursor:opening]
        yield "class:inline-code", text[opening + 1 : closing]
        cursor = closing + 1


def render_markdown_fragments(text: str) -> tuple[TextFragment, ...]:
    """Sanitize text and tokenize the supported terminal Markdown subset."""

    safe = sanitize_untrusted_text(text)
    fragments: list[TextFragment] = []
    fenced = False
    lines = safe.split("\n")
    for index, line in enumerate(lines):
        fence = _FENCE.fullmatch(line)
        if fence is not None:
            fenced = not fenced
        elif fenced:
            fragments.append(("class:code", line))
        else:
            item = _LIST_ITEM.fullmatch(line)
            if item is not None:
                prefix = item["indent"] + item["marker"] + item["space"]
                fragments.append(("class:list-marker", prefix))
                fragments.extend(_inline_fragments(item["body"]))
            else:
                fragments.extend(_inline_fragments(line))
        if index < len(lines) - 1 and fence is None:
            fragments.append(("", "\n"))
    return tuple(fragment for fragment in fragments if fragment[1])


def fragments_are_terminal_safe(fragments: tuple[TextFragment, ...]) -> bool:
    """Return whether fragments contain no executable controls or bidi controls."""

    for _style, text in fragments:
        for character in text:
            if character == "\n":
                continue
            if character in _BIDI_CONTROLS or character == "\x1b":
                return False
            if unicodedata.category(character) == "Cc":
                return False
    return True
