import html
from importlib.metadata import PackageNotFoundError, version
from typing import Optional


PARSE_MODE_HTML = "HTML"


def escape_html(value: object) -> str:
    return html.escape(str(value), quote=True)


def bot_version() -> str:
    try:
        return version("daphne")
    except PackageNotFoundError:
        return "0.1.0"


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
    footer = [f"daphne {bot_version()}"]
    if sender:
        footer.append(sender)
    lines.append("")
    lines.append(" · ".join(escape_html(part) for part in footer))
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
        if normalized:
            self.blocks.append(" ".join(escape_html(tag) for tag in normalized))
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
