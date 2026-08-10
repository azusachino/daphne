import html
import re
import unicodedata
from importlib.metadata import PackageNotFoundError, version
from typing import Optional


PARSE_MODE_HTML = "HTML"

# Pixiv artworks especially can carry a dozen+ tags; past this count, collapse
# them into a tap-to-expand blockquote instead of a wall of hashtags.
TAGS_EXPANDABLE_THRESHOLD = 6

_UNKNOWN_AUTHORS = {"", "unknown"}


def escape_html(value: object) -> str:
    return html.escape(str(value), quote=True)


def slugify_tag(value: object) -> str:
    """
    Normalizes an arbitrary author/uploader name into a single hashtag-safe
    slug: Unicode-normalized (NFKC, so full-width/half-width and combining
    variants of the same name collapse together), non-word runs (spaces,
    punctuation, emoji, "#" itself) folded to a single underscore, and
    leading/trailing underscores trimmed. `\\w` matches Unicode letters, so
    non-Latin names (Japanese, Chinese, etc.) are preserved rather than
    stripped down to nothing. Returns "" for input with no word characters
    at all (e.g. an author name that's pure emoji).
    """
    text = unicodedata.normalize("NFKC", str(value))
    return re.sub(r"\W+", "_", text).strip("_").lower()


def author_tag(value: object) -> Optional[str]:
    """
    Returns a hashtag-safe slug for an author/uploader name, or None when
    there's no usable author (missing, empty, the "unknown" placeholder
    fallback name, or a name with no word characters to slugify).
    """
    if not value or str(value).strip().lower() in _UNKNOWN_AUTHORS:
        return None
    slug = slugify_tag(value)
    return slug or None


def bot_version() -> str:
    try:
        return version("daphne")
    except PackageNotFoundError:
        return "0.3.4"


def sender_attribution(user) -> Optional[str]:
    if not user:
        return None
    if getattr(user, "username", None):
        return f"via @{user.username}"
    full_name = getattr(user, "full_name", None)
    if full_name:
        return f"via {full_name}"
    return None


def append_footer(body: str, sender: Optional[str] = None) -> str:
    lines = [body.rstrip()]
    parts = [f"<code>daphne v{bot_version()}</code>"]
    if sender:
        parts.append(f"<i>{escape_html(sender)}</i>")
    lines.append("")
    lines.append(" │ ".join(parts))
    return "\n".join(lines)


class HtmlMessage:
    def __init__(self, sender: Optional[str] = None):
        self.blocks: list[str] = []
        self.sender = sender

    def title(self, value: object) -> "HtmlMessage":
        if value is not None and value != "":
            self.blocks.append(f"<b>{escape_html(value)}</b>")
        return self

    def text(self, value: object) -> "HtmlMessage":
        if value is not None and value != "":
            self.blocks.append(escape_html(value))
        return self

    def fields(self, *items: tuple[str, object]) -> "HtmlMessage":
        lines = []
        for label, value in items:
            if value is None or value == "":
                continue
            clean_label = str(label).rstrip(":")
            lines.append(f"<b>{escape_html(clean_label)}:</b> {escape_html(value)}")
        if lines:
            self.blocks.append("\n".join(lines))
        return self

    def raw(self, html_value: str) -> "HtmlMessage":
        """Appends a block that is already-safe HTML (pieces pre-escaped by the
        caller), bypassing text()'s whole-string escaping."""
        if html_value:
            self.blocks.append(html_value)
        return self

    def link(self, url: str, label: Optional[str] = None) -> "HtmlMessage":
        if not url:
            return self
        escaped_url = escape_html(url)
        escaped_label = escape_html(label or url)
        self.blocks.append(f'<a href="{escaped_url}">{escaped_label}</a>')
        return self

    def links(self, *urls: str) -> "HtmlMessage":
        lines = []
        for url in urls:
            if not url:
                continue
            escaped_url = escape_html(url)
            lines.append(f'<a href="{escaped_url}">{escaped_url}</a>')
        if lines:
            self.blocks.append("\n".join(lines))
        return self

    def tags(self, *tags: str) -> "HtmlMessage":
        normalized = []
        for tag in tags:
            if not tag:
                continue
            normalized.append(tag if tag.startswith("#") else f"#{tag}")
        if not normalized:
            return self
        line = " ".join(escape_html(tag) for tag in normalized)
        if len(normalized) > TAGS_EXPANDABLE_THRESHOLD:
            line = f"<blockquote expandable>{line}</blockquote>"
        self.blocks.append(line)
        return self

    def render(self) -> str:
        return append_footer("\n\n".join(self.blocks), self.sender)


def render_html_message(
    *,
    title: Optional[str] = None,
    text: Optional[str] = None,
    fields: Optional[list[tuple[str, object]]] = None,
    links: Optional[list[str]] = None,
    tags: Optional[list[str]] = None,
    sender: Optional[str] = None,
) -> str:
    msg = HtmlMessage(sender=sender).title(title).text(text)
    if fields:
        msg.fields(*fields)
    if links:
        msg.links(*links)
    if tags:
        msg.tags(*tags)
    return msg.render()
