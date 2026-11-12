"""Bilibili over matcha: matcha describes the request, daphne performs it.

`matcha.platforms.bilibili` owns the grammar (``WorkRef`` and the one canonical URL), the
request descriptions (`view`, `playurl`, `finger/spi`, `GenWebTicket`, b23 expansion), the
payload parsers and the error vocabulary. This module is the I/O half: it runs those
requests with httpx, keeps the visitor session alive, backs off when Bilibili applies risk
control, muxes the two DASH tracks with ffmpeg and keeps the short-lived mp4 cache.

`x/web-interface/view` is the metadata source (owner, cover, total duration, the parts and
their `cid`); `x/player/playurl` with `fnval=4048` is the download plan, so a size is
priced before any byte moves. Both answer a request that carries the visitor cookies from
`finger/spi` plus a `bili_ticket`; it is the cookieless request that gets 412 (measured).
Anonymous quality still caps at 480p.

Three things keep the request volume low:
- a visitor session, reused for 30 minutes and refreshed once when Bilibili answers risk
  control;
- an on-disk cache of finished mp4 files, keyed by `matcha.link.cache_key`, so a repeated
  link is served without any API call;
- a time limit and a size cap on every stream, so one download cannot hold a worker or
  the disk indefinitely.
"""

import contextlib
import logging
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Iterator, Optional
from urllib.parse import urljoin, urlsplit

import httpx

from matcha import ApiError, Reason, Request, Session, Stream, StreamPlan, WorkRef
from matcha import bilibili as api
from matcha.errors import MatchaError, NeedsExpansionError
from matcha.link import cache_key, canonical_url as _canonical_url
from matcha.link import parse as _parse
from matcha.link import with_expanded
from matcha.types import MediaInfo

from daphne.tg import failure_reason

logger = logging.getLogger(__name__)

RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)
RISK_CONTROL_STATUSES = {412, 429}
RISK_CONTROL_CODES = {-412, -799}
REDIRECT_CODES = {301, 302, 303, 307, 308}
REQUEST_TIMEOUT = 30.0
# Ask for the best quality; anonymous responses stop at 480p on their own.
MAX_QN = 127
SESSION_TTL = 30 * 60
CACHE_TTL = 6 * 60 * 60
CACHE_MAX_BYTES = 2 * 1024**3
STALE_PARTIAL_SECONDS = 60 * 60
# One stream may take at most this long and is never allowed past the upload limit.
STREAM_TIMEOUT_SECONDS = 300.0
MAX_STREAM_BYTES = 2 * 1024**3
# Per user, so another local user cannot plant files in the cache.
CACHE_DIR = os.path.join(tempfile.gettempdir(), f"daphne-bilibili-{os.getuid()}")

_state_lock = threading.Lock()
_session: dict[str, Any] = {"cookies": None, "at": 0.0}


class BilibiliError(RuntimeError):
    """A Bilibili step failed. The message has no URLs or secrets, so it is safe to show."""


class RateLimitedError(BilibiliError):
    """Risk control kept refusing the requests after every retry."""


class UnsupportedError(BilibiliError):
    """A Bilibili work the native path refuses: bangumi episodes and paid content."""


class PartOutOfRangeError(BilibiliError):
    """A ``?p=`` the body does not publish. The message names the real part count.

    It is its own type because the message *is* the instruction, so the caller that shows
    failures to the user has to let it through instead of replacing it with a generic
    reason or retrying the link with the next engine.
    """


@dataclass(frozen=True)
class Media:
    """What one link resolved to: identity, what the source published, and its price."""

    ref: WorkRef
    canonical_url: str
    info: MediaInfo
    # ``None`` means the plan could not be priced; the caller shows a card instead.
    plan: Optional[StreamPlan]


def describe_error(exc: BaseException) -> str:
    """Our own messages are curated; anything else goes through the URL-free reason."""
    if isinstance(exc, BilibiliError):
        return str(exc)
    return failure_reason(exc)


def reason_for(exc: BaseException) -> Reason:
    """The shared reason vocabulary for a failure, so the bot never invents wording.

    Platform codes go through matcha; our own failures carry the closest shared reason;
    anything left is a transport problem.
    """
    if isinstance(exc, RateLimitedError):
        return Reason.RATE_LIMITED
    if isinstance(exc, UnsupportedError):
        return Reason.AUTH_REQUIRED
    if isinstance(exc, ApiError):
        # The platform's own code decides, exactly as the parser reported it.
        return api.describe_error(exc)
    if isinstance(exc, MatchaError):
        # A link matcha itself cannot canonicalise is unreadable, not a network problem.
        return Reason.EXTRACTOR_FAILED
    if isinstance(exc, BilibiliError):
        return Reason.EXTRACTOR_FAILED
    return api.describe_error(exc)


@contextmanager
def _open(client: Optional[httpx.Client]) -> Iterator[httpx.Client]:
    if client is not None:
        yield client
        return
    with httpx.Client(follow_redirects=False, timeout=REQUEST_TIMEOUT) as owned:
        yield owned


def _with_session(request: Request) -> Request:
    """Attach the cookies of the current visitor session to a request description."""
    cookies = dict(_session["cookies"] or {})
    if not cookies:
        return request
    return replace(request, cookies={**cookies, **dict(request.cookies)})


def _send(client: httpx.Client, request: Request) -> httpx.Response:
    """Perform one request description, with its cookies scoped to bilibili.com."""
    headers = dict(request.headers)
    # httpx deprecates per-request cookies, so the description's cookies go into the
    # client's jar, scoped to the site they came from (never the media CDN).
    for name, value in request.cookies.items():
        client.cookies.set(name, value, domain=".bilibili.com", path="/")
    if request.method == "POST":
        return client.post(request.url, headers=headers)
    return client.get(
        request.url,
        params=dict(request.params) or None,
        headers=headers,
    )


def _perform(
    client: httpx.Client, request: Request, *, session: bool = False
) -> dict[str, Any]:
    """Perform a request description and decode its body, backing off on risk control.

    A non-zero platform code is *not* an error here: the matcha parser that reads the body
    owns that judgement, and raises `ApiError` with the platform's own code.
    """
    refreshed = False
    for delay in (*RETRY_DELAYS, None):
        current = _with_session(request) if session else request
        resp = _send(client, current)
        if resp.status_code in RISK_CONTROL_STATUSES:
            reason = f"HTTP {resp.status_code}"
        else:
            resp.raise_for_status()
            body = resp.json()
            if body.get("code") not in RISK_CONTROL_CODES:
                return body
            reason = f"API code {body.get('code')}"
        if delay is None:
            break
        if session and not refreshed:
            # Risk control can be tied to the visitor session, so start a fresh one once.
            refreshed = True
            _ensure_visitor_session(client, force=True)
        logger.info(
            "Bilibili %s hit risk control (%s); retrying in %.0fs",
            urlsplit(request.url).path,
            reason,
            delay,
        )
        time.sleep(delay)
    raise RateLimitedError("Bilibili kept rate-limiting this video; try again later")


def _visitor_cookies(client: httpx.Client) -> dict[str, str]:
    """Visitor cookies from `finger/spi`, plus the ticket when Bilibili issues one."""
    spi = _perform(client, api.spi_request())
    ticket = None
    try:
        ticket = _perform(client, api.ticket_request(int(time.time())))
    except (httpx.HTTPError, ValueError, RateLimitedError) as exc:
        # The ticket is optional; the spi cookies alone often answer for view and playurl.
        logger.info("Bilibili ticket request failed: %s", describe_error(exc))
    return api.visitor_session(spi, ticket)


def _ensure_visitor_session(client: httpx.Client, force: bool = False) -> None:
    """Cache the visitor cookies, reusing the shared session while it is fresh.

    The lock guards only the shared state. The network calls run outside it, so one slow
    setup does not hold up every other Bilibili request in the process.
    """
    with _state_lock:
        fresh = (
            _session["cookies"] is not None
            and time.monotonic() - _session["at"] < SESSION_TTL
        )
        cookies = None if force or not fresh else _session["cookies"]
    if cookies is None:
        try:
            cookies = _visitor_cookies(client)
        except (httpx.HTTPError, BilibiliError, KeyError, TypeError, ValueError) as exc:
            # Carry on without cookies; the API may still answer, and the next call retries.
            logger.info("Bilibili visitor setup failed: %s", describe_error(exc))
            cookies = None
        with _state_lock:
            _session.update(cookies=cookies, at=time.monotonic())


def _cookies() -> dict[str, str]:
    with _state_lock:
        return dict(_session["cookies"] or {})


def _expand_short_link(client: httpx.Client, ref: WorkRef) -> WorkRef:
    """Follow a b23.tv share link by hand until it names a bilibili.com work."""
    current = ref
    for _ in range(3):
        request = api.expand_request(current)
        resp = client.get(
            request.url, headers=dict(request.headers), follow_redirects=False
        )
        location = resp.headers.get("location")
        if resp.status_code not in REDIRECT_CODES or not location:
            raise BilibiliError("Could not expand the b23.tv link")
        target = urljoin(request.url, location)
        host = (urlsplit(target).hostname or "").lower()
        if host != "bilibili.com" and not host.endswith(".bilibili.com"):
            raise BilibiliError("b23.tv link did not resolve to bilibili.com")
        try:
            current = with_expanded(current, target)
        except (NeedsExpansionError, ValueError) as exc:
            raise BilibiliError("Could not expand the b23.tv link") from exc
        if current.kind != "short_link":
            return current
    raise BilibiliError("b23.tv link redirected too many times")


def _ref(client: Optional[httpx.Client], url: str) -> WorkRef:
    """The WorkRef a pasted or canonical link names. Only matcha parses identity."""
    ref = _parse(url)
    if ref is None or ref.platform != "bilibili":
        raise BilibiliError("Not a Bilibili video link")
    if ref.is_episode:
        raise UnsupportedError(
            "Bilibili episodes and paid content are not handled natively"
        )
    if ref.kind == "short_link":
        if client is None:
            with _open(None) as owned:
                return _expand_short_link(owned, ref)
        return _expand_short_link(client, ref)
    return ref


def _canonical(ref: WorkRef) -> str:
    try:
        return _canonical_url(ref)
    except (NeedsExpansionError, ValueError) as exc:
        raise BilibiliError("This Bilibili link has no canonical form") from exc


def _check_part(ref: WorkRef, info: MediaInfo) -> None:
    """Report an out-of-range ``?p=`` as the user's mistake, not as an unpublished cid.

    matcha answers a part the body does not publish with ``cid is None``, which reads the
    same as "the source published no cid". Only the part count can tell the two apart, so
    the count is what the user is told -- the one bounds check daphne still owns, and the
    one failure whose text is the instruction.
    """
    if ref.part is not None and info.parts and not 1 <= ref.part <= len(info.parts):
        raise PartOutOfRangeError(
            f"This video has {len(info.parts)} parts, not {ref.part}"
        )


def _part_cid(ref: WorkRef, info: MediaInfo) -> Optional[int]:
    """The cid worth pricing, or ``None`` when daphne must not price this selection.

    ``MediaInfo.cid`` falls back to the body's own top-level cid only when the body
    published no pages at all, and that number names part 1 whatever part was asked for
    (measured). A ``?p=`` on such a body therefore has no cid that can be trusted: pricing
    it would serve part 1 for a part-2 link, which is worse than an unpriced card, because
    daphne cannot claim a part count it never read.
    """
    if ref.part is not None and not info.parts:
        return None
    return info.cid


def _view(client: httpx.Client, ref: WorkRef) -> MediaInfo:
    """Read `x/web-interface/view`: metadata and the parts' cids, through matcha."""
    body = _perform(client, api.view_request(ref, Session(_cookies())), session=True)
    info = api.parse_view(body, ref, canonical=_canonical(ref))
    _check_part(ref, info)
    return info


def _plan(client: httpx.Client, ref: WorkRef, info: MediaInfo) -> StreamPlan:
    """Price the DASH download with `fnval=4048`, before any byte moves.

    The cid is authoritative and comes from ``MediaInfo.cid``: matcha's description sends
    it as a required parameter, so nothing here edits the request it returned.
    """
    cid = _part_cid(ref, info)
    if cid is None:
        raise BilibiliError("Bilibili returned no cid for this part")
    request = api.playurl_request(ref, Session(_cookies()), cid=cid, qn=MAX_QN)
    body = _perform(client, request, session=True)
    return api.parse_playurl(body, duration_s=info.duration_s)


def resolve_media(url: str, client: Optional[httpx.Client] = None) -> Media:
    """Identity, published metadata and a priced plan for one link.

    A plan that cannot be priced is not an error: the caller shows a card instead of
    uploading, which is exactly what `matcha.deliver.decide` does with ``plan=None``.
    """
    with _open(client) as open_client:
        ref = _ref(open_client, url)
        _ensure_visitor_session(open_client)
        info = _view(open_client, ref)
        plan = None
        if _part_cid(ref, info) is None:
            logger.info("Bilibili view carried no cid this link can trust; unpriced.")
        else:
            try:
                plan = _plan(open_client, ref, info)
            except (ApiError, BilibiliError, httpx.HTTPError) as exc:
                # A refused or unreachable plan is a card, not a failure.
                logger.info("Bilibili plan unavailable: %s", describe_error(exc))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                # A payload the parser could not read is a regression worth seeing.
                logger.warning(
                    "Bilibili plan payload could not be read: %s: %s",
                    type(exc).__name__,
                    exc,
                )
    return Media(ref=ref, canonical_url=_canonical(ref), info=info, plan=plan)


def canonical_url(url: str, client: Optional[httpx.Client] = None) -> str:
    """The ``www.bilibili.com/video/...`` link for a work, with ``?p=`` for later parts.

    Expands b23.tv short links. Canonical links need no request, so passing one again is
    free. This is a thin wrapper over `matcha.link.canonical_url`, the only URL producer.
    """
    return _canonical(_ref(client, url))


def _stream_to_file(
    client: httpx.Client, url: str, dest: str, headers: dict[str, str]
) -> None:
    """Write one CDN response to ``dest``, within the time limit and the size cap."""
    from daphne.doh import check_public_url

    deadline = time.monotonic() + STREAM_TIMEOUT_SECONDS
    written = 0
    current = url
    live_transport = isinstance(
        getattr(client, "_transport", None), httpx.HTTPTransport
    )
    for _ in range(4):
        parsed = urlsplit(current)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise BilibiliError("Invalid Bilibili media URL")
        if live_transport:
            try:
                check_public_url(current)
            except ValueError as exc:
                raise BilibiliError(str(exc)) from exc
        # Signed CDN URLs are never logged; only the reason for a failed host is.
        with client.stream(
            "GET", current, headers=headers, follow_redirects=False
        ) as resp:
            if resp.status_code in REDIRECT_CODES:
                location = resp.headers.get("location")
                if not location:
                    raise BilibiliError("Bilibili media redirect missing location")
                current = urljoin(current, location)
                continue
            resp.raise_for_status()
            with open(dest, "wb") as out:
                for chunk in resp.iter_bytes():
                    written += len(chunk)
                    if written > MAX_STREAM_BYTES:
                        raise BilibiliError(
                            "Bilibili stream is larger than the 2 GiB limit"
                        )
                    if time.monotonic() > deadline:
                        raise BilibiliError("Bilibili stream download timed out")
                    out.write(chunk)
            return
    raise BilibiliError("Bilibili stream redirected too many times")


def _download_stream(
    client: httpx.Client, stream: Stream, dest: str, headers: dict[str, str]
) -> None:
    last_error: Optional[BaseException] = None
    for url in (stream.url, *stream.backup_urls):
        if not url:
            continue
        try:
            _stream_to_file(client, url, dest, headers)
            if os.path.getsize(dest) > 0:
                return
        except (httpx.HTTPError, BilibiliError) as exc:
            last_error = exc
            logger.info("Bilibili media host failed: %s", describe_error(exc))
    if last_error is not None:
        raise BilibiliError(
            f"Bilibili media download failed ({describe_error(last_error)})"
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


def _download_plan(client: httpx.Client, ref: WorkRef) -> StreamPlan:
    """The plan a download needs: streams for both tracks, and the headers to ask for them.

    It is re-read here instead of being threaded down from the card: signed CDN URLs
    expire, and a queued download can start long after its price was quoted.
    """
    _ensure_visitor_session(client)
    info = _view(client, ref)
    plan = _plan(client, ref, info)
    if plan.video is None or plan.audio is None:
        raise BilibiliError("Bilibili returned no DASH streams for this video")
    return plan


def _from_cache(key: str, out_dir: str, name: str) -> Optional[str]:
    if not _cache_dir_ok():
        return None
    source = os.path.join(CACHE_DIR, f"{key}.mp4")
    try:
        if time.time() - os.path.getmtime(source) > CACHE_TTL:
            return None
        out_path = os.path.join(out_dir, f"{name}.mp4")
        shutil.copyfile(source, out_path)
    except OSError:
        return None
    return out_path


def _cache_dir_ok() -> bool:
    """The cache is used only when it is a real directory owned by this user."""
    try:
        os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
        st = os.lstat(CACHE_DIR)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()


def _to_cache(path: str, key: str) -> None:
    """Store a finished mp4 atomically: copy to a unique temp name, then rename.

    A file larger than the whole cap is not stored, since it would evict everything else.
    """
    if not _cache_dir_ok() or os.path.getsize(path) > CACHE_MAX_BYTES:
        return
    dest = os.path.join(CACHE_DIR, f"{key}.mp4")
    fd, partial = tempfile.mkstemp(dir=CACHE_DIR, suffix=".part")
    os.close(fd)
    try:
        shutil.copyfile(path, partial)
        os.replace(partial, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(partial)
        raise
    _evict_cache(dest)


def _evict_cache(keep: str) -> None:
    """Drop expired entries, stale partial files, then the oldest, until the cache fits.

    ``keep`` is the file that was just stored, so it is never evicted.
    """
    now = time.time()
    entries = []
    for name in os.listdir(CACHE_DIR):
        path = os.path.join(CACHE_DIR, name)
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        if name.endswith(".part"):
            if now - st.st_mtime > STALE_PARTIAL_SECONDS:
                with contextlib.suppress(OSError):
                    os.remove(path)
        elif name.endswith(".mp4"):
            entries.append((st.st_mtime, st.st_size, path))
    entries.sort()
    total = sum(size for _, size, _ in entries)
    for mtime, size, path in entries:
        if path == keep:
            continue
        if now - mtime <= CACHE_TTL and total <= CACHE_MAX_BYTES:
            break
        try:
            os.remove(path)
        except OSError:
            continue
        total -= size


def _stem(ref: WorkRef) -> str:
    """The output file stem: the work id, plus the part when one was chosen."""
    return f"{ref.work_id}-p{ref.part}" if ref.part else ref.work_id


def cached_video(url: str, out_dir: str) -> Optional[str]:
    """A finished mp4 for this work and part from the disk cache, copied into ``out_dir``."""
    try:
        ref = _ref(None, url)
    except (BilibiliError, MatchaError) as exc:
        logger.info("Bilibili cache lookup skipped: %s", describe_error(exc))
        return None
    key = cache_key(ref)
    cached = _from_cache(key, out_dir, _stem(ref))
    if cached:
        logger.info("Bilibili cache hit for %s", key)
    return cached


def store_video(url: str, path: str) -> None:
    """Cache a finished mp4 for this work and part. The caller only stores accepted files."""
    try:
        key = cache_key(_ref(None, url))
    except (BilibiliError, MatchaError) as exc:
        # A link layer that cannot name the work must never fail a finished download.
        logger.info("Bilibili cache store skipped: %s", describe_error(exc))
        return
    try:
        _to_cache(path, key)
    except OSError as exc:
        # A full or read-only cache must not fail a download that already succeeded.
        logger.warning("Bilibili cache store failed: %s", failure_reason(exc))


def download_native_video(
    url: str, out_dir: str, client: Optional[httpx.Client] = None
) -> str:
    """Download a Bilibili video as one mp4 in ``out_dir`` and return its path."""
    os.makedirs(out_dir, exist_ok=True)
    with _open(client) as open_client:
        ref = _ref(open_client, url)
        plan = _download_plan(open_client, ref)
        headers = dict(plan.headers)
        work = tempfile.mkdtemp(prefix=".bilibili-", dir=out_dir)
        try:
            video_path = os.path.join(work, "video.m4s")
            audio_path = os.path.join(work, "audio.m4s")
            _download_stream(open_client, plan.video, video_path, headers)
            _download_stream(open_client, plan.audio, audio_path, headers)
            out_path = os.path.join(out_dir, f"{_stem(ref)}.mp4")
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
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return out_path
