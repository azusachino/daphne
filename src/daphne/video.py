"""One pasted link becomes a Video: identity, metadata, price, and the delivery decision.

matcha owns every question that has exactly one right answer: which work a paste names
(`WorkRef`), the only URL worth printing or downloading (`matcha.link.canonical_url`), how
Bilibili's payloads read (`matcha.platforms.bilibili`), and whether a resolved link
becomes an upload or a card (`matcha.deliver.decide` over a `Policy` and `Capabilities`).

This module is the I/O and product half. It runs the metadata chain -- Bilibili's native
`view` first, yt-dlp second -- prices the download before any byte moves, and holds
daphne's single policy value. `None` always means "the source did not publish it"; no
renderer may turn that into a claim about the work.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from matcha import (
    Capabilities,
    Delivery,
    MediaInfo,
    Policy,
    Reason,
    StreamPlan,
    WorkRef,
    canonical_url,
    decide,
    parse,
    strip_tracking,
)
from matcha.errors import NeedsExpansionError

from daphne import bilibili, downloader

logger = logging.getLogger(__name__)

# daphne converts in chat: a file that fits may be uploaded directly, and the card always
# shows the canonical source. These are product choices, so they are values, not code.
POLICY = Policy(auto_preview=True)


class Unreadable(Exception):
    """No engine could read this link. ``reason`` comes from the shared vocabulary."""

    def __init__(self, reason: Reason):
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True)
class Video:
    """A resolved link: what it is, what the source published, what fetching it costs.

    ``plan`` is the priced fetch. For Bilibili it carries the actual DASH streams; for a
    yt-dlp read it carries only the price, because yt-dlp chooses its own formats.

    ``canonical_url`` is the only URL that may be printed. ``media_url`` exists for the
    handlers that resolve a media file themselves, where the file lives somewhere other
    than the post (Bluesky's CDN): it is fetched and never printed.
    """

    ref: Optional[WorkRef]
    canonical_url: str
    info: MediaInfo
    plan: Optional[StreamPlan] = None
    engine: str = ""
    media_url: str = ""

    @property
    def platform(self) -> str:
        return self.info.platform or "video"

    @property
    def expected_duration(self) -> Optional[float]:
        return self.info.duration_s

    @property
    def estimated_bytes(self) -> Optional[int]:
        return None if self.plan is None else self.plan.estimated_bytes

    @property
    def fetch_url(self) -> str:
        """The URL a downloader is given: the canonical one unless a source file is known."""
        return self.media_url or self.canonical_url

    def delivery(self, caps: Capabilities) -> Delivery:
        """Upload or card, decided by policy and capability, never by convenience."""
        return decide(self.info, self.plan, caps, POLICY)


def video_ref(url: str) -> Optional[WorkRef]:
    """The WorkRef a link names, or ``None`` when matcha has no grammar for it."""
    try:
        return parse(url)
    except ValueError:
        return None


def canonical_link(url: str) -> str:
    """The one URL this link may be printed or downloaded as.

    Identity and the canonical form come from matcha. A b23.tv share link is opaque, so
    it keeps a tracking-free form here and `resolve` expands it in a worker: no handler
    performs a request while formatting a message. A link matcha has no grammar for keeps
    a tracking-free form too, because printing the paste would echo its parameters.
    """
    ref = video_ref(url)
    if ref is not None:
        if ref.kind == "short_link":
            return strip_tracking(url)
        try:
            return canonical_url(ref)
        except NeedsExpansionError, ValueError:
            pass
    if downloader.is_bilibili_url(url) or downloader.is_youtube_url(url):
        # matcha has no grammar for this paste (an id outside the spec), but the host is
        # still worth normalising, and a part selection is worth keeping.
        return downloader.sanitize_video_url(url)
    return strip_tracking(url)


def resolve(url: str) -> Video:
    """Read a link through the metadata chain, then price the download.

    Raises `Unreadable` when no engine could read the link, and
    `bilibili.RateLimitedError` when risk control refused every attempt: the other engines
    reach the same host, so trying them would only add to the limit.
    """
    ref = video_ref(url)
    if ref is not None and ref.platform == "bilibili":
        return _resolve_bilibili(url)
    return _resolve_with_ytdlp(url)


def resolve_or_blank(url: str, *, platform: str) -> Video:
    """Resolve a page URL, or describe it as blank when no engine can read it.

    A platform handler reaches this after its own extractor gave up: the metadata chain
    still gets a chance to describe the link, and a link nothing can describe still
    downloads (with no title, uploader or size) instead of failing outright.
    """
    try:
        return resolve(url)
    except (Unreadable, bilibili.RateLimitedError) as exc:
        logger.info("Resolving %s as a blank video: %s", platform, exc)
        return from_metadata(url, {}, platform=platform)


def from_metadata(
    url: str, metadata: dict, *, platform: str, source_url: Optional[str] = None
) -> Video:
    """Wrap a handler's own reading in the one Video shape the bot renders.

    Bluesky, TikTok and Instagram read their posts themselves and know the title, uploader
    and size. This puts that reading in the same shape the upload path, the card and the
    caption already understand, so none of those forks per platform. The URL that gets
    printed is the post; ``url`` is fetched only when it is not that post.
    """
    printable = canonical_link(source_url or str(metadata.get("webpage_url") or url))
    ref = video_ref(printable)
    width, height = metadata.get("width"), metadata.get("height")
    info = MediaInfo(
        platform=platform,
        kind=ref.kind if ref else "video",
        work_id=ref.work_id if ref else "",
        title=str(metadata.get("title") or ""),
        part=ref.part if ref else None,
        creator=_text(metadata.get("uploader")),
        duration_s=_seconds(metadata.get("duration")),
        width=width if isinstance(width, int) else None,
        height=height if isinstance(height, int) else None,
        thumbnail_url=_http_url(metadata.get("thumbnail")),
        canonical_url=printable,
    )
    size = _size(metadata)
    plan = (
        None
        if size is None
        else StreamPlan(
            video=None,
            audio=None,
            estimated_bytes=size,
            quality="",
            needs_merge=False,
        )
    )
    # Compared raw, not by tracking-free form: a signed CDN URL differs from the post
    # only in its query, and dropping that query would fetch the wrong thing (or a 403).
    return Video(
        ref=ref,
        canonical_url=printable,
        info=info,
        plan=plan,
        engine="platform",
        media_url="" if url == printable else url,
    )


def _resolve_bilibili(url: str) -> Video:
    """Bilibili's own view endpoint first: it carries owner, cover and the parts."""
    reason: Optional[Reason] = None
    try:
        media = bilibili.resolve_media(url)
        return Video(
            ref=media.ref,
            canonical_url=media.canonical_url,
            info=media.info,
            plan=media.plan,
            engine="bilibili-view",
        )
    except bilibili.RateLimitedError:
        raise
    except Exception as exc:
        reason = bilibili.reason_for(exc)
        logger.warning(
            "Bilibili view failed, trying yt-dlp: %s (%s)",
            bilibili.describe_error(exc),
            reason.value,
        )
    try:
        return _resolve_with_ytdlp(url)
    except Unreadable:
        # yt-dlp's generic failure says nothing specific; the platform's reason does.
        raise Unreadable(reason or Reason.EXTRACTOR_FAILED) from None


def _resolve_with_ytdlp(url: str) -> Video:
    """The fallback engine. It reports what it read; matcha still owns the identity."""
    data = downloader.fetch_ytdlp_metadata(url)
    if not data:
        raise Unreadable(Reason.EXTRACTOR_FAILED)
    ref = video_ref(url)
    if ref is None and data.get("webpage_url"):
        ref = video_ref(str(data["webpage_url"]))
    if ref is not None:
        try:
            canonical = canonical_url(ref)
        except NeedsExpansionError, ValueError:
            canonical = downloader.sanitize_video_url(url)
    else:
        canonical = downloader.sanitize_video_url(str(data.get("webpage_url") or url))
    width, height = data.get("width"), data.get("height")
    info = MediaInfo(
        platform=ref.platform if ref else "video",
        kind=ref.kind if ref else "video",
        work_id=ref.work_id if ref else str(data.get("id") or ""),
        title=str(data.get("title") or ""),
        part=ref.part if ref else None,
        creator=_text(data.get("uploader")),
        duration_s=_seconds(data.get("duration")),
        width=width if isinstance(width, int) else None,
        height=height if isinstance(height, int) else None,
        thumbnail_url=_http_url(data.get("thumbnail")),
        canonical_url=canonical,
    )
    size = _size(data)
    plan = (
        None
        if size is None
        else StreamPlan(
            video=None,
            audio=None,
            estimated_bytes=size,
            quality="",
            needs_merge=False,
        )
    )
    return Video(
        ref=ref, canonical_url=canonical, info=info, plan=plan, engine="yt-dlp"
    )


def _text(value: object) -> Optional[str]:
    text = str(value).strip() if value else ""
    return text or None


def _seconds(value: object) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except TypeError, ValueError:
        return None


def _http_url(value: object) -> Optional[str]:
    if isinstance(value, str) and value.startswith("http"):
        return value
    return None


def _size(data: dict) -> Optional[int]:
    """The price a source reported, in bytes, when it reported one."""
    for key in ("filesize", "filesize_approx", "size"):
        value = data.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except TypeError, ValueError:
            continue
    return None
