"""Bilibili downloads without yt-dlp.

yt-dlp's Bilibili extractor fails on most videos right now, so this module asks the
endpoints the web player uses: ``view`` for metadata and ``playurl`` with
``fnval=4048`` for DASH streams. It downloads the video and audio tracks and merges
them with ffmpeg. Requests are anonymous, so Bilibili caps quality at 480p, and it
sometimes answers HTTP 412 (risk control). Those calls back off and retry.
"""

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from daphne.tg import failure_reason

logger = logging.getLogger(__name__)

API_BASE = "https://api.bilibili.com"
REFERER = "https://www.bilibili.com/"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30.0
# 412 is intermittent risk control, so wait between attempts instead of retrying at once.
RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)
RISK_CONTROL_STATUSES = {412, 429}
RISK_CONTROL_CODES = {-412, -799}
# Ask for the best quality; anonymous responses stop at 480p on their own.
MAX_QN = 127
# fnval=4048 asks for DASH with every format, the same flag yt-dlp and the web player use.
FNVAL_DASH = 4048
AVC_CODEC_ID = 7
REDIRECT_CODES = {301, 302, 303, 307, 308}
BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
AV_RE = re.compile(r"/av(\d+)", re.IGNORECASE)


class BilibiliError(RuntimeError):
    """A Bilibili step failed. The message has no URLs or secrets, so it is safe to show."""


def describe_error(exc: BaseException) -> str:
    """Our own messages are curated; anything else goes through the URL-free reason."""
    if isinstance(exc, BilibiliError):
        return str(exc)
    return failure_reason(exc)


def _headers() -> dict[str, str]:
    return {"User-Agent": USER_AGENT, "Referer": REFERER}


@contextmanager
def _open(client: Optional[httpx.Client]) -> Iterator[httpx.Client]:
    if client is not None:
        yield client
        return
    with httpx.Client(follow_redirects=False, timeout=REQUEST_TIMEOUT) as owned:
        yield owned


def _expand_short_link(client: httpx.Client, url: str) -> str:
    """Follow b23.tv share links by hand so the final bilibili.com URL is known."""
    for _ in range(3):
        if not (urlsplit(url).hostname or "").endswith("b23.tv"):
            return url
        resp = client.get(url, headers=_headers())
        location = resp.headers.get("location")
        if resp.status_code not in REDIRECT_CODES or not location:
            raise BilibiliError("Could not expand the b23.tv link")
        url = urljoin(url, location)
    raise BilibiliError("b23.tv link redirected too many times")


def video_ref(url: str) -> tuple[dict[str, str], int]:
    """Return the view-API identifier and the 1-based part number for a video URL."""
    parts = urlsplit(url)
    if "/bangumi/" in parts.path:
        raise BilibiliError(
            "Bilibili episodes and paid content are not handled natively"
        )
    page_values = parse_qs(parts.query).get("p", ["1"])
    page = int(page_values[0]) if page_values[0].isdigit() else 1
    bv = BV_RE.search(parts.path)
    if bv:
        return {"bvid": bv.group(0)}, page
    av = AV_RE.search(parts.path)
    if av:
        return {"aid": av.group(1)}, page
    raise BilibiliError("Not a Bilibili video link")


def _api(client: httpx.Client, path: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET a Bilibili API, backing off on risk control. Returns the ``data`` object."""
    for delay in (*RETRY_DELAYS, None):
        resp = client.get(f"{API_BASE}{path}", params=params, headers=_headers())
        if resp.status_code in RISK_CONTROL_STATUSES:
            reason = f"HTTP {resp.status_code}"
        else:
            resp.raise_for_status()
            body = resp.json()
            code = body.get("code")
            if code == 0:
                return body.get("data") or {}
            if code not in RISK_CONTROL_CODES:
                raise BilibiliError(f"Bilibili API error {code}: {body.get('message')}")
            reason = f"API code {code}"
        if delay is None:
            break
        logger.info(
            "Bilibili %s hit risk control (%s); retrying in %.0fs", path, reason, delay
        )
        time.sleep(delay)
    raise BilibiliError("Bilibili kept rate-limiting this video; try again later")


def _warm_session(client: httpx.Client, ref: dict[str, str]) -> None:
    """Load the watch page first, as a browser does, so Bilibili sets its visitor cookies."""
    slug = ref["bvid"] if "bvid" in ref else f"av{ref['aid']}"
    try:
        client.get(f"https://www.bilibili.com/video/{slug}/", headers=_headers())
    except httpx.HTTPError as exc:
        logger.info("Bilibili watch page visit failed: %s", failure_reason(exc))


def _view(client: httpx.Client, ref: dict[str, str]) -> dict[str, Any]:
    return _api(client, "/x/web-interface/view", ref)


def _page(view: dict[str, Any], page: int) -> dict[str, Any]:
    pages = view.get("pages") or []
    if not pages:
        raise BilibiliError("Bilibili returned no parts for this video")
    if not 1 <= page <= len(pages):
        raise BilibiliError(f"This video has {len(pages)} parts, not {page}")
    return pages[page - 1]


def _playurl(client: httpx.Client, bvid: str, cid: int) -> dict[str, Any]:
    return _api(
        client,
        "/x/player/playurl",
        {
            "bvid": bvid,
            "cid": cid,
            "qn": MAX_QN,
            "fnval": FNVAL_DASH,
            "fnver": 0,
            "fourk": 1,
        },
    )


def fetch_bilibili_metadata(
    url: str, client: Optional[httpx.Client] = None
) -> dict[str, Any]:
    """Metadata in the same shape as the yt-dlp path returns."""
    with _open(client) as open_client:
        ref, page = video_ref(_expand_short_link(open_client, url))
        _warm_session(open_client, ref)
        view = _view(open_client, ref)
        part = _page(view, page)
    dimension = view.get("dimension") or {}
    width, height = dimension.get("width"), dimension.get("height")
    if dimension.get("rotate") == 1:
        width, height = height, width
    return {
        "title": view.get("title", ""),
        "uploader": (view.get("owner") or {}).get("name", ""),
        "duration": part.get("duration") or view.get("duration"),
        "thumbnail": view.get("pic"),
        "webpage_url": f"https://www.bilibili.com/video/{view.get('bvid', '')}",
        "width": width,
        "height": height,
        "filesize": None,
        "filesize_approx": None,
        "url": None,
    }


def pick_video_stream(dash: dict[str, Any]) -> dict[str, Any]:
    streams = dash.get("video") or []
    if not streams:
        raise BilibiliError("Bilibili returned no video streams")
    # H.264 first: it plays everywhere Telegram clients do. Other codecs remain a fallback.
    avc = [stream for stream in streams if stream.get("codecid") == AVC_CODEC_ID]
    return max(avc or streams, key=lambda s: (s.get("id", 0), s.get("bandwidth", 0)))


def pick_audio_stream(dash: dict[str, Any]) -> dict[str, Any]:
    streams = dash.get("audio") or []
    if not streams:
        raise BilibiliError("Bilibili returned no audio track")
    return max(streams, key=lambda s: s.get("bandwidth", 0))


def _stream_urls(stream: dict[str, Any]) -> list[str]:
    base = stream.get("baseUrl") or stream.get("base_url")
    backups = stream.get("backupUrl") or stream.get("backup_url") or []
    return [url for url in [base, *backups] if url]


def _download_stream(client: httpx.Client, stream: dict[str, Any], dest: str) -> None:
    last_error: Optional[BaseException] = None
    for url in _stream_urls(stream):
        try:
            # Signed CDN URLs are never logged; only the reason for a failed host is.
            with client.stream("GET", url, headers=_headers()) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as out:
                    for chunk in resp.iter_bytes():
                        out.write(chunk)
            if os.path.getsize(dest) > 0:
                return
        except httpx.HTTPError as exc:
            last_error = exc
            logger.info("Bilibili media host failed: %s", failure_reason(exc))
    if last_error is not None:
        raise BilibiliError(
            f"Bilibili media download failed ({failure_reason(last_error)})"
        )
    raise BilibiliError("Bilibili returned no media URL for a stream")


def _ffmpeg(args: list[str]) -> None:
    res = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", *args],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if res.returncode != 0:
        lines = (res.stderr or "").strip().splitlines()
        raise BilibiliError(f"ffmpeg failed: {lines[-1] if lines else res.returncode}")


def _fetch_tracks(
    url: str, work: str, client: Optional[httpx.Client], want_video: bool
) -> tuple[str, str, str]:
    """Download the chosen streams into ``work``. Returns (bvid, video path, audio path)."""
    with _open(client) as open_client:
        ref, page = video_ref(_expand_short_link(open_client, url))
        _warm_session(open_client, ref)
        view = _view(open_client, ref)
        part = _page(view, page)
        play = _playurl(open_client, view["bvid"], part["cid"])
        dash = play.get("dash") or {}
        if not dash:
            raise BilibiliError("Bilibili returned no DASH streams for this video")
        audio = pick_audio_stream(dash)
        video_path = os.path.join(work, "video.m4s")
        audio_path = os.path.join(work, "audio.m4s")
        if want_video:
            _download_stream(open_client, pick_video_stream(dash), video_path)
        _download_stream(open_client, audio, audio_path)
    return view["bvid"], video_path, audio_path


def download_bilibili(
    url: str, out_dir: str, client: Optional[httpx.Client] = None
) -> str:
    """Download a Bilibili video as one mp4 in ``out_dir`` and return its path."""
    os.makedirs(out_dir, exist_ok=True)
    work = tempfile.mkdtemp(prefix=".bilibili-", dir=out_dir)
    try:
        bvid, video_path, audio_path = _fetch_tracks(url, work, client, want_video=True)
        out_path = os.path.join(out_dir, f"{bvid}.mp4")
        _ffmpeg(
            [
                "-i",
                video_path,
                "-i",
                audio_path,
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                out_path,
            ]
        )
        return out_path
    finally:
        shutil.rmtree(work, ignore_errors=True)


def download_bilibili_audio(
    url: str, out_dir: str, client: Optional[httpx.Client] = None
) -> str:
    """Download only the audio track of a Bilibili video as an m4a file."""
    os.makedirs(out_dir, exist_ok=True)
    work = tempfile.mkdtemp(prefix=".bilibili-", dir=out_dir)
    try:
        bvid, _, audio_path = _fetch_tracks(url, work, client, want_video=False)
        out_path = os.path.join(out_dir, f"{bvid}.m4a")
        _ffmpeg(["-i", audio_path, "-vn", "-c:a", "copy", out_path])
        return out_path
    finally:
        shutil.rmtree(work, ignore_errors=True)
