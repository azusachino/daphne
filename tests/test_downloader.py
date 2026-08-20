import os
import unittest
import tempfile
import shutil
import json
import subprocess
from unittest.mock import patch, MagicMock

from daphne.downloader import (
    scan_largest_media_file,
    download_video,
    probe_video_dimensions,
    fetch_video_metadata,
    fetch_instagram_fallback_media,
    format_duration,
    format_video_caption,
    download_audio,
    sanitize_video_url,
)


class TestDownloader(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_sanitize_video_url(self):
        # Bilibili: drop all query params (BV id is in the path) and normalize domain.
        self.assertEqual(
            sanitize_video_url(
                "https://www.bilibili.com/video/BV1aMEj62EdA/?buvid=ABC&p=1"
            ),
            "https://www.bilibili.com/video/BV1aMEj62EdA",
        )
        self.assertEqual(
            sanitize_video_url(
                "https://bilibili.com/video/BV1aMEj62EdA/?buvid=ABC&p=1"
            ),
            "https://www.bilibili.com/video/BV1aMEj62EdA",
        )
        # YouTube: keep v + t, drop tracking/navigation noise.
        self.assertEqual(
            sanitize_video_url(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
                "&list=PLx&index=2&t=30s&si=AbC&pp=ygU_"
            ),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30s",
        )
        # youtu.be: id in path, keep t, drop si.
        self.assertEqual(
            sanitize_video_url("https://youtu.be/dQw4w9WgXcQ?si=AbC&t=10"),
            "https://youtu.be/dQw4w9WgXcQ?t=10",
        )
        # Other platforms: left untouched.
        self.assertEqual(
            sanitize_video_url("https://example.com/v/abc?ref=x"),
            "https://example.com/v/abc?ref=x",
        )

    def test_scan_largest_media_file(self):
        # 1. No media files
        self.assertIsNone(scan_largest_media_file(self.test_dir))

        # 2. Write small and large files with valid and invalid extensions
        file_txt = os.path.join(self.test_dir, "large_text.txt")
        with open(file_txt, "wb") as f:
            f.write(b"0" * 1000)

        file_mp4_small = os.path.join(self.test_dir, "small.mp4")
        with open(file_mp4_small, "wb") as f:
            f.write(b"0" * 10)

        file_mp4_large = os.path.join(self.test_dir, "large.mp4")
        with open(file_mp4_large, "wb") as f:
            f.write(b"0" * 100)

        # Largest media file should be large.mp4
        res = scan_largest_media_file(self.test_dir)
        self.assertEqual(res, file_mp4_large)

    def test_format_duration(self):
        self.assertEqual(format_duration(5), "00:05")
        self.assertEqual(format_duration(125), "02:05")
        self.assertEqual(format_duration(3665), "01:01:05")

    def test_format_video_caption(self):
        # YouTube, no sender
        cap = format_video_caption(
            "A * B", "Uploader #1", "12:34", "http://x.com", "youtube"
        )
        self.assertIn("<b>A * B</b>", cap)
        self.assertIn("<b>👤 Uploader:</b> Uploader #1", cap)
        self.assertIn("<b>🕒 Duration:</b> 12:34", cap)
        self.assertIn('<a href="http://x.com">🔗 Source (📺 Youtube)</a>', cap)
        self.assertIn("#youtube", cap)
        self.assertIn("daphne", cap)
        self.assertNotIn("via", cap)

        # Bilibili, with sender
        cap_bili = format_video_caption(
            "Bili Bili", "User2", "01:00", "http://b23.tv/xyz", "bilibili", "via @haru"
        )
        self.assertIn("<b>Bili Bili</b>", cap_bili)
        self.assertIn("#bilibili", cap_bili)
        self.assertIn("via @haru", cap_bili)

        # Multi-word uploader name collapses into one author hashtag
        cap_multi = format_video_caption(
            "Some Video", "The New York Times", "01:00", "http://x.com", "youtube"
        )
        self.assertIn("#youtube #the_new_york_times", cap_multi)

        # Non-ASCII uploader name is preserved, not stripped
        cap_unicode = format_video_caption(
            "Some Video", "山田太郎", "01:00", "http://x.com", "youtube"
        )
        self.assertIn("#youtube #山田太郎", cap_unicode)

        # No uploader ("unknown" or empty) -> no author hashtag added
        cap_unknown = format_video_caption(
            "Some Video", "unknown", "01:00", "http://x.com", "youtube"
        )
        self.assertIn("#youtube", cap_unknown)
        self.assertNotIn("#unknown", cap_unknown)

        cap_empty = format_video_caption(
            "Some Video", "", "01:00", "http://x.com", "youtube"
        )
        self.assertIn("#youtube", cap_empty)

    @patch("subprocess.run")
    def test_probe_video_dimensions(self, mock_run):
        # 1. Success case
        mock_run.return_value = MagicMock(
            stdout=json.dumps(
                {"streams": [{"width": 1280, "height": 720, "duration": "45.67"}]}
            ),
            returncode=0,
        )
        w, h, d = probe_video_dimensions("dummy_path")
        self.assertEqual(w, 1280)
        self.assertEqual(h, 720)
        self.assertEqual(d, 45)

        # 2. Failure / empty stream case
        mock_run.return_value = MagicMock(stdout="{}", returncode=0)
        w, h, d = probe_video_dimensions("dummy_path")
        self.assertIsNone(w)
        self.assertIsNone(h)
        self.assertIsNone(d)

    @patch("subprocess.run")
    def test_fetch_video_metadata(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout=json.dumps(
                {
                    "title": "My Title",
                    "uploader": "My Uploader",
                    "duration": 300,
                    "webpage_url": "http://youtube.com/watch?v=123",
                }
            ),
            returncode=0,
        )
        meta = fetch_video_metadata("http://youtube.com/watch?v=123")
        self.assertEqual(meta["title"], "My Title")
        self.assertEqual(meta["uploader"], "My Uploader")
        self.assertEqual(meta["duration"], 300)

    @patch("subprocess.run")
    def test_fetch_video_metadata_trims_bilibili_webpage_url(self, mock_run):
        # yt-dlp returns a Bilibili webpage_url with tracking query params;
        # the caption link must be trimmed to the canonical video URL.
        mock_run.return_value = MagicMock(
            stdout=json.dumps(
                {
                    "title": "Cat",
                    "webpage_url": "https://www.bilibili.com/video/BV1aMEj62EdA/"
                    "?buvid=ABC&share_source=COPY&p=1",
                }
            ),
            returncode=0,
        )
        meta = fetch_video_metadata("https://www.bilibili.com/video/BV1aMEj62EdA")
        self.assertEqual(
            meta["webpage_url"], "https://www.bilibili.com/video/BV1aMEj62EdA"
        )

    @patch("daphne.downloader.logger.warning")
    @patch("subprocess.run")
    def test_fetch_video_metadata_bilibili_retries_with_headers(
        self, mock_run, mock_log_warn
    ):
        mock_run.side_effect = [
            Exception("HTTP 412"),
            MagicMock(
                stdout=json.dumps(
                    {
                        "title": "Bili Title",
                        "uploader": "Bili Uploader",
                        "duration": 108,
                        "webpage_url": "https://www.bilibili.com/video/BV1",
                    }
                ),
                returncode=0,
            ),
        ]

        meta = fetch_video_metadata("https://www.bilibili.com/video/BV1")

        self.assertEqual(meta["title"], "Bili Title")
        self.assertEqual(mock_run.call_count, 2)
        second_cmd = mock_run.call_args_list[1][0][0]
        self.assertIn("Referer:https://www.bilibili.com/", second_cmd)
        self.assertIn("Origin:https://www.bilibili.com", second_cmd)

    @patch("subprocess.run")
    def test_fetch_instagram_fallback_media_single_image(self, mock_run):
        # Real case that motivated this fallback: an Instagram photo post
        # parth-dl couldn't read. yt-dlp --dump-json --ignore-no-formats-error
        # still returns one JSON object with no "formats" but a "thumbnail".
        mock_run.return_value = MagicMock(
            stdout=json.dumps(
                {
                    "id": "DRZSlC7D3OT",
                    "description": "Happy birthday!",
                    "uploader": "nyarumaa.cosplay",
                    "formats": [],
                    "thumbnail": "https://instagram.fna.fbcdn.net/photo.jpg",
                }
            )
            + "\n",
            returncode=0,
        )

        data = fetch_instagram_fallback_media("https://www.instagram.com/p/DRZSlC7D3OT")

        self.assertEqual(data["type"], "image")
        self.assertEqual(
            data["images"], [{"url": "https://instagram.fna.fbcdn.net/photo.jpg"}]
        )
        self.assertEqual(data["formats"], [])
        self.assertEqual(data["uploader"], "nyarumaa.cosplay")
        self.assertEqual(data["title"], "Happy birthday!")

    @patch("subprocess.run")
    def test_fetch_instagram_fallback_media_carousel(self, mock_run):
        # Carousels are dumped as one JSON object per line (no --no-playlist),
        # mixing an image slide and a video slide.
        lines = [
            json.dumps(
                {
                    "id": "ABC123",
                    "uploader": "someone",
                    "formats": [],
                    "thumbnail": "https://cdn/img1.jpg",
                }
            ),
            json.dumps(
                {
                    "id": "ABC123",
                    "uploader": "someone",
                    "formats": [{"url": "https://cdn/video.mp4"}],
                    "thumbnail": "https://cdn/img2.jpg",
                }
            ),
        ]
        mock_run.return_value = MagicMock(stdout="\n".join(lines), returncode=0)

        data = fetch_instagram_fallback_media("https://www.instagram.com/p/ABC123")

        self.assertEqual(data["images"], [{"url": "https://cdn/img1.jpg"}])
        self.assertEqual(data["formats"], [{"url": "https://cdn/video.mp4"}])
        self.assertEqual(data["type"], "video")

    @patch("subprocess.run")
    def test_fetch_instagram_fallback_media_no_usable_data(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        self.assertIsNone(
            fetch_instagram_fallback_media("https://www.instagram.com/p/Nope")
        )

    @patch("subprocess.run", side_effect=Exception("yt-dlp not found"))
    def test_fetch_instagram_fallback_media_subprocess_error(self, mock_run):
        self.assertIsNone(
            fetch_instagram_fallback_media("https://www.instagram.com/p/Nope")
        )

    @patch("daphne.downloader.scan_largest_media_file")
    @patch("subprocess.run")
    def test_download_video_fallback(self, mock_run, mock_scan):
        # Case 1: Pass 1 works
        mock_scan.side_effect = ["/tmp/file.mp4"]
        res = download_video("http://x.com", self.test_dir)
        self.assertEqual(res, "/tmp/file.mp4")
        self.assertEqual(mock_run.call_count, 1)

        # Reset mocks
        mock_run.reset_mock()
        mock_scan.reset_mock()

        # Case 2: Pass 1 fails, Pass 2 succeeds
        mock_scan.side_effect = [None, "/tmp/pass2.mp4"]
        res = download_video("http://x.com", self.test_dir)
        self.assertEqual(res, "/tmp/pass2.mp4")
        self.assertEqual(mock_run.call_count, 2)

        # Reset mocks
        mock_run.reset_mock()
        mock_scan.reset_mock()

        # Case 3: Pass 1 & 2 fail, you-get succeeds
        mock_scan.side_effect = [None, None, "/tmp/youget.mp4"]
        res = download_video("http://x.com", self.test_dir)
        self.assertEqual(res, "/tmp/youget.mp4")
        self.assertEqual(mock_run.call_count, 3)

        # Reset mocks
        mock_run.reset_mock()
        mock_scan.reset_mock()

        # Case 4: Pass 1 & 2 & you-get fail, lux succeeds
        mock_scan.side_effect = [None, None, None, "/tmp/lux.mp4"]
        res = download_video("http://x.com", self.test_dir)
        self.assertEqual(res, "/tmp/lux.mp4")
        self.assertEqual(mock_run.call_count, 4)
        self.assertEqual(mock_run.call_args_list[-1][0][0][0], "lux")

        # Reset mocks
        mock_run.reset_mock()
        mock_scan.reset_mock()

        # Case 5: All fail -> raise RuntimeError
        mock_scan.side_effect = [None, None, None, None]
        with self.assertRaises(RuntimeError):
            download_video("http://x.com", self.test_dir)

    @patch("daphne.downloader.scan_largest_media_file", return_value=None)
    @patch("subprocess.run")
    def test_download_video_raises_specific_reason_for_missing_js_runtime(
        self, mock_run, mock_scan
    ):
        # Real case that motivated this classification: YouTube's extractor
        # warns and then 403s when yt-dlp has no JS runtime to solve its
        # challenge -- every engine fails identically, and the raised error
        # should say *that*, not a generic "all engines failed".
        mock_run.side_effect = subprocess.CalledProcessError(
            1,
            ["yt-dlp"],
            stderr="WARNING: [youtube] No supported JavaScript runtime could "
            "be found. See https://github.com/yt-dlp/yt-dlp/wiki/EJS for "
            "details.\nERROR: unable to download video data: HTTP Error 403: "
            "Forbidden",
        )
        with self.assertRaises(RuntimeError) as ctx:
            download_video("https://youtube.com/watch?v=x", self.test_dir)
        self.assertIn("JavaScript runtime", str(ctx.exception))

    @patch("daphne.downloader.probe_video_duration")
    @patch("daphne.downloader.scan_largest_media_file")
    @patch("subprocess.run")
    def test_download_video_retries_on_truncation(self, mock_run, mock_scan, mock_dur):
        # Pass 1 returns a truncated file (120s of an expected 270s video);
        # Pass 2 returns a complete file and should be accepted.
        mock_scan.side_effect = ["/tmp/short.mp4", "/tmp/full.mp4"]
        mock_dur.side_effect = [120, 270]
        res = download_video("http://x.com", self.test_dir, expected_duration=270)
        self.assertEqual(res, "/tmp/full.mp4")
        self.assertEqual(mock_run.call_count, 2)

    @patch("daphne.downloader.probe_video_duration")
    @patch("daphne.downloader.scan_largest_media_file")
    @patch("subprocess.run")
    def test_download_video_returns_longest_partial(
        self, mock_run, mock_scan, mock_dur
    ):
        # Every engine truncates; the longest partial is returned as a last resort.
        mock_scan.side_effect = [
            "/tmp/a.mp4",
            "/tmp/b.mp4",
            "/tmp/c.mp4",
            "/tmp/d.mp4",
        ]
        # probed once per engine to record candidate length (100, 200, 150, 120)
        mock_dur.side_effect = [100, 200, 150, 120]
        res = download_video("http://x.com", self.test_dir, expected_duration=270)
        self.assertEqual(res, "/tmp/b.mp4")
        self.assertEqual(mock_run.call_count, 4)

    @patch("daphne.downloader.scan_largest_audio_file")
    @patch("subprocess.run")
    def test_download_audio(self, mock_run, mock_scan):
        mock_scan.return_value = "/tmp/audio.mp3"
        res = download_audio("http://x.com", self.test_dir)
        self.assertEqual(res, "/tmp/audio.mp3")
        self.assertEqual(mock_run.call_count, 1)
        cmd = mock_run.call_args[0][0]
        self.assertIn("--extract-audio", cmd)
