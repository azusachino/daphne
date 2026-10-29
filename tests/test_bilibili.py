import collections
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from daphne import bilibili
from daphne.bilibili import (
    SESSION_TTL,
    BilibiliError,
    download_native_video,
    cached_video,
    canonical_url,
    store_video,
    fetch_bilibili_metadata,
    pick_audio_stream,
    pick_video_stream,
    video_ref,
)

SPI = {"code": 0, "data": {"b_3": "buvid3-test", "b_4": "buvid4-test", "b_nut": "1"}}
PARTS = [
    {
        "cid": 111,
        "page": 1,
        "part": "Cat",
        "duration": 100,
        "dimension": {"width": 852, "height": 480, "rotate": 0},
    },
    {
        "cid": 222,
        "page": 2,
        "part": "Second cut",
        "duration": 8,
        "dimension": {"width": 852, "height": 480, "rotate": 0},
    },
]
URL = "https://www.bilibili.com/video/BV1aMEj62EdA"


def _json(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload)


class FakeBilibili:
    """Serves the endpoints the module calls and records what it asked for."""

    def __init__(self, pagelist_statuses=(), media=None, spi_status=200):
        self.calls = collections.Counter()
        self.pagelist_statuses = list(pagelist_statuses)
        self.pagelist_cookies = []
        self.media = media or {}
        self.spi_status = spi_status
        self.order = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls[path] += 1
        self.order.append(request.url.host)
        if path == "/x/frontend/finger/spi":
            if self.spi_status != 200:
                return httpx.Response(self.spi_status)
            return _json(SPI)
        if path == "/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket":
            return _json({"code": 0, "data": {"ticket": "ticket-test"}})
        if path == "/x/player/pagelist":
            self.pagelist_cookies.append(request.headers.get("cookie", ""))
            if self.pagelist_statuses:
                status = self.pagelist_statuses.pop(0)
                if status != 200:
                    return httpx.Response(status)
            return _json({"code": 0, "data": PARTS})
        if path == "/x/player/playurl":
            return _json(
                {
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
            )
        if path == "/v.m4s" and "v" in self.media:
            return httpx.Response(200, content=self.media["v"])
        if path == "/a.m4s" and "a" in self.media:
            return httpx.Response(200, content=self.media["a"])
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


class BilibiliTestCase(unittest.TestCase):
    def setUp(self):
        # Module state is shared across calls, so every test starts cold.
        bilibili._session.update(cookies=None, at=0.0)
        cache_dir = tempfile.mkdtemp(prefix="bili-cache-")
        self.addCleanup(shutil.rmtree, cache_dir, True)
        patcher = patch.object(bilibili, "CACHE_DIR", cache_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cache_dir = cache_dir


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


class TestApiBackoff(BilibiliTestCase):
    def test_risk_control_412_is_retried_until_it_clears(self):
        fake = FakeBilibili(pagelist_statuses=[412, 412, 200])
        with patch("daphne.bilibili.time.sleep") as sleep:
            meta = fetch_bilibili_metadata(URL, client=fake.client())
        self.assertEqual(meta["duration"], 100)
        self.assertEqual(fake.calls["/x/player/pagelist"], 3)
        self.assertEqual(sleep.call_count, 2)

    def test_gives_up_after_all_retries(self):
        fake = FakeBilibili(pagelist_statuses=[412] * 10)
        with patch("daphne.bilibili.time.sleep"):
            with self.assertRaisesRegex(BilibiliError, "rate-limiting"):
                fetch_bilibili_metadata(URL, client=fake.client())

    def test_api_error_code_is_reported_without_retry(self):
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: _json({"code": -10403, "message": "premium"})
            )
        )
        with patch("daphne.bilibili.time.sleep") as sleep:
            with self.assertRaisesRegex(BilibiliError, "-10403"):
                fetch_bilibili_metadata(URL, client=client)
        sleep.assert_not_called()


class TestVisitorSession(BilibiliTestCase):
    def test_spi_cookies_are_sent_and_reused_within_the_ttl(self):
        fake = FakeBilibili()
        fetch_bilibili_metadata(URL, client=fake.client())
        fetch_bilibili_metadata(URL, client=fake.client())
        self.assertEqual(fake.calls["/x/frontend/finger/spi"], 1)
        self.assertIn("buvid3=buvid3-test", fake.pagelist_cookies[0])
        self.assertIn("buvid4=buvid4-test", fake.pagelist_cookies[-1])

    def test_session_is_refreshed_after_the_ttl(self):
        fake = FakeBilibili()
        fetch_bilibili_metadata(URL, client=fake.client())
        bilibili._session["at"] -= SESSION_TTL + 1
        fetch_bilibili_metadata(URL, client=fake.client())
        self.assertEqual(fake.calls["/x/frontend/finger/spi"], 2)

    def test_risk_control_refreshes_the_session_once(self):
        fake = FakeBilibili(pagelist_statuses=[412, 200])
        with patch("daphne.bilibili.time.sleep"):
            fetch_bilibili_metadata(URL, client=fake.client())
        self.assertEqual(fake.calls["/x/frontend/finger/spi"], 2)

    def test_failed_visitor_setup_does_not_block_the_api_call(self):
        fake = FakeBilibili(spi_status=500)
        meta = fetch_bilibili_metadata(URL, client=fake.client())
        self.assertEqual(meta["duration"], 100)
        self.assertIsNone(bilibili._session["cookies"])


class TestMetadataAndShortLinks(BilibiliTestCase):
    def test_metadata_matches_the_shape_the_yt_dlp_path_returns(self):
        fake = FakeBilibili()
        meta = fetch_bilibili_metadata(URL + "?p=2", client=fake.client())
        self.assertEqual(meta["title"], "Second cut")  # the part's name
        self.assertEqual(meta["uploader"], "")  # view is refused, so no uploader
        self.assertEqual(meta["duration"], 8)  # part 2's duration
        self.assertEqual(meta["webpage_url"], URL)
        self.assertEqual((meta["width"], meta["height"]), (852, 480))
        self.assertNotIn("/x/web-interface/view", fake.calls)

    def test_ticket_is_attached_to_the_visitor_session(self):
        fake = FakeBilibili()
        fetch_bilibili_metadata(URL, client=fake.client())
        self.assertEqual(
            fake.calls["/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket"], 1
        )
        self.assertIn("bili_ticket=ticket-test", fake.pagelist_cookies[-1])

    def test_failed_ticket_does_not_block_the_download(self):
        fake = FakeBilibili()
        with patch.object(bilibili, "_bili_ticket", return_value=None):
            meta = fetch_bilibili_metadata(URL, client=fake.client())
        self.assertEqual(meta["duration"], 100)
        self.assertNotIn("bili_ticket", fake.pagelist_cookies[-1])

    def test_b23_short_link_is_expanded_before_the_api_calls(self):
        fake = FakeBilibili()

        def handler(request):
            if request.url.host == "b23.tv":
                fake.order.append("b23.tv")
                return httpx.Response(302, headers={"location": URL})
            return fake(request)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        fetch_bilibili_metadata("https://b23.tv/abc123", client=client)
        self.assertEqual(fake.order[0], "b23.tv")
        self.assertEqual(fake.calls["/x/player/pagelist"], 1)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class TestDownloadEndToEnd(BilibiliTestCase):
    """Fake Bilibili API and CDN, real ffmpeg muxing of generated media."""

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

    def _fake(self) -> FakeBilibili:
        return FakeBilibili(
            media={
                "v": Path(self.video).read_bytes(),
                "a": Path(self.audio).read_bytes(),
            }
        )

    def test_merges_tracks_into_one_mp4_and_cleans_up(self):
        out_dir = tempfile.mkdtemp(prefix="bili-out-")
        self.addCleanup(shutil.rmtree, out_dir, True)
        path = download_native_video(URL, out_dir, client=self._fake().client())
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

    def test_stored_video_is_served_from_the_cache_without_api_calls(self):
        first_dir = tempfile.mkdtemp(prefix="bili-out-")
        second_dir = tempfile.mkdtemp(prefix="bili-out-")
        self.addCleanup(shutil.rmtree, first_dir, True)
        self.addCleanup(shutil.rmtree, second_dir, True)
        first = download_native_video(URL, first_dir, client=self._fake().client())
        store_video(URL, first)

        second = cached_video(URL, second_dir)

        self.assertEqual(Path(first).read_bytes(), Path(second).read_bytes())

    def test_expired_cache_entry_is_not_served(self):
        out_dir = tempfile.mkdtemp(prefix="bili-out-")
        self.addCleanup(shutil.rmtree, out_dir, True)
        store_video(
            URL, download_native_video(URL, out_dir, client=self._fake().client())
        )
        cached = os.path.join(self.cache_dir, "BV1aMEj62EdA-p1.mp4")
        old = time.time() - bilibili.CACHE_TTL - 60
        os.utime(cached, (old, old))

        self.assertIsNone(cached_video(URL, out_dir))

    def test_cache_evicts_the_oldest_entry_when_over_the_size_cap(self):
        out_dir = tempfile.mkdtemp(prefix="bili-out-")
        self.addCleanup(shutil.rmtree, out_dir, True)
        other = "https://www.bilibili.com/video/BV1kh4y1Y7Ko"
        first = download_native_video(URL, out_dir, client=self._fake().client())
        store_video(URL, first)
        size = os.path.getsize(first)
        second = download_native_video(other, out_dir, client=self._fake().client())
        # The cap fits one stored video but not two, so the older one must go.
        with patch.object(bilibili, "CACHE_MAX_BYTES", size + size // 2):
            store_video(other, second)
        names = sorted(os.listdir(self.cache_dir))
        self.assertEqual(names, ["BV1kh4y1Y7Ko-p1.mp4"])


class TestCanonicalUrl(unittest.TestCase):
    def test_video_links_keep_only_the_id_and_part(self):
        self.assertEqual(
            canonical_url("https://bilibili.com/video/BV1aMEj62EdA/?p=1&spm_id_from=x"),
            "https://www.bilibili.com/video/BV1aMEj62EdA",
        )
        self.assertEqual(
            canonical_url("https://www.bilibili.com/video/BV1aMEj62EdA?p=2&t=5"),
            "https://www.bilibili.com/video/BV1aMEj62EdA?p=2",
        )

    def test_av_links_stay_av_links(self):
        self.assertEqual(
            canonical_url("https://www.bilibili.com/video/av170001"),
            "https://www.bilibili.com/video/av170001",
        )


class TestLimitsAndCacheRules(BilibiliTestCase):
    def test_oversize_video_is_not_cached(self):
        out_dir = tempfile.mkdtemp(prefix="bili-out-")
        self.addCleanup(shutil.rmtree, out_dir, True)
        path = os.path.join(out_dir, "BV1aMEj62EdA.mp4")
        with open(path, "wb") as f:
            f.write(b"x" * 64)
        with patch.object(bilibili, "CACHE_MAX_BYTES", 16):
            store_video(URL, path)
        self.assertEqual(os.listdir(self.cache_dir), [])

    def test_stale_partial_files_are_removed_on_the_next_store(self):
        out_dir = tempfile.mkdtemp(prefix="bili-out-")
        self.addCleanup(shutil.rmtree, out_dir, True)
        stale = os.path.join(self.cache_dir, "crashed.part")
        Path(stale).write_bytes(b"partial")
        old = time.time() - bilibili.STALE_PARTIAL_SECONDS - 60
        os.utime(stale, (old, old))
        path = os.path.join(out_dir, "BV1aMEj62EdA.mp4")
        Path(path).write_bytes(b"video")
        store_video(URL, path)
        self.assertFalse(os.path.exists(stale))
        self.assertEqual(os.listdir(self.cache_dir), ["BV1aMEj62EdA-p1.mp4"])

    def test_cache_owned_by_another_user_is_not_used(self):
        out_dir = tempfile.mkdtemp(prefix="bili-out-")
        self.addCleanup(shutil.rmtree, out_dir, True)
        path = os.path.join(out_dir, "BV1aMEj62EdA.mp4")
        Path(path).write_bytes(b"video")
        store_video(URL, path)
        with patch.object(bilibili.os, "getuid", return_value=os.getuid() + 1):
            self.assertIsNone(cached_video(URL, out_dir))
        self.assertIsNotNone(cached_video(URL, out_dir))

    def test_stream_over_the_size_cap_is_refused(self):
        out_dir = tempfile.mkdtemp(prefix="bili-out-")
        self.addCleanup(shutil.rmtree, out_dir, True)
        fake = FakeBilibili(media={"v": b"x" * 100, "a": b"y" * 4})
        with patch.object(bilibili, "MAX_STREAM_BYTES", 10):
            with self.assertRaisesRegex(BilibiliError, "larger than the 2 GiB limit"):
                download_native_video(URL, out_dir, client=fake.client())

    def test_stream_past_the_time_limit_is_refused(self):
        out_dir = tempfile.mkdtemp(prefix="bili-out-")
        self.addCleanup(shutil.rmtree, out_dir, True)
        fake = FakeBilibili(media={"v": b"x" * 100, "a": b"y" * 4})
        with (
            patch.object(bilibili, "STREAM_TIMEOUT_SECONDS", -1.0),
            self.assertRaisesRegex(BilibiliError, "timed out"),
        ):
            download_native_video(URL, out_dir, client=fake.client())


if __name__ == "__main__":
    unittest.main()
