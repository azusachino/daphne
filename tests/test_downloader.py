import os
import signal
import unittest
import tempfile
import shutil
import json
import subprocess
from unittest.mock import patch, MagicMock

from daphne import downloader
from daphne.bilibili import BilibiliError, RateLimitedError
from daphne.downloader import (
    yutto_fetch,
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


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _via_subprocess_run(cmd, timeout):
    """Route run_tool through subprocess.run, so tests keep their subprocess.run fakes."""
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=timeout
        )
    except subprocess.CalledProcessError as e:
        return e.returncode, _text(e.stdout), _text(e.stderr)
    code = res.returncode if isinstance(res.returncode, int) else 0
    return code, _text(res.stdout), _text(res.stderr)


class TestDownloader(unittest.TestCase):
    def setUp(self):
        tool_patch = patch(
            "daphne.downloader.run_tool", side_effect=_via_subprocess_run
        )
        tool_patch.start()
        self.addCleanup(tool_patch.stop)
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

    @patch(
        "daphne.bilibili.fetch_bilibili_metadata",
        side_effect=BilibiliError("native unavailable"),
    )
    @patch("subprocess.run")
    def test_fetch_video_metadata_trims_bilibili_webpage_url(self, mock_run, _native):
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

    @patch(
        "daphne.bilibili.fetch_bilibili_metadata",
        side_effect=BilibiliError("native unavailable"),
    )
    @patch("daphne.downloader.logger.warning")
    @patch("subprocess.run")
    def test_fetch_video_metadata_bilibili_retries_with_headers(
        self, mock_run, mock_log_warn, _native
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

        # Both attempts failing must not invoke another downloader.
        mock_run.reset_mock()
        mock_scan.reset_mock()
        mock_scan.side_effect = [None, None]
        with self.assertRaisesRegex(RuntimeError, "yt-dlp"):
            download_video("http://x.com", self.test_dir)
        self.assertEqual(mock_run.call_count, 2)
        for call in mock_run.call_args_list:
            self.assertEqual(call.args[0][:2], ["uvx", "--from"])
            self.assertIn("yt-dlp", call.args[0])

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
        # Both yt-dlp attempts truncate; preserve the longest partial.
        mock_scan.side_effect = ["/tmp/a.mp4", "/tmp/b.mp4"]
        mock_dur.side_effect = [100, 200]
        res = download_video("http://x.com", self.test_dir, expected_duration=270)
        self.assertEqual(res, "/tmp/b.mp4")
        self.assertEqual(mock_run.call_count, 2)

    @patch("daphne.downloader.scan_largest_audio_file")
    @patch("subprocess.run")
    def test_download_audio(self, mock_run, mock_scan):
        mock_scan.return_value = "/tmp/audio.mp3"
        res = download_audio("http://x.com", self.test_dir)
        self.assertEqual(res, "/tmp/audio.mp3")
        self.assertEqual(mock_run.call_count, 1)
        cmd = mock_run.call_args[0][0]
        self.assertIn("--extract-audio", cmd)


def _media(folder: str, name: str) -> str:
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(b"media")
    return path


class TestBilibiliChain(unittest.TestCase):
    """Cache, then native player API, then yutto, then yt-dlp. Only complete files are cached."""

    BILI = "https://www.bilibili.com/video/BV1aMEj62EdA"

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.cache_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.test_dir, True)
        self.addCleanup(shutil.rmtree, self.cache_dir, True)
        self.native = self._patch(
            "daphne.bilibili.download_native_video",
            side_effect=BilibiliError("native unavailable"),
        )
        self.native_audio = self._patch(
            "daphne.bilibili.download_native_audio",
            side_effect=BilibiliError("native unavailable"),
        )
        self.yutto = self._patch(
            "daphne.downloader.yutto_fetch",
            side_effect=RuntimeError("yutto unavailable"),
        )
        self.ytdlp = self._patch(
            "daphne.downloader._ytdlp_bilibili_video",
            side_effect=RuntimeError("yt-dlp unavailable"),
        )
        self._patch("daphne.bilibili.CACHE_DIR", new=self.cache_dir)

    def _patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_native_file_is_returned_and_cached(self):
        self.native.side_effect = lambda link, folder: _media(
            folder, "BV1aMEj62EdA.mp4"
        )
        path = download_video(self.BILI, self.test_dir)
        self.assertEqual(os.path.basename(os.path.dirname(path)), "native")
        self.yutto.assert_not_called()

        again = download_video(self.BILI, self.test_dir)
        self.assertEqual(self.native.call_count, 1)
        self.assertEqual(os.path.basename(again), "BV1aMEj62EdA.mp4")
        self.assertTrue(
            os.path.exists(os.path.join(self.cache_dir, "BV1aMEj62EdA-p1.mp4"))
        )

    def test_yutto_runs_when_native_fails(self):
        self.yutto.side_effect = lambda link, folder, audio_only=False: (
            _media(folder, "clip.mp4"),
            None,
        )
        path = download_video(self.BILI, self.test_dir)
        self.assertEqual(os.path.basename(os.path.dirname(path)), "yutto")
        self.ytdlp.assert_not_called()

    def test_yt_dlp_is_the_last_resort(self):
        self.ytdlp.side_effect = lambda link, folder: _media(folder, "last.mp4")
        path = download_video(self.BILI, self.test_dir)
        self.assertEqual(os.path.basename(os.path.dirname(path)), "yt-dlp")

    def test_truncated_native_file_is_not_cached_and_yutto_completes(self):
        self.native.side_effect = lambda link, folder: _media(folder, "partial.mp4")
        self.yutto.side_effect = lambda link, folder, audio_only=False: (
            _media(folder, "full.mp4"),
            290,
        )
        durations = lambda path: 50 if "partial" in path else 290  # noqa: E731
        with (
            patch("daphne.downloader.probe_video_duration", side_effect=durations),
            patch("daphne.downloader.bilibili.store_video") as store,
        ):
            path = download_video(self.BILI, self.test_dir, expected_duration=300)
        self.assertTrue(path.endswith("full.mp4"))
        store.assert_called_once()
        self.assertTrue(store.call_args[0][1].endswith("full.mp4"))

    def test_longest_partial_is_returned_and_not_cached_when_nothing_is_complete(self):
        self.native.side_effect = lambda link, folder: _media(folder, "short.mp4")
        self.yutto.side_effect = lambda link, folder, audio_only=False: (
            _media(folder, "longer.mp4"),
            120,
        )
        durations = lambda path: 50 if "short" in path else 120  # noqa: E731
        with (
            patch("daphne.downloader.probe_video_duration", side_effect=durations),
            patch("daphne.downloader.bilibili.store_video") as store,
        ):
            path = download_video(self.BILI, self.test_dir, expected_duration=300)
        self.assertTrue(path.endswith("longer.mp4"))
        store.assert_not_called()

    def test_rate_limited_native_engine_reports_the_rate_limit(self):
        self.native.side_effect = RateLimitedError("risk control")
        with self.assertRaisesRegex(RuntimeError, "rate-limiting"):
            download_video(self.BILI, self.test_dir)

    def test_all_engines_failing_reports_the_generic_message(self):
        with self.assertRaisesRegex(RuntimeError, "no download engine could fetch"):
            download_video(self.BILI, self.test_dir)

    def test_no_new_engine_starts_after_the_time_budget(self):
        with patch(
            "daphne.downloader.time.monotonic", side_effect=[0.0] + [1000.0] * 20
        ):
            with self.assertRaises(RuntimeError):
                download_video(self.BILI, self.test_dir)
        self.native.assert_not_called()
        self.yutto.assert_not_called()

    def test_logs_name_the_host_not_the_full_link(self):
        with patch("daphne.downloader.logger.warning") as warn:
            with self.assertRaises(RuntimeError):
                download_video(self.BILI, self.test_dir)
        logged = str(warn.call_args_list)
        self.assertNotIn("BV1aMEj62EdA", logged)

    def test_audio_uses_native_then_yutto_audio_only(self):
        self.yutto.side_effect = lambda link, folder, audio_only=False: (
            _media(folder, "song.m4a"),
            None,
        )
        path = download_audio(self.BILI, self.test_dir)
        self.assertEqual(os.path.basename(path), "song.m4a")
        self.native_audio.assert_called_once()
        self.assertTrue(self.yutto.call_args.kwargs["audio_only"])

    def test_native_metadata_is_used_first(self):
        with (
            patch(
                "daphne.bilibili.fetch_bilibili_metadata",
                return_value={"title": "Cat"},
            ),
            patch("daphne.downloader.run_tool") as run,
        ):
            self.assertEqual(fetch_video_metadata(self.BILI), {"title": "Cat"})
        run.assert_not_called()

    def test_metadata_falls_back_to_yt_dlp_when_native_fails(self):
        with (
            patch(
                "daphne.bilibili.fetch_bilibili_metadata",
                side_effect=BilibiliError("view failed"),
            ),
            patch("daphne.downloader.run_tool", return_value=(1, "", "")) as run,
        ):
            self.assertEqual(fetch_video_metadata(self.BILI), {})
        self.assertEqual(run.call_count, 2)

    @patch("daphne.downloader._run_yutto")
    @patch("daphne.downloader._run_cmd", return_value=(False, ""))
    def test_other_sites_never_reach_the_bilibili_chain(self, mock_ytdlp, mock_yutto):
        with self.assertRaises(RuntimeError):
            download_video("https://www.youtube.com/watch?v=abc", self.test_dir)
        self.native.assert_not_called()
        self.yutto.assert_not_called()
        mock_yutto.assert_not_called()


class TestYuttoHardening(unittest.TestCase):
    BILI = "https://www.bilibili.com/video/BV1aMEj62EdA"

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.test_dir, True)

    def test_exit_code_zero_without_a_file_is_a_failure(self):
        proc = MagicMock(pid=1)
        proc.communicate.return_value = ("", "")
        with patch("daphne.downloader.subprocess.Popen", return_value=proc):
            with self.assertRaisesRegex(RuntimeError, "produced no file"):
                yutto_fetch(self.BILI, self.test_dir)

    def test_timeout_kills_the_whole_process_group(self):
        proc = MagicMock(pid=77)
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired("yutto", 1),
            ("", ""),
        ]
        with (
            patch("daphne.downloader.subprocess.Popen", return_value=proc),
            patch("daphne.downloader.os.killpg") as killpg,
        ):
            output = downloader._run_yutto(["--x"])
        killpg.assert_called_once_with(77, signal.SIGKILL)
        self.assertIn("timeout", output)

    def test_tools_do_not_inherit_the_bot_token(self):
        with patch.dict(
            os.environ,
            {"DAPHNE_BOT_TOKEN": "123:SECRET", "PATH": "/usr/bin"},
            clear=True,
        ):
            with patch("daphne.downloader.subprocess.Popen") as popen:
                popen.return_value.communicate.return_value = ("", "")
                popen.return_value.returncode = 0
                downloader._run_cmd(["uvx", "yt-dlp"])
        env = popen.call_args.kwargs["env"]
        self.assertNotIn("DAPHNE_BOT_TOKEN", env)
        self.assertEqual(env["PATH"], "/usr/bin")


class TestToolWarmUp(unittest.TestCase):
    def test_refresh_never_raises_and_downloads_still_run(self):
        with patch(
            "daphne.downloader.run_tool",
            side_effect=subprocess.TimeoutExpired("uvx", 1),
        ):
            downloader.warm_up_tools()
        with patch("daphne.downloader.run_tool", side_effect=OSError("uvx missing")):
            downloader.warm_up_tools()
