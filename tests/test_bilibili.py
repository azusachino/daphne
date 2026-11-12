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
    RateLimitedError,
    UnsupportedError,
    cached_video,
    canonical_url,
    download_native_video,
    reason_for,
    resolve_media,
    store_video,
)
from matcha import ApiError, Reason
from matcha.errors import NeedsExpansionError

SPI = {"code": 0, "data": {"b_3": "buvid3-test", "b_4": "buvid4-test", "b_nut": "1"}}
TICKET = {"code": 0, "data": {"ticket": "ticket-test"}}
# The shape `x/web-interface/view` answers with, including each part's cid.
VIEW = {
    "code": 0,
    "data": {
        "title": "🐱躲在超市后门哈气的二人🐱",
        "duration": 117,
        "pic": "http://i2.hdslb.com/bfs/archive/cover.png",
        "owner": {"name": "初小晓Dawn"},
        "pages": [
            {"page": 1, "cid": 111, "part": "上", "duration": 109},
            {"page": 2, "cid": 222, "part": "（纯享版）", "duration": 8},
        ],
    },
}
PLAYURL = {
    "code": 0,
    "data": {
        "quality": 32,
        "dash": {
            "duration": 109,
            "video": [
                {
                    "id": 32,
                    "codecid": 7,
                    "bandwidth": 191380,
                    "baseUrl": "https://cdn.example/v.m4s",
                    "width": 852,
                    "height": 480,
                },
                {
                    "id": 32,
                    "codecid": 12,
                    "bandwidth": 300000,
                    "baseUrl": "https://cdn.example/av1.m4s",
                },
            ],
            "audio": [
                {
                    "id": 30280,
                    "bandwidth": 64000,
                    "baseUrl": "https://cdn.example/a.m4s",
                }
            ],
        },
    },
}
URL = "https://www.bilibili.com/video/BV1aMEj62EdA"


def _json(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload)


class FakeBilibili:
    """Serves the endpoints the module calls and records what it asked for."""

    def __init__(
        self,
        view_statuses=(),
        view_body=None,
        playurl_body=None,
        media=None,
        spi_status=200,
        ticket_body=None,
    ):
        self.calls = collections.Counter()
        self.view_statuses = list(view_statuses)
        self.view_body = view_body if view_body is not None else VIEW
        self.playurl_body = playurl_body if playurl_body is not None else PLAYURL
        self.view_requests = []
        self.playurl_requests = []
        self.media = media or {}
        self.spi_status = spi_status
        self.ticket_body = ticket_body if ticket_body is not None else TICKET
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
            return _json(self.ticket_body)
        if path == "/x/web-interface/view":
            self.view_requests.append(request)
            if self.view_statuses:
                status = self.view_statuses.pop(0)
                if status != 200:
                    return httpx.Response(status)
            return _json(self.view_body)
        if path == "/x/player/playurl":
            self.playurl_requests.append(request)
            return _json(self.playurl_body)
        if path == "/v.m4s" and "v" in self.media:
            return httpx.Response(200, content=self.media["v"])
        if path == "/a.m4s" and "a" in self.media:
            return httpx.Response(200, content=self.media["a"])
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def cookies_of(self, request: httpx.Request) -> str:
        return request.headers.get("cookie", "")


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


class TestIdentity(BilibiliTestCase):
    def test_canonical_url_comes_from_matcha(self):
        self.assertEqual(
            canonical_url("https://bilibili.com/video/BV1aMEj62EdA/?p=1&spm_id_from=x"),
            "https://www.bilibili.com/video/BV1aMEj62EdA",
        )
        self.assertEqual(
            canonical_url("https://www.bilibili.com/video/BV1aMEj62EdA?p=2&t=5"),
            "https://www.bilibili.com/video/BV1aMEj62EdA?p=2",
        )
        self.assertEqual(
            canonical_url("https://www.bilibili.com/video/av170001"),
            "https://www.bilibili.com/video/av170001",
        )
        self.assertEqual(
            canonical_url("https://m.bilibili.com/video/BV1aMEj62EdA?vd_source=abc"),
            "https://www.bilibili.com/video/BV1aMEj62EdA",
        )

    def test_non_video_and_bangumi_links_are_refused(self):
        with self.assertRaises(BilibiliError):
            canonical_url("https://www.bilibili.com/")
        with self.assertRaises(UnsupportedError):
            resolve_media("https://www.bilibili.com/bangumi/play/ep38979504395")

    def test_b23_short_link_is_expanded_before_any_api_call(self):
        fake = FakeBilibili()

        def handler(request):
            if request.url.host == "b23.tv":
                fake.order.append("b23.tv")
                return httpx.Response(302, headers={"location": URL})
            return fake(request)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        media = resolve_media("https://b23.tv/abc123", client=client)
        self.assertEqual(fake.order[0], "b23.tv")
        self.assertEqual(media.canonical_url, URL)
        self.assertEqual(fake.calls["/x/web-interface/view"], 1)

    def test_b23_short_link_to_non_bilibili_host_is_refused(self):
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    302, headers={"location": "https://evil.example/video"}
                )
            )
        )
        with self.assertRaisesRegex(BilibiliError, "did not resolve to bilibili.com"):
            resolve_media("https://b23.tv/abc123", client=client)


class TestMetadata(BilibiliTestCase):
    def test_view_carries_owner_cover_and_parts(self):
        fake = FakeBilibili()
        media = resolve_media(URL + "?p=2", client=fake.client())
        info = media.info
        self.assertEqual(info.title, "🐱躲在超市后门哈气的二人🐱")
        self.assertEqual(info.creator, "初小晓Dawn")
        self.assertEqual(
            info.thumbnail_url, "http://i2.hdslb.com/bfs/archive/cover.png"
        )
        self.assertEqual(info.duration_s, 8)  # the selected part, not the whole video
        self.assertTrue(info.is_multi_part)
        self.assertEqual(info.parts[1].title, "（纯享版）")
        self.assertEqual(info.canonical_url, URL + "?p=2")
        self.assertEqual(media.canonical_url, URL + "?p=2")

    def test_absent_fields_stay_absent(self):
        body = {"code": 0, "data": {"title": "t", "pages": [{"page": 1, "cid": 5}]}}
        fake = FakeBilibili(view_body=body)
        info = resolve_media(URL, client=fake.client()).info
        self.assertIsNone(info.creator)
        self.assertIsNone(info.thumbnail_url)
        self.assertIsNone(info.duration_s)

    def test_part_out_of_range_is_reported(self):
        fake = FakeBilibili()
        with self.assertRaisesRegex(BilibiliError, "has 2 parts, not 9"):
            resolve_media(URL + "?p=9", client=fake.client())


class TestPlan(BilibiliTestCase):
    def test_plan_prices_the_download_before_any_byte_moves(self):
        fake = FakeBilibili()
        plan = resolve_media(URL + "?p=2", client=fake.client()).plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.video.url, "https://cdn.example/v.m4s")  # H.264 wins
        self.assertEqual(plan.audio.url, "https://cdn.example/a.m4s")
        self.assertEqual(plan.estimated_bytes, int((191380 + 64000) * 109 / 8))
        self.assertEqual(plan.quality, "480p")
        self.assertTrue(plan.needs_merge)
        self.assertEqual(plan.headers["Referer"], "https://www.bilibili.com/")

    def test_playurl_asks_for_dash_with_the_selected_parts_cid(self):
        fake = FakeBilibili()
        resolve_media(URL + "?p=2", client=fake.client())
        request = fake.playurl_requests[0]
        self.assertEqual(request.url.params["fnval"], "4048")
        self.assertEqual(request.url.params["cid"], "222")

    def test_unpriced_plan_is_not_a_failure(self):
        # playurl refuses, but the metadata was read: the caller shows a card.
        fake = FakeBilibili(playurl_body={"code": -404, "message": "啥都木有"})
        media = resolve_media(URL, client=fake.client())
        self.assertIsNone(media.plan)
        self.assertEqual(media.info.creator, "初小晓Dawn")

    def test_a_view_without_a_cid_leaves_the_plan_unpriced(self):
        body = {"code": 0, "data": {"title": "t", "pages": [{"page": 1, "part": "a"}]}}
        fake = FakeBilibili(view_body=body)
        media = resolve_media(URL, client=fake.client())
        self.assertIsNone(media.plan)
        self.assertEqual(fake.calls["/x/player/playurl"], 0)


class TestVisitorSession(BilibiliTestCase):
    def test_view_and_playurl_carry_the_visitor_cookies(self):
        fake = FakeBilibili()
        resolve_media(URL, client=fake.client())
        for request in (*fake.view_requests, *fake.playurl_requests):
            cookies = fake.cookies_of(request)
            self.assertIn("buvid3=buvid3-test", cookies)
            self.assertIn("buvid4=buvid4-test", cookies)
            self.assertIn("bili_ticket=ticket-test", cookies)

    def test_session_is_reused_within_the_ttl_and_refreshed_after_it(self):
        fake = FakeBilibili()
        resolve_media(URL, client=fake.client())
        resolve_media(URL, client=fake.client())
        self.assertEqual(fake.calls["/x/frontend/finger/spi"], 1)
        bilibili._session["at"] -= SESSION_TTL + 1
        resolve_media(URL, client=fake.client())
        self.assertEqual(fake.calls["/x/frontend/finger/spi"], 2)

    def test_failed_visitor_setup_does_not_block_the_api_calls(self):
        fake = FakeBilibili(spi_status=500)
        media = resolve_media(URL, client=fake.client())
        self.assertEqual(media.info.title, VIEW["data"]["title"])
        self.assertIsNone(bilibili._session["cookies"])

    def test_failed_ticket_leaves_only_the_spi_cookies(self):
        fake = FakeBilibili(ticket_body={"code": -1, "message": "refused"})
        resolve_media(URL, client=fake.client())
        cookies = fake.cookies_of(fake.view_requests[0])
        self.assertIn("buvid3=buvid3-test", cookies)
        self.assertNotIn("bili_ticket", cookies)


class TestRiskControl(BilibiliTestCase):
    def test_412_is_retried_with_a_fresh_session_until_it_clears(self):
        fake = FakeBilibili(view_statuses=[412, 412, 200])
        with patch("daphne.bilibili.time.sleep") as sleep:
            media = resolve_media(URL, client=fake.client())
        self.assertEqual(media.info.creator, "初小晓Dawn")
        self.assertEqual(fake.calls["/x/web-interface/view"], 3)
        self.assertEqual(sleep.call_count, 2)
        # One session for the first attempt, one fresh session after the first refusal.
        self.assertEqual(fake.calls["/x/frontend/finger/spi"], 2)

    def test_gives_up_after_all_retries(self):
        fake = FakeBilibili(view_statuses=[412] * 10)
        with patch("daphne.bilibili.time.sleep"):
            with self.assertRaisesRegex(RateLimitedError, "rate-limiting"):
                resolve_media(URL, client=fake.client())

    def test_a_platform_code_is_not_retried(self):
        fake = FakeBilibili(view_body={"code": -10403, "message": "premium"})
        with patch("daphne.bilibili.time.sleep") as sleep:
            with self.assertRaises(ApiError):
                resolve_media(URL, client=fake.client())
        sleep.assert_not_called()


class TestReasonVocabulary(unittest.TestCase):
    def test_platform_codes_map_to_shared_reasons(self):
        self.assertIs(reason_for(ApiError(-404, "no")), Reason.NOT_FOUND)
        self.assertIs(reason_for(ApiError(-403, "no")), Reason.AUTH_REQUIRED)
        self.assertIs(reason_for(ApiError(-10403, "no")), Reason.PREMIUM_ONLY)
        self.assertIs(reason_for(ApiError(-412, "no")), Reason.RATE_LIMITED)
        self.assertIs(reason_for(ApiError(9999, "no")), Reason.EXTRACTOR_FAILED)

    def test_our_own_failures_use_the_same_vocabulary(self):
        self.assertIs(reason_for(RateLimitedError("x")), Reason.RATE_LIMITED)
        self.assertIs(reason_for(UnsupportedError("x")), Reason.AUTH_REQUIRED)
        self.assertIs(reason_for(BilibiliError("x")), Reason.EXTRACTOR_FAILED)
        self.assertIs(reason_for(NeedsExpansionError("x")), Reason.EXTRACTOR_FAILED)
        self.assertIs(reason_for(httpx.ConnectError("x")), Reason.NETWORK)


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

    def test_the_cache_keys_one_entry_per_part(self):
        out_dir = tempfile.mkdtemp(prefix="bili-out-")
        self.addCleanup(shutil.rmtree, out_dir, True)
        store_video(
            URL, download_native_video(URL, out_dir, client=self._fake().client())
        )
        other_dir = tempfile.mkdtemp(prefix="bili-out-")
        self.addCleanup(shutil.rmtree, other_dir, True)
        store_video(
            URL + "?p=2",
            download_native_video(
                URL + "?p=2", other_dir, client=self._fake().client()
            ),
        )
        self.assertEqual(
            sorted(os.listdir(self.cache_dir)),
            ["bilibili-BV1aMEj62EdA-p2.mp4", "bilibili-BV1aMEj62EdA.mp4"],
        )
        # Each part resolves to its own entry, so part one is never served part two.
        self.assertIsNotNone(cached_video(URL, other_dir))
        self.assertIsNotNone(cached_video(URL + "?p=2", other_dir))

    def test_expired_cache_entry_is_not_served(self):
        out_dir = tempfile.mkdtemp(prefix="bili-out-")
        self.addCleanup(shutil.rmtree, out_dir, True)
        store_video(
            URL, download_native_video(URL, out_dir, client=self._fake().client())
        )
        cached = os.path.join(self.cache_dir, "bilibili-BV1aMEj62EdA.mp4")
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
        self.assertEqual(names, ["bilibili-BV1kh4y1Y7Ko.mp4"])


class TestLimitsAndCacheRules(BilibiliTestCase):
    def test_a_store_leaves_no_temp_file_behind(self):
        out_dir = tempfile.mkdtemp(prefix="bili-out-")
        self.addCleanup(shutil.rmtree, out_dir, True)
        path = os.path.join(out_dir, "BV1aMEj62EdA.mp4")
        Path(path).write_bytes(b"video")
        store_video(URL, path)
        store_video(URL, path)
        self.assertEqual(os.listdir(self.cache_dir), ["bilibili-BV1aMEj62EdA.mp4"])

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
        self.assertEqual(os.listdir(self.cache_dir), ["bilibili-BV1aMEj62EdA.mp4"])

    def test_cache_owned_by_another_user_is_not_used(self):
        out_dir = tempfile.mkdtemp(prefix="bili-out-")
        self.addCleanup(shutil.rmtree, out_dir, True)
        path = os.path.join(out_dir, "BV1aMEj62EdA.mp4")
        Path(path).write_bytes(b"video")
        store_video(URL, path)
        with patch.object(bilibili.os, "getuid", return_value=os.getuid() + 1):
            self.assertIsNone(cached_video(URL, out_dir))
        self.assertIsNotNone(cached_video(URL, out_dir))

    def test_a_link_layer_that_cannot_name_the_work_never_fails_a_store(self):
        # store_video runs after a download already succeeded: a link matcha cannot
        # canonicalise may be logged, never raised.
        out_dir = tempfile.mkdtemp(prefix="bili-out-")
        self.addCleanup(shutil.rmtree, out_dir, True)
        path = os.path.join(out_dir, "x.mp4")
        Path(path).write_bytes(b"video")
        with patch.object(bilibili, "_ref", side_effect=NeedsExpansionError("opaque")):
            store_video(URL, path)
            self.assertIsNone(cached_video(URL, out_dir))

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
