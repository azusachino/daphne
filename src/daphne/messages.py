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
