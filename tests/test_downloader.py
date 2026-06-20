import os
import unittest
import tempfile
import shutil
import json
from unittest.mock import patch, MagicMock

from daphne.downloader import (
    scan_largest_media_file,
    download_video,
    probe_video_dimensions,
    fetch_video_metadata,
    format_duration,
    format_video_caption,
)


class TestDownloader(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir)

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
            "A * B", "Uploader #1", "12:34", "http://x.com", False
        )
        self.assertIn("🎬 <b>A * B</b>", cap)
        self.assertIn("👤 Uploader #1", cap)
        self.assertIn("⏱️ 12:34", cap)
        self.assertIn('<a href="http://x.com">http://x.com</a>', cap)
        self.assertIn("#youtube", cap)
        self.assertIn("daphne", cap)
        self.assertNotIn("via", cap)

        # Bilibili, with sender
        cap_bili = format_video_caption(
            "Bili Bili", "User2", "01:00", "http://b23.tv/xyz", True, "via @haru"
        )
        self.assertIn("🎬 <b>Bili Bili</b>", cap_bili)
        self.assertIn("#bilibili", cap_bili)
        self.assertIn("via @haru", cap_bili)

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
    def test_fetch_video_metadata_bilibili_retries_with_headers(self, mock_run):
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
