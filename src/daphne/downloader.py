import os
import subprocess
import random
import logging
import json
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from typing import Tuple, Optional

from daphne.messages import HtmlMessage

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


def is_bilibili_url(url: str) -> bool:
    return "bilibili.com" in url or "b23.tv" in url


def is_youtube_url(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


# YouTube query params worth keeping; everything else (si, pp, list, index,
# feature, ...) is tracking/navigation noise that clutters the caption link.
YOUTUBE_KEEP_PARAMS = {"v", "t"}


def sanitize_video_url(url: str) -> str:
    url = url.strip().strip("<>()[]{}\"'`,")
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    if is_bilibili_url(url):
        netloc = parsed.netloc
        if netloc == "bilibili.com":
            netloc = "www.bilibili.com"
        # BV id lives in the path; no query is needed.
        return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))
    if is_youtube_url(url):
        kept = [(k, v) for k, v in parse_qsl(parsed.query) if k in YOUTUBE_KEEP_PARAMS]
        return urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), urlencode(kept), "")
        )
    return url


def bilibili_headers() -> list[str]:
    return [
        "--add-header",
        "Referer:https://www.bilibili.com/",
        "--add-header",
        "Origin:https://www.bilibili.com",
    ]


def scan_largest_audio_file(out_dir: str) -> Optional[str]:
    valid_exts = {".mp3", ".m4a", ".ogg", ".wav", ".opus", ".flac"}
    largest_file = None
    largest_size = -1
    for root, _, files in os.walk(out_dir):
        for file in files:
            _, ext = os.path.splitext(file)
            if ext.lower() in valid_exts:
                full_path = os.path.join(root, file)
                try:
                    size = os.path.getsize(full_path)
                    if size > largest_size:
                        largest_size = size
                        largest_file = full_path
                except OSError:
                    pass
    return largest_file


def scan_largest_media_file(out_dir: str) -> Optional[str]:
    valid_exts = {".mp4", ".mkv", ".webm", ".flv", ".mov", ".m4v", ".ts"}
    largest_file = None
    largest_size = -1
    for root, _, files in os.walk(out_dir):
        for file in files:
            _, ext = os.path.splitext(file)
            if ext.lower() in valid_exts:
                full_path = os.path.join(root, file)
                try:
                    size = os.path.getsize(full_path)
                    if size > largest_size:
                        largest_size = size
                        largest_file = full_path
                except OSError:
                    pass
    return largest_file


def _run_cmd(cmd: list[str]) -> bool:
    logger.info(f"Running command: {' '.join(cmd)}")
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=300.0
        )
        logger.info(f"Command succeeded. stdout: {res.stdout[:500]}")
        return True
    except subprocess.CalledProcessError as e:
        logger.warning(
            f"Command failed with exit code {e.returncode}. stderr: {e.stderr}"
        )
        return False
    except Exception as e:
        logger.warning(f"Failed to run command {cmd}: {e}")
        return False


# A download is treated as complete when its probed duration covers at least
# this fraction of the expected (metadata) duration. Bilibili anti-bot / segment
# limits can silently return a truncated file, so we verify and fall through to
# the next engine instead of accepting a partial video.
DURATION_COMPLETE_RATIO = 0.95


def probe_video_duration(file_path: str) -> Optional[int]:
    _, _, duration = probe_video_dimensions(file_path)
    return duration


def _is_complete(actual: Optional[int], expected_duration: Optional[float]) -> bool:
    """
    Whether a downloaded file is complete enough to accept, given its already
    probed duration. When no expected duration is known, or the file could not
    be probed, we cannot verify and accept the file rather than looping.
    """
    if not expected_duration or expected_duration <= 0:
        return True
    if actual is None:
        return True
    if actual >= expected_duration * DURATION_COMPLETE_RATIO:
        return True
    logger.warning(
        f"Downloaded video is truncated: {actual}s of expected "
        f"{int(expected_duration)}s. Trying next engine."
    )
    return False


def download_video(
    url: str, out_dir: str, expected_duration: Optional[float] = None
) -> str:
    os.makedirs(out_dir, exist_ok=True)

    yt_dlp_format = (
        "bestvideo[height<=1080][vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
        "best[vcodec^=avc1][ext=mp4]/best"
    )

    def cmd_pass1() -> list[str]:
        return [
            "uvx",
            "yt-dlp",
            "-f",
            yt_dlp_format,
            "--output",
            f"{out_dir}/%(id)s.%(ext)s",
            "--no-playlist",
            "--restrict-filenames",
            "--",
            url,
        ]

    def cmd_pass2() -> list[str]:
        cmd = [
            "uvx",
            "yt-dlp",
            "-f",
            yt_dlp_format,
            "--output",
            f"{out_dir}/%(id)s.%(ext)s",
            "--no-playlist",
            "--restrict-filenames",
            "--user-agent",
            random.choice(USER_AGENTS),
        ]
        if is_bilibili_url(url):
            cmd.extend(bilibili_headers())
        cmd.extend(["--", url])
        return cmd

    engines = [
        cmd_pass1,
        cmd_pass2,
        lambda: ["uvx", "you-get", "--output-dir", out_dir, url],
        lambda: ["lux", "-o", out_dir, "--silent", url],
    ]

    # Track the longest result across engines so that, if none is verifiably
    # complete, we still return the best partial rather than failing outright.
    best_path: Optional[str] = None
    best_duration = -1.0

    for build_cmd in engines:
        _run_cmd(build_cmd())
        largest = scan_largest_media_file(out_dir)
        if not largest:
            continue
        # Probe when we can verify against expected duration; otherwise accept.
        actual = probe_video_duration(largest) if expected_duration else None
        if _is_complete(actual, expected_duration):
            return largest
        if (actual or 0) > best_duration:
            best_duration = actual or 0
            best_path = largest

    if best_path:
        logger.warning(
            "No engine produced a complete video; returning longest partial "
            f"({int(best_duration)}s) from {best_path}."
        )
        return best_path

    raise RuntimeError("Failed to download video using all engines")


def download_audio(url: str, out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)

    cmd = [
        "uvx",
        "yt-dlp",
        "-f",
        "bestaudio/best",
        "--extract-audio",
        "--audio-format",
        "mp3",
        "--output",
        f"{out_dir}/%(id)s.%(ext)s",
        "--no-playlist",
        "--restrict-filenames",
        "--",
        url,
    ]
    _run_cmd(cmd)
    largest = scan_largest_audio_file(out_dir)
    if largest:
        return largest

    largest_media = scan_largest_media_file(out_dir)
    if largest_media:
        return largest_media

    raise RuntimeError("Failed to download audio using all engines")


def probe_video_dimensions(
    file_path: str,
) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,duration",
        file_path,
    ]
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=15.0
        )
        data = json.loads(res.stdout)
        streams = data.get("streams", [])
        if not streams:
            return None, None, None

        stream = streams[0]
        width = stream.get("width")
        height = stream.get("height")
        duration = stream.get("duration")

        w = int(width) if width is not None else None
        h = int(height) if height is not None else None

        d = None
        if duration is not None:
            try:
                d = int(float(duration))
            except ValueError:
                pass

        return w, h, d
    except Exception as e:
        logger.warning(f"Error probing video dimensions for {file_path}: {e}")
        return None, None, None


def fetch_video_metadata(url: str) -> dict:
    base = [
        "uvx",
        "yt-dlp",
        "--dump-json",
        "--no-playlist",
        "--user-agent",
        random.choice(USER_AGENTS),
    ]
    commands = [base + ["--", url]]
    if is_bilibili_url(url):
        commands.append(base + bilibili_headers() + ["--", url])

    for cmd in commands:
        try:
            res = subprocess.run(
                cmd, capture_output=True, text=True, check=True, timeout=30.0
            )
            data = json.loads(res.stdout)
            return {
                "title": data.get("title", ""),
                "uploader": data.get("uploader", ""),
                "duration": data.get("duration"),
                "thumbnail": data.get("thumbnail"),
                "webpage_url": sanitize_video_url(data.get("webpage_url", url)),
                "width": data.get("width"),
                "height": data.get("height"),
                "filesize": data.get("filesize"),
                "filesize_approx": data.get("filesize_approx"),
                "url": data.get("url")
                or (
                    data.get("formats", [{}])[-1].get("url")
                    if data.get("formats")
                    else None
                ),
            }
        except Exception as e:
            logger.warning(f"Failed to fetch video metadata via yt-dlp: {e}")
    return {}


def format_duration(seconds: int) -> str:
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    else:
        return f"{minutes:02d}:{secs:02d}"


def format_video_caption(
    title: str,
    uploader: str,
    duration: str,
    url: str,
    platform: str,
    sender: Optional[str] = None,
) -> str:
    source_tag = f"#{platform}"
    platform_icons = {
        "youtube": "📺",
        "bilibili": "⚡",
        "tiktok": "🎵",
        "instagram": "📸",
        "bluesky": "🦋",
        "twitter": "🐦",
        "pixiv": "🎨",
    }
    icon = platform_icons.get(platform.lower(), "🎥")
    return (
        HtmlMessage(sender=sender)
        .title(title)
        .fields(
            ("👤 Uploader", uploader),
            ("🕒 Duration", duration),
        )
        .link(url, f"🔗 Source ({icon} {platform.capitalize()})")
        .tags(source_tag)
        .render()
    )
