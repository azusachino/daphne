import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from daphne.bilibili import (
    BilibiliError,
    download_bilibili,
    fetch_bilibili_metadata,
    pick_audio_stream,
    pick_video_stream,
    video_ref,
)
from daphne.downloader import download_video

VIEW = {
    "bvid": "BV1aMEj62EdA",
    "title": "Cat",
    "owner": {"name": "Uploader"},
    "pic": "https://i.example/cover.jpg",
    "duration": 108,
    "dimension": {"width": 852, "height": 480, "rotate": 0},
    "pages": [{"cid": 111, "duration": 100}, {"cid": 222, "duration": 8}],
}


def _json(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload)


class TestUrlParsing(unittest.TestCase):
    def test_bv_and_av_links(self):
        self.assertEqual(
            video_ref("https://www.bilibili.com/video/BV1aMEj62EdA/"),
            ({"bvid": "BV1aMEj62EdA"}, 1),
        )
        self.assertEqual(
            video_ref("https://www.bilibili.com/video/av170001"), ({"aid": "170001"}, 1)
        )

    def test_part_number_is_read_from_query(self):
        self.assertEqual(
            video_ref("https://www.bilibili.com/video/BV1aMEj62EdA?p=2"),
            ({"bvid": "BV1aMEj62EdA"}, 2),
        )

    def test_bangumi_and_non_video_links_are_refused(self):
        with self.assertRaises(BilibiliError):
            video_ref("https://www.bilibili.com/bangumi/play/ep1")
        with self.assertRaises(BilibiliError):
            video_ref("https://www.bilibili.com/")


class TestStreamSelection(unittest.TestCase):
    def test_prefers_avc_then_highest_quality(self):
        dash = {
            "video": [
                {"id": 127, "codecid": 12, "baseUrl": "hevc-8k"},
                {"id": 32, "codecid": 7, "baseUrl": "avc-480"},
                {"id": 16, "codecid": 7, "baseUrl": "avc-360"},
            ],
            "audio": [
                {"id": 30216, "bandwidth": 64000, "baseUrl": "low"},
                {"id": 30280, "bandwidth": 192000, "baseUrl": "high"},
            ],
        }
        self.assertEqual(pick_video_stream(dash)["baseUrl"], "avc-480")
        self.assertEqual(pick_audio_stream(dash)["baseUrl"], "high")

    def test_missing_tracks_are_errors(self):
        with self.assertRaises(BilibiliError):
            pick_video_stream({"video": []})
        with self.assertRaises(BilibiliError):
            pick_audio_stream({"audio": []})


class TestApiBackoff(unittest.TestCase):
    def test_risk_control_412_is_retried_until_it_clears(self):
        calls = []

        def handler(request):
            if request.url.host == "www.bilibili.com":
                return httpx.Response(200, text="<html></html>")
            calls.append(request)
            if len(calls) < 3:
                return httpx.Response(412)
            return _json({"code": 0, "data": VIEW})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch("daphne.bilibili.time.sleep") as sleep:
            meta = fetch_bilibili_metadata(
                "https://www.bilibili.com/video/BV1aMEj62EdA", client=client
            )
        self.assertEqual(meta["title"], "Cat")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_gives_up_after_all_retries(self):
        client = httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(412))
        )
        with patch("daphne.bilibili.time.sleep"):
            with self.assertRaisesRegex(BilibiliError, "rate-limiting"):
                fetch_bilibili_metadata(
                    "https://www.bilibili.com/video/BV1aMEj62EdA", client=client
                )

    def test_api_error_code_is_reported_without_retry(self):
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: _json({"code": -10403, "message": "premium"})
            )
        )
        with patch("daphne.bilibili.time.sleep") as sleep:
            with self.assertRaisesRegex(BilibiliError, "-10403"):
                fetch_bilibili_metadata(
                    "https://www.bilibili.com/video/BV1aMEj62EdA", client=client
                )
        sleep.assert_not_called()


class TestMetadataAndShortLinks(unittest.TestCase):
    def test_metadata_matches_the_shape_the_yt_dlp_path_returns(self):
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: _json({"code": 0, "data": VIEW})
            )
        )
        meta = fetch_bilibili_metadata(
            "https://www.bilibili.com/video/BV1aMEj62EdA?p=2", client=client
        )
        self.assertEqual(meta["title"], "Cat")
        self.assertEqual(meta["uploader"], "Uploader")
        self.assertEqual(meta["duration"], 8)  # part 2's duration
        self.assertEqual(
            meta["webpage_url"], "https://www.bilibili.com/video/BV1aMEj62EdA"
        )
        self.assertEqual((meta["width"], meta["height"]), (852, 480))

    def test_b23_short_link_is_expanded_before_the_view_call(self):
        seen = []

        def handler(request):
            seen.append(request.url.host)
            if request.url.host == "www.bilibili.com":
                return httpx.Response(200, text="<html></html>")
            if request.url.host == "b23.tv":
                return httpx.Response(
                    302,
                    headers={"location": "https://www.bilibili.com/video/BV1aMEj62EdA"},
                )
            return _json({"code": 0, "data": VIEW})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        fetch_bilibili_metadata("https://b23.tv/abc123", client=client)
        self.assertEqual(seen, ["b23.tv", "www.bilibili.com", "api.bilibili.com"])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class TestDownloadEndToEnd(unittest.TestCase):
    """Mocked Bilibili API and CDN, real ffmpeg muxing of generated media."""

    @classmethod
    def setUpClass(cls):
        cls.src = tempfile.mkdtemp(prefix="bili-src-")
        cls.video = os.path.join(cls.src, "v.m4s")
        cls.audio = os.path.join(cls.src, "a.m4s")
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=1:size=64x64:rate=5",
                "-c:v",
                "mpeg4",
                "-f",
                "mp4",
                cls.video,
            ],
            check=True,
        )
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=duration=1",
                "-c:a",
                "aac",
                "-f",
                "mp4",
                cls.audio,
            ],
            check=True,
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.src, ignore_errors=True)

    def _client(self):
        video_bytes = Path(self.video).read_bytes()
        audio_bytes = Path(self.audio).read_bytes()
        playurl = {
            "code": 0,
            "data": {
                "dash": {
                    "video": [
                        {
                            "id": 32,
                            "codecid": 7,
                            "bandwidth": 1,
                            "baseUrl": "https://cdn.example/v.m4s",
                        }
                    ],
                    "audio": [
                        {
                            "id": 30280,
                            "bandwidth": 1,
                            "baseUrl": "https://cdn.example/a.m4s",
                        }
                    ],
                }
            },
        }

        def handler(request):
            path = request.url.path
            if path == "/x/web-interface/view":
                return _json({"code": 0, "data": VIEW})
            if path == "/x/player/playurl":
                self.assertEqual(request.url.params["fnval"], "4048")
                self.assertEqual(request.url.params["cid"], "111")
                return _json(playurl)
            if path == "/v.m4s":
                return httpx.Response(200, content=video_bytes)
            if path == "/a.m4s":
                return httpx.Response(200, content=audio_bytes)
            return httpx.Response(404)

        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_merges_tracks_into_one_mp4_and_cleans_up(self):
        out_dir = tempfile.mkdtemp(prefix="bili-out-")
        try:
            path = download_bilibili(
                "https://www.bilibili.com/video/BV1aMEj62EdA",
                out_dir,
                client=self._client(),
            )
            self.assertEqual(os.path.basename(path), "BV1aMEj62EdA.mp4")
            streams = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "csv=p=0",
                    path,
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.split()
            self.assertEqual(sorted(streams), ["audio", "video"])
            # Only the merged file remains; the intermediate work dir is gone.
            self.assertEqual(os.listdir(out_dir), ["BV1aMEj62EdA.mp4"])
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)


class TestFallbackAndErrors(unittest.TestCase):
    def test_native_failure_message_surfaces_when_yt_dlp_also_fails(self):
        out_dir = tempfile.mkdtemp(prefix="bili-fallback-")
        try:
            with (
                patch(
                    "daphne.downloader.download_bilibili",
                    side_effect=BilibiliError(
                        "Bilibili kept rate-limiting this video; try again later"
                    ),
                ),
                patch("daphne.downloader._run_cmd", return_value=(False, "")),
                patch("daphne.downloader.scan_largest_media_file", return_value=None),
            ):
                with self.assertRaisesRegex(RuntimeError, "kept rate-limiting"):
                    download_video(
                        "https://www.bilibili.com/video/BV1aMEj62EdA", out_dir
                    )
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
