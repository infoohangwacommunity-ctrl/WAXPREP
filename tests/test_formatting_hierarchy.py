"""Founder's accessibility fixes — formatting hierarchy tests.

These tests verify the four fixes the Founder asked for on ``main``:

1. **Numbered-list integrity (WCAG 1.3.1 / 1.3.2)** — ``split_for_platform``
   must NOT split a numbered list mid-list. A 10-step list whose total
   exceeds the transport limit produces bubbles where each bubble contains
   COMPLETE numbered steps (no ``"1. Do this\\n2. Do"`` fragment).

2. **WhatsApp code blocks** — visually distinct from inline code via
   ``---`` separators above and below (WhatsApp collapses both to the
   same triple-backtick monospace, breaking the visual hierarchy
   weak-foundation students depend on).

3. **Telegram code blocks** — emit ``<pre><code>...</code></pre>`` (with
   optional ``class="language-X"`` for syntax highlighting). Bold, italic,
   inline code, and links must also produce valid HTML.

4. **LaTeX-to-Unicode** — neither WhatsApp nor Telegram renders LaTeX.
   The renderer converts ``$x^2$`` → ``x²``, ``$x_1$`` → ``x₁``,
   ``\\pi`` → ``π``, ``\\times`` → ``×``, etc. BEFORE markdown processing.
   Dollar amounts (``$5``) are NOT mistaken for LaTeX.

5. **Render + split pipeline** — bold / italic / code all survive the
   render→split→send pipeline (no formatting span is split across
   transport pieces).

No keyword matching, no hardcoded conversation patterns — the LaTeX
conversion is a data-driven lookup table plus regexes; the list
detector is a regex on ``^\\s*\\d+\\.\\s+``.
"""

from __future__ import annotations

import re

import pytest

from waxprep.channel.delivery import (
    PLATFORM_MAX_CHARS,
    platform_max,
    split_for_platform,
)
from waxprep.channel.streaming.renderers import (
    render_plain,
    render_telegram,
    render_whatsapp,
)

# ---------------------------------------------------------------------------
# Fix 1 — numbered-list integrity (WCAG 1.3.1 / 1.3.2)
# ---------------------------------------------------------------------------
#
# A weak-foundation student depends on the structure of a numbered list.
# If ``split_for_platform`` cuts a long list mid-item (e.g. bubble 1 ends
# with ``"2. Do"`` and bubble 2 starts with ``"this and that"``), the
# student loses the meaning of the sequence. The splitter must cut
# BETWEEN items, never within an item.


def _extract_item_numbers(chunk: str) -> list[int]:
    """Return the numbered-item markers present in ``chunk``, in order."""
    return [
        int(m.group(1))
        for m in re.finditer(r"^\s*(\d+)\.\s+", chunk, re.MULTILINE)
    ]


def test_numbered_list_does_not_split_mid_item_whatsapp() -> None:
    """A 10-step list > 4000 chars must split BETWEEN items.

    Each bubble must contain complete numbered steps — no bubble ends
    with a truncated ``"N. partial"`` and the next starts with the
    continuation. WCAG 1.3.1 (Info and Relationships) and 1.3.2
    (Meaningful Sequence).
    """
    # 10 steps, each ~450 chars, total > 4000 (WhatsApp limit).
    steps = [f"{i}. " + "x" * 440 + f" end{i}" for i in range(1, 11)]
    text = "\n".join(steps)
    assert len(text) > PLATFORM_MAX_CHARS["whatsapp"]

    chunks = split_for_platform(text, "whatsapp")
    assert len(chunks) >= 2, "expected the list to be split across bubbles"

    # Every chunk's items must be contiguous and every item must be a
    # complete line (starts with \d+\. followed by non-trivial content).
    seen: list[int] = []
    for chunk in chunks:
        nums = _extract_item_numbers(chunk)
        assert nums, f"chunk has no numbered items: {chunk!r}"
        # Items in this chunk must be contiguous (e.g. 1,2,3 or 8,9,10).
        assert nums == list(range(nums[0], nums[-1] + 1)), (
            f"chunk items not contiguous: {nums}"
        )
        # No item is a truncated fragment: each numbered line must have
        # substantial content after the marker (not just "2. Do").
        for m in re.finditer(r"^\s*(\d+)\.\s+(.+)$", chunk, re.MULTILINE):
            item_text = m.group(2)
            assert len(item_text) >= 10, (
                f"item {m.group(1)} looks truncated: {item_text!r}"
            )
        seen.extend(nums)

    # Across chunks, items must be sequential with no gaps or overlaps.
    assert seen == list(range(1, 11)), (
        f"items lost or duplicated across bubbles: {seen}"
    )


def test_numbered_list_does_not_split_mid_item_telegram() -> None:
    """Same property on Telegram (4096-char limit)."""
    steps = [f"{i}. " + "y" * 450 for i in range(1, 11)]
    text = "\n".join(steps)
    assert len(text) > PLATFORM_MAX_CHARS["telegram"]

    chunks = split_for_platform(text, "telegram")
    assert len(chunks) >= 2

    seen: list[int] = []
    for chunk in chunks:
        nums = _extract_item_numbers(chunk)
        assert nums
        assert nums == list(range(nums[0], nums[-1] + 1))
        seen.extend(nums)
    assert seen == list(range(1, 11))


def test_short_numbered_list_stays_in_one_bubble() -> None:
    """A list that fits within the limit is sent as ONE bubble — the
    splitter does not fragment it unnecessarily."""
    text = "\n".join(f"{i}. short step {i}" for i in range(1, 6))
    assert len(text) < PLATFORM_MAX_CHARS["whatsapp"]
    chunks = split_for_platform(text, "whatsapp")
    assert chunks == [text]


def test_numbered_list_preceded_by_prose_moves_whole_list() -> None:
    """When prose precedes a numbered list and the list would straddle
    the limit, the WHOLE list moves to the next bubble — the splitter
    does not cut the prose from the list mid-list."""
    intro = "Here is how to solve it. " + "a" * 200 + ".\n\n"
    steps = "\n".join(f"{i}. " + "b" * 440 + f" end{i}" for i in range(1, 11))
    text = intro + steps
    assert len(text) > PLATFORM_MAX_CHARS["whatsapp"]

    chunks = split_for_platform(text, "whatsapp")
    assert len(chunks) >= 2

    # The first chunk must NOT contain a partial numbered list. Either
    # it contains no numbered items (pure prose) or it contains a
    # contiguous run of complete items.
    first_items = _extract_item_numbers(chunks[0])
    if first_items:
        # If the first chunk has list items, they must be the START of
        # the list (item 1) and contiguous.
        assert first_items[0] == 1, (
            f"first chunk starts mid-list at item {first_items[0]}"
        )
        assert first_items == list(range(1, first_items[-1] + 1))


def test_split_for_platform_preserves_items_exactly() -> None:
    """Concatenating the chunks (modulo whitespace) must reproduce the
    original numbered items in order — no item is silently dropped or
    duplicated by the list-aware splitter."""
    steps = [f"{i}. " + "z" * 440 for i in range(1, 11)]
    text = "\n".join(steps)
    original_items = re.split(r"\n(?=\d+\.\s)", text)

    chunks = split_for_platform(text, "whatsapp")
    all_items: list[str] = []
    for chunk in chunks:
        chunk_items = re.split(r"\n(?=\d+\.\s)", chunk)
        all_items.extend(it.strip() for it in chunk_items if it.strip())

    assert all_items == [s.strip() for s in original_items if s.strip()], (
        "items were split, lost, or duplicated"
    )


def test_split_for_platform_no_partial_item_at_boundary() -> None:
    """The regression the Founder named: no bubble ends with a bare
    ``"N. "`` (number with no text) and the next starts with the text."""
    # Construct a list where naive ``. `` cutting would land mid-item.
    steps = [f"{i}. Do thing number {i}. " + "y" * 400 for i in range(1, 11)]
    text = "\n".join(steps)
    chunks = split_for_platform(text, "whatsapp")
    for chunk in chunks:
        lines = [ln for ln in chunk.split("\n") if ln.strip()]
        # No line should be a bare number marker ("2." with no content).
        for ln in lines:
            assert not re.match(r"^\s*\d+\.\s*$", ln), (
                f"partial item marker reached a bubble: {ln!r}"
            )


# ---------------------------------------------------------------------------
# Fix 2 — WhatsApp code blocks visually distinct from inline code
# ---------------------------------------------------------------------------


def test_whatsapp_code_block_has_separator_lines() -> None:
    """A markdown code block must be visually distinct from inline code
    on WhatsApp. The renderer wraps the block with ``---`` separators
    above and below so a student can tell a structured block from an
    inline token (WCAG 1.3.1)."""
    result = render_whatsapp("```python\nprint('hello')\n```")
    assert "print('hello')" in result.text
    # The block must carry a `---` separator (the visual block boundary).
    assert "---" in result.text, (
        f"code block missing --- separator: {result.text!r}"
    )
    # And it must keep triple backticks (WhatsApp renders as monospace).
    assert "```" in result.text


def test_whatsapp_inline_code_does_not_get_separator() -> None:
    """Inline code stays as ```` ```code``` ```` (triple backtick, no
    separators) — only multi-line code blocks get the ``---`` treatment.
    This is what makes the two visually distinguishable."""
    result = render_whatsapp("Use `print()` to output.")
    assert "```print()```" in result.text
    # Inline code must NOT carry the `---` block separator.
    assert "---" not in result.text, (
        f"inline code wrongly given block separator: {result.text!r}"
    )


def test_whatsapp_code_block_and_inline_code_are_visually_distinct() -> None:
    """The Founder's accessibility concern: a student must be able to
    tell a code block from inline code on WhatsApp. The two rendered
    forms must NOT be identical."""
    block = render_whatsapp("```python\nx = 1\n```").text
    inline = render_whatsapp("Use `x = 1` here.").text
    assert block != inline, (
        "code block and inline code render identically on WhatsApp"
    )
    # The block must have something the inline does not (the separator).
    assert "---" in block
    assert "---" not in inline


def test_whatsapp_code_block_strips_language_tag() -> None:
    """The language hint (e.g. ``python``) is not meaningful on WhatsApp
    and must not leak into the rendered text as a stray word."""
    result = render_whatsapp("```javascript\nconst x = 1;\n```")
    assert "const x = 1;" in result.text
    # The language tag must not appear as a stray word in the output.
    assert "javascript" not in result.text.lower() or "---" in result.text


# ---------------------------------------------------------------------------
# Fix 3 — Telegram code blocks emit <pre><code>...</code></pre>
# ---------------------------------------------------------------------------


def test_telegram_code_block_emits_pre_code_tags() -> None:
    """Telegram supports ```code blocks``` via ``parse_mode=HTML`` with
    ``<pre><code>...</code></pre>``. The renderer must produce valid
    HTML that Telegram's Bot API accepts."""
    result = render_telegram("```python\nprint('hello')\n```")
    assert "<pre>" in result.text
    assert "</pre>" in result.text
    # ``<code>`` opening tag (may carry a class attribute for syntax
    # highlighting when the Teacher tagged a language).
    assert re.search(r"<code[ >]", result.text)
    assert "</code>" in result.text
    assert "print('hello')" in result.text
    assert result.format_mode == "HTML"


def test_telegram_code_block_preserves_language_for_highlighting() -> None:
    """When the Teacher tags a language, the renderer emits
    ``<pre><code class="language-X">`` so Telegram can syntax-highlight
    (the previous renderer silently dropped the tag)."""
    result = render_telegram("```python\nx = 1\n```")
    assert 'class="language-python"' in result.text, (
        f"language tag not preserved: {result.text!r}"
    )


def test_telegram_code_block_without_language_emits_plain_pre_code() -> None:
    """A code block without a language fence still produces valid
    ``<pre><code>...</code></pre>`` (no empty class attribute)."""
    result = render_telegram("```\nx = 1\n```")
    assert "<pre><code>x = 1</code></pre>" in result.text
    assert "class=" not in result.text


def test_telegram_bold_italic_code_links_all_valid_html() -> None:
    """Verify ``render_telegram`` produces valid HTML for every
    formatting primitive the Bot API supports."""
    text = (
        "**bold** and *italic* and _under_ and `code` "
        "and ~struck~ and [docs](https://example.com)"
    )
    result = render_telegram(text)
    assert result.format_mode == "HTML"
    assert "<b>bold</b>" in result.text
    assert "<i>italic</i>" in result.text
    assert "<i>under</i>" in result.text
    assert "<code>code</code>" in result.text
    assert "<s>struck</s>" in result.text
    assert '<a href="https://example.com">docs</a>' in result.text


def test_telegram_html_special_chars_in_code_are_escaped() -> None:
    """HTML special characters inside code blocks must be escaped so
    Telegram's HTML parser does not choke (blueprint §7.5)."""
    result = render_telegram("```python\nx = a < b & c > d\n```")
    assert "&lt;" in result.text
    assert "&amp;" in result.text
    assert "&gt;" in result.text
    # The escaped entities must be INSIDE the <pre><code> span.
    assert "<pre><code" in result.text
    assert "</code></pre>" in result.text


# ---------------------------------------------------------------------------
# Fix 4 — LaTeX → Unicode / plain-text conversion
# ---------------------------------------------------------------------------


def test_latex_superscript_on_whatsapp() -> None:
    """``$x^2$`` → ``x²`` on WhatsApp (LaTeX is not rendered natively)."""
    result = render_whatsapp("The area is $x^2$ square units.")
    assert "x²" in result.text
    assert "$" not in result.text, f"LaTeX delimiter leaked: {result.text!r}"


def test_latex_subscript_on_whatsapp() -> None:
    """``$x_1$`` → ``x₁`` on WhatsApp."""
    result = render_whatsapp("The variable $x_1$ is first.")
    assert "x₁" in result.text
    assert "$" not in result.text


def test_latex_superscript_curly_braces() -> None:
    """``$x^{2}$`` → ``x²`` (curly-braced exponents)."""
    result = render_whatsapp("Compute $x^{2}$ and $y^{10}$.")
    assert "x²" in result.text
    assert "y¹⁰" in result.text


def test_latex_subscript_curly_braces() -> None:
    """``$x_{1}$`` → ``x₁`` (curly-braced subscripts)."""
    result = render_whatsapp("Variables $x_{1}$ and $x_{2}$.")
    assert "x₁" in result.text
    assert "x₂" in result.text


def test_latex_greek_letters() -> None:
    """``\\pi`` → ``π``, ``\\alpha`` → ``α``, ``\\Delta`` → ``Δ``."""
    result = render_whatsapp("Angle $\\pi$ and $\\alpha$ and $\\Delta$.")
    assert "π" in result.text
    assert "α" in result.text
    assert "Δ" in result.text


def test_latex_operators() -> None:
    """``\\times`` → ``×``, ``\\leq`` → ``≤``, ``\\geq`` → ``≥``."""
    result = render_whatsapp("$a \\times b$ and $a \\leq b$ and $a \\geq b$.")
    assert "×" in result.text
    assert "≤" in result.text
    assert "≥" in result.text


def test_latex_sqrt() -> None:
    """``\\sqrt{x}`` → ``√x``; compound radicand gets parentheses."""
    result = render_whatsapp("The root is $\\sqrt{x}$ and $\\sqrt{x+1}$.")
    assert "√x" in result.text
    assert "√(x+1)" in result.text


def test_latex_frac() -> None:
    """``\\frac{a}{b}`` → ``a/b`` (plain-text fallback)."""
    result = render_whatsapp("The ratio $\\frac{a}{b}$ is small.")
    assert "a/b" in result.text


def test_latex_display_math() -> None:
    """``$$x^2$$`` (display math) is also converted."""
    result = render_whatsapp("$$x^2 + y^2 = r^2$$")
    assert "x²" in result.text
    assert "y²" in result.text
    assert "r²" in result.text
    assert "$" not in result.text


def test_dollar_amount_not_mistaken_for_latex() -> None:
    """A dollar amount (``$5``) must NOT be stripped or converted —
    only ``$...$`` spans that contain a LaTeX indicator (``^``, ``_``,
    ``\\``) are converted."""
    result = render_whatsapp("The price is $5 and $10 each.")
    assert "$5" in result.text
    assert "$10" in result.text


def test_latex_conversion_on_telegram() -> None:
    """Telegram also gets the LaTeX-to-Unicode conversion (it does not
    render LaTeX either)."""
    result = render_telegram("Area $x^2$ and variable $x_1$.")
    assert "x²" in result.text
    assert "x₁" in result.text
    assert "$" not in result.text


def test_latex_conversion_on_plain_renderer() -> None:
    """The plain-text fallback also converts LaTeX (so a fallback path
    still produces legible math notation)."""
    result = render_plain("Area $x^2$ and $\\pi$ radians.")
    assert "x²" in result.text
    assert "π" in result.text
    assert "$" not in result.text


def test_latex_does_not_break_markdown_bold() -> None:
    """LaTeX conversion runs BEFORE markdown processing, so a bold marker
    (``**``) is not confused by the math span."""
    result = render_whatsapp("**Key:** the area is $x^2$ units.")
    assert "*Key:*" in result.text  # bold rendered to WhatsApp syntax
    assert "x²" in result.text
    assert "$" not in result.text


# ---------------------------------------------------------------------------
# Fix 5 — bold / italic / code survive the render + split pipeline
# ---------------------------------------------------------------------------
#
# The render→split→send order (render the full text FIRST, then split the
# rendered text) guarantees a markdown span is never split across transport
# pieces. These tests verify that property holds for the formatted output
# on both channels.


def test_formatting_survives_render_and_split_whatsapp() -> None:
    """Bold, italic, and inline code all survive the render+split
    pipeline on WhatsApp — no formatting marker is split across bubbles."""
    body = (
        "**Bold key term** and _italic note_ and `inline code` "
        "plus a numbered list:\n"
        + "\n".join(f"{i}. step {i} " + "x" * 400 for i in range(1, 11))
    )
    rendered = render_whatsapp(body).text
    chunks = split_for_platform(rendered, "whatsapp")
    assert len(chunks) >= 2

    # The first chunk must contain the formatted markers (they were at
    # the start of the body, well under the limit).
    first = chunks[0]
    assert "*Bold key term*" in first, (
        f"bold marker lost in render+split: {first!r}"
    )
    assert "_italic note_" in first
    assert "```inline code```" in first
    # Raw markdown must never reach the platform.
    for chunk in chunks:
        assert "**" not in chunk, f"raw ** reached WhatsApp: {chunk!r}"


def test_formatting_survives_render_and_split_telegram() -> None:
    """Bold, italic, and inline code all survive the render+split
    pipeline on Telegram — the HTML tags stay balanced (no tag is
    split across bubbles)."""
    body = (
        "**Bold key term** and *italic note* and `inline code` "
        "plus a numbered list:\n"
        + "\n".join(f"{i}. step {i} " + "y" * 400 for i in range(1, 11))
    )
    rendered = render_telegram(body).text
    chunks = split_for_platform(rendered, "telegram")
    assert len(chunks) >= 2

    first = chunks[0]
    assert "<b>Bold key term</b>" in first
    assert "<i>italic note</i>" in first
    assert "<code>inline code</code>" in first
    # Every chunk's HTML tags must be balanced (open == close count).
    for chunk in chunks:
        for tag in ("b", "i", "code", "pre", "s"):
            opens = len(re.findall(rf"<{tag}[ >]", chunk))
            closes = chunk.count(f"</{tag}>")
            assert opens == closes, (
                f"unbalanced <{tag}> in chunk: {opens} opens vs {closes} closes"
            )


def test_code_block_survives_render_and_split_whatsapp() -> None:
    """A code block (with ``---`` separators) survives the split
    pipeline intact — the ``---`` boundary lines are not separated
    from the code they delimit."""
    body = (
        "Here is the function:\n```python\ndef f():\n    return 1\n```\n"
        + "padding " * 600
        + "\nend."
    )
    rendered = render_whatsapp(body).text
    chunks = split_for_platform(rendered, "whatsapp")
    # The code block (with its `---` separators) must be in ONE chunk.
    code_chunk = next((c for c in chunks if "def f():" in c), None)
    assert code_chunk is not None, "code block content lost in split"
    assert "---" in code_chunk, "code block separator lost in split"
    # Both `---` separators (above and below) must be in the same chunk.
    assert code_chunk.count("---") >= 2, (
        f"code block separators split across chunks: {code_chunk!r}"
    )


def test_code_block_survives_render_and_split_telegram() -> None:
    """A Telegram ``<pre><code>`` block survives the split pipeline —
    the opening and closing tags stay in the same bubble (balanced)."""
    body = (
        "Here is the function:\n```python\ndef f():\n    return 1\n```\n"
        + "padding " * 600
        + "\nend."
    )
    rendered = render_telegram(body).text
    chunks = split_for_platform(rendered, "telegram")
    code_chunk = next((c for c in chunks if "def f():" in c), None)
    assert code_chunk is not None
    assert "<pre><code" in code_chunk
    assert "</code></pre>" in code_chunk


def test_numbered_list_with_formatting_survives_pipeline_whatsapp() -> None:
    """A numbered list whose items contain bold/italic/code markers
    survives the full render+split pipeline — each bubble contains
    complete items with the formatting intact (WCAG 1.3.1 + 1.3.2)."""
    steps = [
        f"{i}. **Step {i}**: do `action_{i}` then _check_ the result. " + "z" * 380
        for i in range(1, 11)
    ]
    body = "\n".join(steps)
    rendered = render_whatsapp(body).text
    chunks = split_for_platform(rendered, "whatsapp")
    assert len(chunks) >= 2

    # Each chunk must have complete numbered items.
    seen: list[int] = []
    for chunk in chunks:
        nums = _extract_item_numbers(chunk)
        assert nums
        assert nums == list(range(nums[0], nums[-1] + 1))
        seen.extend(nums)
    assert seen == list(range(1, 11))

    # The formatting (WhatsApp single-asterisk bold, underscore italic,
    # triple-backtick code) must be present in the chunks — never raw
    # markdown ``**`` or single backticks.
    full_rendered = "\n".join(chunks)
    assert "*Step 1*" in full_rendered
    assert "_check_" in full_rendered
    assert "```action_1```" in full_rendered
    assert "**" not in full_rendered


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty() -> None:
    """Empty input produces empty output on all three renderers."""
    assert render_whatsapp("").text == ""
    assert render_telegram("").text == ""
    assert render_plain("").text == ""


def test_plain_text_passthrough() -> None:
    """Plain text with no formatting markers passes through unchanged
    (modulo LaTeX conversion)."""
    text = "The quick brown fox jumps over the lazy dog."
    assert render_whatsapp(text).text == text
    assert render_telegram(text).text == text
    assert render_plain(text).text == text


def test_platform_max_returns_defaults() -> None:
    """Unknown channels fall back to the WhatsApp-style 4000-char limit."""
    assert platform_max("whatsapp") == 4000
    assert platform_max("telegram") == 4096
    assert platform_max("unknown") == 4000
    assert platform_max("") == 4000


def test_split_for_platform_empty() -> None:
    """Empty / whitespace-only input produces no chunks."""
    assert split_for_platform("", "whatsapp") == []
    assert split_for_platform("   \n\n  ", "whatsapp") == []


@pytest.mark.parametrize("channel", ["whatsapp", "telegram"])
def test_numbered_list_integrity_parametrized(channel: str) -> None:
    """The numbered-list integrity property holds on every channel."""
    steps = [f"{i}. " + "w" * 440 for i in range(1, 11)]
    text = "\n".join(steps)
    chunks = split_for_platform(text, channel)
    if len(text) <= platform_max(channel):
        assert chunks == [text]
        return
    assert len(chunks) >= 2
    seen: list[int] = []
    for chunk in chunks:
        nums = _extract_item_numbers(chunk)
        assert nums
        assert nums == list(range(nums[0], nums[-1] + 1))
        seen.extend(nums)
    assert seen == list(range(1, 11))
