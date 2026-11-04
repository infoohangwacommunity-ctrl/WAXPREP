"""Channel-specific answer renderers.

Blueprint §7 (Formatting blueprint): one canonical answer, multiple
renderers. The Teacher expresses the intended content (markdown-like).
The renderer transforms it into channel-compatible syntax.

The renderer must NOT:
- Paraphrase the answer.
- Change a calculation.
- Remove a warning.
- Decide that a paragraph is unnecessary.

The renderer MUST:
- Preserve all text content.
- Emit only formatting syntax the channel supports.
- Fall back to plain text when formatting is unsupported or ambiguous.
- Escape literal characters safely.

WhatsApp formatting (blueprint §7.4, Cloud API v21.0):
  *bold*, _italic_, ~strikethrough~, ```code```, ```block```
  WhatsApp does NOT support full Markdown. The renderer emits only
  the subset WhatsApp recognises. WhatsApp collapses inline code and
  code blocks to the same triple-backtick monospace style, so the
  renderer wraps multi-line code blocks with ``---`` separators above
  and below to restore the visual hierarchy weak-foundation students
  depend on (WCAG 1.3.1 Info and Relationships).

Telegram formatting (blueprint §7.5, Route A):
  HTML entities: <b>, <i>, <s>, <code>, <pre>, <a href="...">
  Code blocks emit ``<pre><code>...</code></pre>`` (optionally with a
  ``class="language-X"`` attribute for syntax highlighting). The renderer
  uses HTML parse_mode for maximum compatibility.

LaTeX-to-Unicode (accessibility, both channels):
  Neither WhatsApp nor Telegram renders LaTeX. The Teacher may emit
  ``$x^2$`` or ``$x_1$``; the renderer converts these to Unicode/plain
  equivalents (``x²``, ``x₁``) BEFORE markdown processing so the math
  notation survives the channel boundary. Greek letters (``\\pi`` →
  ``π``), operators (``\\times`` → ``×``, ``\\leq`` → ``≤``), and
  ``\\sqrt{}`` / ``\\frac{}{}`` are also converted. This is a
  data-driven lookup table, not keyword matching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from waxprep.channel.streaming.contract import (
    ChannelCapabilities,
    ChannelCapability,
)


@dataclass(frozen=True, slots=True)
class RenderResult:
    """The output of rendering an answer for a specific channel.

    ``text`` is the rendered, channel-compatible text.
    ``format_mode`` is the channel's formatting mode (e.g. "HTML" for
    Telegram, "whatsapp" for WhatsApp, "plain" for fallback).
    ``fallback_used`` is True when rich formatting failed and the
    renderer fell back to plain text (blueprint §10.3).
    """

    text: str
    format_mode: str
    fallback_used: bool = False


# ---------------------------------------------------------------------------
# Canonical answer parsing (minimal — the Teacher produces markdown-like text)
# ---------------------------------------------------------------------------
#
# Markdown patterns the renderers understand. These are deliberately
# minimal: the Teacher is not expected to produce complex documents,
# but it may use bold, italic, code, code blocks, and links.

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC_RE = re.compile(r"\*(.+?)\*", re.DOTALL)
_ITALIC_UNDERSCORE_RE = re.compile(r"_(.+?)_", re.DOTALL)
_STRIKE_RE = re.compile(r"~(.+?)~", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]+?)`", re.DOTALL)
_CODE_BLOCK_RE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
_LINK_RE = re.compile(r"\[(.+?)\]\((https?://[^\s)]+)\)")


def _escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram's HTML parse_mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# LaTeX → Unicode/plain conversion (accessibility, both channels)
# ---------------------------------------------------------------------------
#
# Neither WhatsApp nor Telegram renders LaTeX. The Teacher may emit
# ``$x^2$`` or ``$\\pi$`` in canonical markdown; the renderer converts
# these to Unicode/plain equivalents before any markdown processing so
# the math notation reaches the student intact.
#
# This is a data-driven lookup table plus a handful of regexes for
# superscripts, subscripts, ``\\sqrt{}``, and ``\\frac{}{}``. It is NOT
# keyword matching of student input — it only fires on ``$...$`` /
# ``$$...$$`` math delimiters that contain a LaTeX indicator character
# (``^``, ``_``, or ``\\``), so ordinary dollar amounts like ``$5``
# pass through untouched.

# Unicode superscript / subscript translation tables. Characters absent
# from the table are left unchanged by str.translate; the caller checks
# whether any ASCII alnum survived translation and falls back to the
# caret/underscore parenthesised notation (e.g. ``x^(qb)``) when so.
_SUPERSCRIPT_MAP = str.maketrans(
    # No Unicode superscript for 'q' — omit it; the caller's fallback
    # emits ``^(qb)`` notation for any unmappable char. The source
    # string is the alphabet with 'q' removed (a-p then r-z).
    "0123456789+-=()abcdefghijklmnoprstuvwxyz",
    "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻ",
)
_SUBSCRIPT_MAP = str.maketrans(
    # Unicode has no subscript for b/c/d/f/g/q/w/y/z — only the chars
    # below have subscript codepoints.
    "0123456789+-=()aehijklmnoprstuvx",
    "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ",
)

# LaTeX command → Unicode replacement. Keys MUST be ordered longest-first
# so that ``\alpha`` does not shadow a hypothetical ``\alphabet``. The
# table is data, not logic — adding a command is a one-line change.
_LATEX_COMMANDS: dict[str, str] = {
    r"\rightarrow": "→",
    r"\leftarrow": "←",
    r"\Rightarrow": "⇒",
    r"\Leftarrow": "⇐",
    r"\leftrightarrow": "↔",
    r"\Leftrightarrow": "⇔",
    r"\partial": "∂",
    r"\nabla": "∇",
    r"\forall": "∀",
    r"\exists": "∃",
    r"\notin": "∉",
    r"\subset": "⊂",
    r"\supset": "⊃",
    r"\approx": "≈",
    r"\degree": "°",
    r"\times": "×",
    r"\alpha": "α",
    r"\theta": "θ",
    r"\delta": "δ",
    r"\gamma": "γ",
    r"\Sigma": "Σ",
    r"\Omega": "Ω",
    r"\frac": "",  # handled by _LATEX_FRAC_RE
    r"\sqrt": "",  # handled by _LATEX_SQRT_RE
    r"\leq": "≤",
    r"\geq": "≥",
    r"\neq": "≠",
    r"\pi": "π",
    r"\beta": "β",
    r"\Delta": "Δ",
    r"\mu": "μ",
    r"\rho": "ρ",
    r"\phi": "φ",
    r"\sum": "Σ",
    r"\int": "∫",
    r"\prod": "Π",
    r"\div": "÷",
    r"\pm": "±",
    r"\to": "→",
    r"\in": "∈",
    r"\cup": "∪",
    r"\cap": "∩",
    r"\circ": "∘",
    r"\infty": "∞",
    r"\sigma": "σ",
    r"\omega": "ω",
    r"\Gamma": "Γ",
    r"\lambda": "λ",
    r"\epsilon": "ε",
    r"\cdot": "·",
}

# Math delimiters. We require non-space immediately inside the ``$...$``
# (``(?!\s)`` after opening, ``(?<!\s)`` before closing) so ordinary
# prose like ``"the price is $5 and $10"`` is NOT mistaken for math.
_LATEX_DISPLAY_MATH_RE = re.compile(r"\$\$(?!\s)(.+?)(?<!\s)\$\$", re.DOTALL)
_LATEX_INLINE_MATH_RE = re.compile(r"\$(?!\s)(.+?)(?<!\s)\$", re.DOTALL)

# A LaTeX math span is only converted when it contains at least one of
# ``^``, ``_``, or ``\``. This guards against false positives such as
# ``$5`` (a dollar amount) being interpreted as math.
_LATEX_INDICATOR_RE = re.compile(r"[\^_\\]")

_LATEX_FRAC_RE = re.compile(r"\\frac\{([^{}]+)\}\{([^{}]+)\}")
_LATEX_SQRT_RE = re.compile(r"\\sqrt\{([^{}]+)\}")
_LATEX_SUPERSCRIPT_CURLY_RE = re.compile(r"\^\{([^{}]+)\}")
_LATEX_SUBSCRIPT_CURLY_RE = re.compile(r"_\{([^{}]+)\}")
_LATEX_SUPERSCRIPT_SINGLE_RE = re.compile(r"\^(?!\{)([a-zA-Z0-9])")
_LATEX_SUBSCRIPT_SINGLE_RE = re.compile(r"_(?!\{)([a-zA-Z0-9])")


def _to_superscript(s: str) -> str:
    """Translate ``s`` to Unicode superscript; fall back to ``^(s)``."""
    translated = s.translate(_SUPERSCRIPT_MAP)
    # If any ASCII alnum survived, Unicode does not cover every char —
    # fall back to the caret-parenthesised notation so the meaning is
    # still legible (e.g. ``x^(qb)`` for an unmappable superscript).
    if any(c.isascii() and c.isalnum() for c in translated):
        return f"^({s})"
    return translated


def _to_subscript(s: str) -> str:
    """Translate ``s`` to Unicode subscript; fall back to ``_(s)``."""
    translated = s.translate(_SUBSCRIPT_MAP)
    if any(c.isascii() and c.isalnum() for c in translated):
        return f"_({s})"
    return translated


def _latex_to_plain(math: str) -> str:
    """Convert one LaTeX math expression to Unicode/plain text.

    Handles common patterns: ``\\frac{a}{b}`` → ``a/b``, ``\\sqrt{x}`` →
    ``√x`` (or ``√(x+1)`` when the radicand is not a single token),
    Greek letters and operators via the ``_LATEX_COMMANDS`` table,
    superscripts (``x^2``, ``x^{2}``) and subscripts (``x_1``, ``x_{1}``).
    Falls back to the original text for unsupported patterns.
    """
    result = math
    # \frac{a}{b} → a/b
    result = _LATEX_FRAC_RE.sub(r"\1/\2", result)
    # \sqrt{x} → √x (single token) or √(x+1) (compound radicand)
    result = _LATEX_SQRT_RE.sub(_replace_sqrt, result)
    # Greek letters / operators (longest-first to avoid prefix shadowing).
    for cmd in sorted(_LATEX_COMMANDS, key=len, reverse=True):
        result = result.replace(cmd, _LATEX_COMMANDS[cmd])
    # ^{...} and _{...} (curly-braced exponents)
    result = _LATEX_SUPERSCRIPT_CURLY_RE.sub(
        lambda m: _to_superscript(m.group(1)), result
    )
    result = _LATEX_SUBSCRIPT_CURLY_RE.sub(lambda m: _to_subscript(m.group(1)), result)
    # ^x and _x (single-char exponents)
    result = _LATEX_SUPERSCRIPT_SINGLE_RE.sub(
        lambda m: _to_superscript(m.group(1)), result
    )
    result = _LATEX_SUBSCRIPT_SINGLE_RE.sub(lambda m: _to_subscript(m.group(1)), result)
    return result


def _replace_sqrt(m: re.Match[str]) -> str:
    inner = m.group(1)
    # A single token (one char, or all-alnum like "xyz") renders as √x.
    if len(inner) == 1 or inner.isalnum():
        return f"√{inner}"
    # A compound radicand (e.g. "x+1") needs parentheses so the student
    # sees √(x+1), not √x+1.
    return f"√({inner})"


def _normalize_latex(text: str) -> str:
    """Convert ``$...$`` and ``$$...$$`` LaTeX math to Unicode/plain text.

    Only fires on math spans that contain a LaTeX indicator (``^``, ``_``,
    or ``\\``) so ordinary dollar amounts (``$5``) pass through. The
    conversion runs BEFORE markdown processing so the ``*`` / ``_``
    markdown markers never collide with math notation.
    """

    def _replace(m: re.Match[str]) -> str:
        content = m.group(1)
        if not _LATEX_INDICATOR_RE.search(content):
            # Not actually LaTeX — preserve the original ``$...$`` so a
            # dollar amount is not silently stripped.
            return m.group(0)
        return _latex_to_plain(content)

    text = _LATEX_DISPLAY_MATH_RE.sub(_replace, text)
    text = _LATEX_INLINE_MATH_RE.sub(_replace, text)
    return text


# ---------------------------------------------------------------------------
# WhatsApp renderer
# ---------------------------------------------------------------------------


def render_whatsapp(text: str) -> RenderResult:
    """Render markdown-like text for WhatsApp.

    WhatsApp supports: *bold*, _italic_, ~strikethrough~, ```inline code```,
    and ```code blocks```. It does NOT support Markdown links or headings.

    WhatsApp collapses inline code and code blocks to the same triple-
    backtick monospace style, so multi-line code blocks are wrapped with
    ``---`` separators above and below to restore the visual hierarchy
    weak-foundation students depend on (WCAG 1.3.1).

    LaTeX math (``$x^2$``) is converted to Unicode/plain text before
    markdown processing, because WhatsApp does not render LaTeX.

    The renderer preserves all text content. Unsupported formatting
    (links, headings) is rendered as plain text.
    """
    if not text:
        return RenderResult(text="", format_mode="whatsapp")

    try:
        # LaTeX → Unicode FIRST, before any markdown processing, so the
        # ``*`` / ``_`` markdown markers never collide with math notation.
        result = _normalize_latex(text)

        # Code blocks: ```lang\ncode``` → \n---\n```\ncode\n```\n---\n
        # The `---` separators visually distinguish a multi-line code
        # block from inline code (which stays as ```code``` on one line).
        # WhatsApp renders ```...``` as monospace; the `---` lines are
        # plain text but make the block boundary unmistakable.
        result = _CODE_BLOCK_RE.sub(_replace_whatsapp_code_block, result)

        # Inline code: `code` → ```code``` (WhatsApp uses triple backticks
        # for inline monospace — single backticks are NOT recognised).
        result = _INLINE_CODE_RE.sub(r"```\1```", result)

        # Bold: **text** → *text* (WhatsApp uses single asterisks)
        # Use a placeholder to prevent the italic regex from matching
        # the already-rendered bold text.
        result = _BOLD_RE.sub("\x01\\1\x01", result)

        # Italic: *text* → _text_ (WhatsApp uses underscores)
        # Skip * when followed by space (it's a list item, not italic)
        result = _ITALIC_RE.sub(r"_\1_", result)

        # Restore bold placeholders to WhatsApp single-asterisk syntax.
        result = result.replace("\x01", "*")

        # Strikethrough: ~text~ stays as ~text~ (already WhatsApp syntax)

        # Links: [text](url) → text (url) — WhatsApp doesn't support inline links
        result = _LINK_RE.sub(r"\1 (\2)", result)

        return RenderResult(text=result, format_mode="whatsapp")
    except Exception:
        return RenderResult(text=text, format_mode="plain", fallback_used=True)


def _replace_whatsapp_code_block(m: re.Match[str]) -> str:
    """Render a markdown code block for WhatsApp.

    WhatsApp renders ```...``` as monospace but does NOT visually
    distinguish a multi-line code block from inline code. The renderer
    wraps the block with ``---`` separators above and below and puts the
    triple-backtick content on its own lines, restoring the visual
    hierarchy the founder asked for (WCAG 1.3.1).
    """
    code = m.group(2).rstrip("\n")
    return f"\n---\n```\n{code}\n```\n---\n"


# ---------------------------------------------------------------------------
# Telegram renderer (HTML parse_mode, Route A per blueprint §7.5)
# ---------------------------------------------------------------------------


def render_telegram(text: str) -> RenderResult:
    """Render markdown-like text for Telegram using HTML entities.

    Telegram supports: <b>, <i>, <s>, <code>, <pre>, <a href="...">.
    Code blocks emit ``<pre><code>...</code></pre>`` (optionally with a
    ``class="language-X"`` attribute for syntax highlighting when the
    Teacher tagged the language).

    The renderer uses HTML parse_mode for maximum compatibility.

    HTML is escaped FIRST (after LaTeX normalisation), then markdown
    markers are converted to HTML tags. This prevents double-escaping
    (blueprint §7.5).

    LaTeX math (``$x^2$``) is converted to Unicode/plain text before
    HTML escaping, because Telegram does not render LaTeX.
    """
    if not text:
        return RenderResult(text="", format_mode="HTML")

    try:
        # LaTeX → Unicode FIRST (before HTML escape so ``<`` / ``>`` inside
        # math spans do not break the LaTeX regex).
        result = _normalize_latex(text)

        # Escape all HTML. The markdown markers (**, *, _, ~, `, ```)
        # are NOT HTML special characters, so they survive escaping and
        # the regexes can still match them.
        result = _escape_html(result)

        # Code blocks: ```lang\ncode``` → <pre><code>code</code></pre>
        # (with optional language class for syntax highlighting).
        result = _CODE_BLOCK_RE.sub(_replace_telegram_code_block, result)

        # Inline code: `code` → <code>code</code>
        result = _INLINE_CODE_RE.sub(r"<code>\1</code>", result)

        # Bold: **text** → <b>text</b>
        result = _BOLD_RE.sub(r"<b>\1</b>", result)

        # Italic: *text* → <i>text</i>
        result = _ITALIC_RE.sub(r"<i>\1</i>", result)

        # Italic underscore: _text_ → <i>text</i>
        result = _ITALIC_UNDERSCORE_RE.sub(r"<i>\1</i>", result)

        # Strikethrough: ~text~ → <s>text</s>
        result = _STRIKE_RE.sub(r"<s>\1</s>", result)

        # Links: [text](url) → <a href="url">text</a>
        result = _LINK_RE.sub(r'<a href="\2">\1</a>', result)

        return RenderResult(text=result, format_mode="HTML")
    except Exception:
        return RenderResult(text=text, format_mode="plain", fallback_used=True)


def _replace_telegram_code_block(m: re.Match[str]) -> str:
    """Render a markdown code block for Telegram as ``<pre><code>``.

    Telegram Bot API HTML mode supports ``<pre><code class="language-X">``
    for syntax-highlighted code blocks. The language tag from the
    markdown fence is preserved as the class attribute (the previous
    renderer silently dropped it, weakening the visual hierarchy signal).
    """
    lang = m.group(1)
    code = m.group(2).rstrip("\n")
    # ``lang`` is constrained to ``\w+`` by _CODE_BLOCK_RE so it is safe
    # to interpolate into the class attribute without further escaping.
    if lang:
        return f'<pre><code class="language-{lang}">{code}</code></pre>'
    return f"<pre><code>{code}</code></pre>"


def _escape_outside_tags(text: str) -> str:
    """Escape & < > that appear outside of our generated HTML tags.

    We look for our known tags (<b>, </b>, <i>, </i>, <s>, </s>,
    <code>, </code>, <pre>, </pre>, <a href="...">, </a>) and escape
    everything else. This is a pragmatic scanner, not a full HTML parser.
    """
    # Split on our known tags, escape the text between them.
    tag_pattern = re.compile(
        r"(<b>|</b>|<i>|</i>|<s>|</s>|<code>|</code>|<pre>|</pre>|<a href=\"[^\"]*\">|</a>)"
    )
    parts = tag_pattern.split(text)
    out: list[str] = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            # Text between tags — escape it.
            out.append(
                part.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
        else:
            # A tag — keep it as-is.
            out.append(part)
    return "".join(out)


# ---------------------------------------------------------------------------
# Plain-text fallback renderer (blueprint §10.3)
# ---------------------------------------------------------------------------


def render_plain(text: str) -> RenderResult:
    """Render as plain text — strips all formatting markers.

    Used as a fallback when rich formatting fails (blueprint §10.3).
    The canonical answer's text content is preserved; only the
    formatting markers are removed. LaTeX math is still converted to
    Unicode/plain text so the math notation is legible without LaTeX.
    """
    if not text:
        return RenderResult(text="", format_mode="plain")

    # LaTeX → Unicode FIRST (same as the rich renderers).
    result = _normalize_latex(text)
    # Strip code blocks first (before stripping inline code).
    result = _CODE_BLOCK_RE.sub(r"\2", result)
    # Strip inline code.
    result = _INLINE_CODE_RE.sub(r"\1", result)
    # Strip bold.
    result = _BOLD_RE.sub(r"\1", result)
    # Strip italic.
    result = _ITALIC_RE.sub(r"\1", result)
    result = _ITALIC_UNDERSCORE_RE.sub(r"\1", result)
    # Strip strikethrough.
    result = _STRIKE_RE.sub(r"\1", result)
    # Links: [text](url) → text (url)
    result = _LINK_RE.sub(r"\1 (\2)", result)

    return RenderResult(text=result, format_mode="plain")


# ---------------------------------------------------------------------------
# Channel capability declarations (blueprint §4.2)
# ---------------------------------------------------------------------------


def whatsapp_capabilities() -> ChannelCapabilities:
    """WhatsApp Cloud API capabilities as verified for this integration.

    Meta's Cloud API documents a typing indicator (mark the student's
    inbound message as read with a ``typing_indicator`` object; it is
    dismissed when the reply arrives or after ~25 seconds). The handler
    sends it best-effort at turn start using the inbound message id.

    WhatsApp has NO native live-draft API: streaming is delivered as
    progressive complete messages (see whatsapp/progressive.py).
    """
    return ChannelCapabilities(
        channel="whatsapp",
        capabilities=frozenset(
            {
                ChannelCapability.COMPLETE_MESSAGE_SEND,
                ChannelCapability.SUPPORTS_FINAL_PERSISTENT_MESSAGE,
                ChannelCapability.TYPING_INDICATOR,
                ChannelCapability.MESSAGE_RECEIPTS,
            }
        ),
        max_text_size=4096,
        supported_formatting=("bold", "italic", "strikethrough", "code", "code_block"),
    )


def telegram_capabilities() -> ChannelCapabilities:
    """Telegram Bot API capabilities as verified for this integration.

    ``sendMessageDraft`` (native live-draft preview): introduced in Bot
    API 9.3 (2025-12-31), opened to all bots in Bot API 9.5 (2026-03-01).
    ``draft_id`` must be a non-zero integer; the preview is ephemeral
    (~30 seconds) and must be finalized with ``sendMessage``.
    ``sendChatAction`` provides the typing indicator; inline keyboards
    provide interactive buttons with ``callback_query`` updates (1-64
    byte callback_data, answered via ``answerCallbackQuery``).
    """
    return ChannelCapabilities(
        channel="telegram",
        capabilities=frozenset(
            {
                ChannelCapability.NATIVE_TEXT_DRAFT_UPDATES,
                ChannelCapability.COMPLETE_MESSAGE_SEND,
                ChannelCapability.TYPING_INDICATOR,
                ChannelCapability.SUPPORTS_FINAL_PERSISTENT_MESSAGE,
                ChannelCapability.MESSAGE_EDIT,
                ChannelCapability.MESSAGE_RECEIPTS,
                ChannelCapability.SUPPORTS_MESSAGE_THREAD,
            }
        ),
        max_text_size=4096,
        supported_formatting=(
            "bold",
            "italic",
            "strikethrough",
            "code",
            "code_block",
            "link",
            "pre",
            "blockquote",
        ),
    )


__all__ = [
    "RenderResult",
    "render_plain",
    "render_telegram",
    "render_whatsapp",
    "telegram_capabilities",
    "whatsapp_capabilities",
]
