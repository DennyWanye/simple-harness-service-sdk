from __future__ import annotations

import pytest

from simple_harness_service.text_rendering import (
    fragments_are_terminal_safe,
    render_markdown_fragments,
    sanitize_untrusted_text,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("one\r\ntwo\rthree", "one\ntwo\nthree"),
        ("a\x00b\x1bc\x7fd", "a\u2400b\u241bc\u2421d"),
        ("safe\u0085text", "safe<U+0085>text"),
        ("left\u202eright\u2066end", "left<U+202E>right<U+2066>end"),
        ("plain\ttab", "plain\u2409tab"),
        ("`code\ttab`", "`code   tab`"),
        ("```python\n\tprint('x')\n```", "```python\n    print('x')\n```"),
    ],
)
def test_sanitize_untrusted_text(raw: str, expected: str) -> None:
    assert sanitize_untrusted_text(raw) == expected


def test_closed_markdown_fragments_cover_supported_grammar() -> None:
    fragments = render_markdown_fragments(
        "Paragraph with `inline` code.\n- first\n2. second\n```python\n\tprint('x')\n```"
    )

    assert fragments == (
        ("", "Paragraph with "),
        ("class:inline-code", "inline"),
        ("", " code."),
        ("", "\n"),
        ("class:list-marker", "- "),
        ("", "first"),
        ("", "\n"),
        ("class:list-marker", "2. "),
        ("", "second"),
        ("", "\n"),
        ("class:code", "    print('x')"),
        ("", "\n"),
    )


def test_unknown_or_unclosed_markdown_degrades_to_plain_text() -> None:
    assert render_markdown_fragments("## heading **bold** `open") == (
        ("", "## heading **bold** `open"),
    )


@pytest.mark.parametrize(
    "payload",
    [
        "\x1b[31mred\x1b[0m",
        "\x1b]8;;https://example.test\x07link\x1b]8;;\x07",
        "\x1bPmalicious\x1b\\",
        "prefix\u202espoof",
        "split \x1b\n[31m sequence",
        "\x00\x01\x02\x1f\x7f\u0080\u009f",
    ],
)
def test_all_fragments_are_terminal_safe(payload: str) -> None:
    fragments = render_markdown_fragments(payload)

    assert fragments_are_terminal_safe(fragments)
    assert "".join(text for _style, text in fragments) == sanitize_untrusted_text(payload)


def test_long_wide_text_is_not_truncated() -> None:
    text = "界" * 10_000
    fragments = render_markdown_fragments(text)

    assert fragments == (("", text),)
    assert fragments_are_terminal_safe(fragments)
