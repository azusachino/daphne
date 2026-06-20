import os
import subprocess
import random
import logging
import json
from urllib.parse import urlsplit, urlunsplit
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


def sanitize_video_url(url: str) -> str:
    url = url.strip().strip("<>()[]{}\"'`,")
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    if is_bilibili_url(url):
        return urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
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
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
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


def download_video(url: str, out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)

    # 1. yt-dlp Pass 1: plain
    cmd_pass1 = [
        "uvx",
        "yt-dlp",
        "-f",
        "bestvideo[height<=1080][vcodec^=avc1]+bestaudio[acodec^=mp4a]/best[vcodec^=avc1][ext=mp4]/best",
        "--output",
        f"{out_dir}/%(id)s.%(ext)s",
        "--no-playlist",
        "--restrict-filenames",
        url,
    ]
    _run_cmd(cmd_pass1)
    largest = scan_largest_media_file(out_dir)
    if largest:
        return largest

    # 2. yt-dlp Pass 2: anti-bot
    ua = random.choice(USER_AGENTS)
    cmd_pass2 = [
        "uvx",
        "yt-dlp",
        "-f",
        "bestvideo[height<=1080][vcodec^=avc1]+bestaudio[acodec^=mp4a]/best[vcodec^=avc1][ext=mp4]/best",
        "--output",
        f"{out_dir}/%(id)s.%(ext)s",
        "--no-playlist",
        "--restrict-filenames",
        "--user-agent",
        ua,
    ]
    if is_bilibili_url(url):
        cmd_pass2.extend(bilibili_headers())
    cmd_pass2.append(url)
    _run_cmd(cmd_pass2)
    largest = scan_largest_media_file(out_dir)
    if largest:
        return largest

    # 3. you-get (via uvx)
    cmd_youget = ["uvx", "you-get", "--output-dir", out_dir, url]
    _run_cmd(cmd_youget)
    largest = scan_largest_media_file(out_dir)
    if largest:
        return largest

    # 4. lux binary installed in the image
    cmd_lux = ["lux", "-o", out_dir, "--silent", url]
    _run_cmd(cmd_lux)
    largest = scan_largest_media_file(out_dir)
    if largest:
        return largest

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
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
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
    commands = [
        [
            "uvx",
            "yt-dlp",
            "--dump-json",
            "--no-playlist",
            "--user-agent",
            random.choice(USER_AGENTS),
            url,
        ]
    ]
    if is_bilibili_url(url):
        cmd = commands[0][:-1] + bilibili_headers() + [url]
        commands.append(cmd)

    for cmd in commands:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(res.stdout)
            return {
                "title": data.get("title", ""),
                "uploader": data.get("uploader", ""),
                "duration": data.get("duration"),
                "webpage_url": data.get("webpage_url", url),
                "width": data.get("width"),
                "height": data.get("height"),
                "filesize": data.get("filesize"),
                "filesize_approx": data.get("filesize_approx"),
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
    return (
        HtmlMessage(sender=sender)
        .title(title)
        .fields(
            ("Uploader:", uploader),
            ("Duration:", duration),
        )
        .link(url)
        .tags(source_tag)
        .render()
    )
