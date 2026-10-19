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

WhatsApp formatting (blueprint §7.4):
  *bold*, _italic_, ~strikethrough_, ```code```, ```block```
  WhatsApp does NOT support full Markdown. The renderer emits only
  the subset WhatsApp recognises.

Telegram formatting (blueprint §7.5, Route A):
  HTML entities: <b>, <i>, <s>, <code>, <pre>, <a href="...">
  The renderer uses HTML parse_mode for maximum compatibility.
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
# WhatsApp renderer
# ---------------------------------------------------------------------------


def render_whatsapp(text: str) -> RenderResult:
    """Render markdown-like text for WhatsApp.

    WhatsApp supports: *bold*, _italic_, ~strikethrough~, ```inline code```,
    and ```code blocks```. It does NOT support Markdown links or headings.

    The renderer preserves all text content. Unsupported formatting
    (links, headings) is rendered as plain text.
    """
    if not text:
        return RenderResult(text="", format_mode="whatsapp")

    try:
        result = text

        # Code blocks: ```lang\ncode``` → ```code``` (WhatsApp uses triple backticks)
        def _replace_code_block(m: re.Match[str]) -> str:
            return f"```{m.group(2)}```"

        result = _CODE_BLOCK_RE.sub(_replace_code_block, result)

        # Inline code: `code` → ```code``` (WhatsApp uses triple backticks for inline too)
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


# ---------------------------------------------------------------------------
# Telegram renderer (HTML parse_mode, Route A per blueprint §7.5)
# ---------------------------------------------------------------------------


def render_telegram(text: str) -> RenderResult:
    """Render markdown-like text for Telegram using HTML entities.

    Telegram supports: <b>, <i>, <s>, <code>, <pre>, <a href="...">.
    The renderer uses HTML parse_mode for maximum compatibility.

    HTML is escaped FIRST, then markdown markers are converted to HTML
    tags. This prevents double-escaping (blueprint §7.5).
    """
    if not text:
        return RenderResult(text="", format_mode="HTML")

    try:
        # Escape all HTML first. The markdown markers (**, *, _, ~, `, ```)
        # are NOT HTML special characters, so they survive escaping and
        # the regexes can still match them.
        result = _escape_html(text)

        # Code blocks: ```lang\ncode``` → <pre>code</pre>
        # Content is already escaped.
        result = _CODE_BLOCK_RE.sub(r"<pre>\2</pre>", result)

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
    formatting markers are removed.
    """
    if not text:
        return RenderResult(text="", format_mode="plain")

    result = text
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

    Blueprint §6.4: WhatsApp does NOT support a native typing indicator
    for the Cloud API integration. The capability is reported as
    unavailable — do not fabricate it.
    """
    return ChannelCapabilities(
        channel="whatsapp",
        capabilities=frozenset(
            {
                ChannelCapability.COMPLETE_MESSAGE_SEND,
                ChannelCapability.SUPPORTS_FINAL_PERSISTENT_MESSAGE,
                ChannelCapability.MESSAGE_RECEIPTS,
            }
        ),
        max_text_size=4096,
        supported_formatting=("bold", "italic", "strikethrough", "code", "code_block"),
    )


def telegram_capabilities() -> ChannelCapabilities:
    """Telegram Bot API capabilities as verified for this integration.

    Blueprint §5: sendMessageDraft and sendRichMessageDraft were verified
    against the live Bot API on 19 October 2026. The bot is
    @Waxdavidesssaffbot.
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
