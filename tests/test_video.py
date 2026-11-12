import unittest
from unittest.mock import patch

from matcha import (
    ApiError,
    Card,
    Capabilities,
    MediaInfo,
    Reason,
    StreamPlan,
    Upload,
    canonical_url,
    conformance,
    parse,
)

from daphne import bilibili, video
from daphne.messages import reason_text
from daphne.video import Unreadable, Video, canonical_link, resolve

BILI = "https://www.bilibili.com/video/BV1aMEj62EdA"
BV = "BV1aMEj62EdA"


def _info(**kwargs) -> MediaInfo:
    fields = {"platform": "bilibili", "kind": "video", "work_id": "BV1aMEj62EdA"}
    fields.update(kwargs)
    return MediaInfo(title="Cat", **fields)


def _plan(size: int) -> StreamPlan:
    return StreamPlan(
        video=None, audio=None, estimated_bytes=size, quality="480p", needs_merge=True
    )


class TestConformance(unittest.TestCase):
    """The paste shapes both bots must agree on, run in this bot's own suite."""

    def test_paste_shapes_conform(self):
        self.assertEqual(conformance.check(), [])

    def test_every_case_round_trips_through_the_canonical_url(self):
        for paste, *_ in conformance.CASES:
            ref = parse(paste)
            if ref is None:
                continue
            self.assertEqual(parse(canonical_url(ref)), ref, paste)


class TestIntake(unittest.TestCase):
    def test_part_selection_survives_and_tracking_goes(self):
        self.assertEqual(
            canonical_link(f"https://www.bilibili.com/video/{BV}/?p=2&vd_source=abc"),
            BILI + "?p=2",
        )
        self.assertEqual(
            canonical_link("https://m.bilibili.com/video/BV1aMEj62EdA?p=1"),
            BILI,
        )
        self.assertEqual(
            canonical_link("bilibili.com/video/BV1aMEj62EdA?spm_id_from=333.1007"),
            BILI,
        )

    def test_a_short_link_stays_unexpanded_until_a_worker_resolves_it(self):
        # No request may happen while a handler is formatting a message.
        self.assertEqual(
            canonical_link("https://b23.tv/abc123?share_source=copy_web"),
            "https://b23.tv/abc123",
        )

    def test_a_link_matcha_cannot_read_keeps_its_generic_form(self):
        self.assertEqual(
            canonical_link("https://www.bilibili.com/video/BV1abc/?share_source=x&p=1"),
            "https://www.bilibili.com/video/BV1abc",
        )

    def test_youtube_time_offset_survives(self):
        self.assertEqual(
            canonical_link("https://youtu.be/dQw4w9WgXcQ?t=42&si=abc"),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42",
        )


class TestResolveChain(unittest.TestCase):
    def test_bilibili_view_is_the_first_engine(self):
        media = bilibili.Media(
            ref=parse(BILI),
            canonical_url=BILI,
            info=_info(creator="初小晓Dawn"),
            plan=_plan(10 * 1024 * 1024),
        )
        with (
            patch("daphne.bilibili.resolve_media", return_value=media),
            patch("daphne.downloader.fetch_ytdlp_metadata") as ytdlp,
        ):
            result = resolve(BILI)
        ytdlp.assert_not_called()
        self.assertEqual(result.engine, "bilibili-view")
        self.assertEqual(result.info.creator, "初小晓Dawn")
        self.assertEqual(result.canonical_url, BILI)
        self.assertEqual(result.estimated_bytes, 10 * 1024 * 1024)

    def test_yt_dlp_is_the_second_engine(self):
        with (
            patch(
                "daphne.bilibili.resolve_media",
                side_effect=bilibili.BilibiliError("view failed"),
            ),
            patch(
                "daphne.downloader.fetch_ytdlp_metadata",
                return_value={
                    "title": "Cat",
                    "uploader": "初小晓Dawn",
                    "duration": 109,
                    "width": 852,
                    "height": 480,
                    # yt-dlp echoes the paste's tracking parameters back.
                    "webpage_url": BILI + "/?buvid=ABC&share_source=COPY&p=1",
                    "filesize_approx": 4_000_000,
                },
            ),
        ):
            result = resolve(BILI)
        self.assertEqual(result.engine, "yt-dlp")
        self.assertEqual(result.canonical_url, BILI)
        self.assertEqual(result.info.creator, "初小晓Dawn")
        self.assertEqual(result.info.duration_s, 109)
        self.assertEqual(result.platform, "bilibili")
        self.assertEqual(result.estimated_bytes, 4_000_000)

    def test_an_unreadable_link_reports_the_platforms_reason(self):
        with (
            patch(
                "daphne.bilibili.resolve_media",
                side_effect=ApiError(-404, "啥都木有"),
            ),
            patch("daphne.downloader.fetch_ytdlp_metadata", return_value={}),
        ):
            with self.assertRaises(Unreadable) as caught:
                resolve(BILI)
        self.assertIs(caught.exception.reason, Reason.NOT_FOUND)

    def test_risk_control_stops_the_chain_before_yt_dlp(self):
        with (
            patch(
                "daphne.bilibili.resolve_media",
                side_effect=bilibili.RateLimitedError("risk control"),
            ),
            patch("daphne.downloader.fetch_ytdlp_metadata") as ytdlp,
        ):
            with self.assertRaises(bilibili.RateLimitedError):
                resolve(BILI)
        ytdlp.assert_not_called()

    def test_youtube_is_read_by_yt_dlp_alone(self):
        with (
            patch("daphne.bilibili.resolve_media") as native,
            patch(
                "daphne.downloader.fetch_ytdlp_metadata",
                return_value={
                    "title": "Song",
                    "uploader": "Rick",
                    "duration": 213,
                    "filesize": 30_000_000,
                },
            ),
        ):
            result = resolve("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        native.assert_not_called()
        self.assertEqual(result.platform, "youtube")
        self.assertEqual(
            result.canonical_url, "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )

    def test_an_unknown_size_leaves_the_plan_unpriced(self):
        with patch(
            "daphne.downloader.fetch_ytdlp_metadata",
            return_value={"title": "Song", "uploader": "Rick"},
        ):
            result = video._resolve_with_ytdlp(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
            )
        self.assertIsNone(result.plan)
        self.assertIsNone(result.estimated_bytes)


class TestDelivery(unittest.TestCase):
    """matcha decides; daphne only supplies the policy and its own upload limit."""

    def _video(self, size):
        plan = None if size is None else _plan(size)
        return Video(ref=parse(BILI), canonical_url=BILI, info=_info(), plan=plan)

    def test_a_priced_video_that_fits_is_uploaded(self):
        caps = Capabilities(max_upload_bytes=1000)
        self.assertIsInstance(self._video(900).delivery(caps), Upload)
        self.assertEqual(self._video(2000).delivery(caps), Card(Reason.TOO_LARGE))
        self.assertEqual(self._video(None).delivery(caps), Card(Reason.NO_PLAN))
        blocked = Capabilities(max_upload_bytes=1000, preview_allowed=False)
        self.assertEqual(self._video(900).delivery(blocked), Card(Reason.NO_ACCESS))


class TestReasonText(unittest.TestCase):
    def test_every_reason_has_one_sentence(self):
        for reason in Reason:
            text = reason_text(reason)
            self.assertTrue(text)
            self.assertEqual(text, text.strip())

    def test_reasons_are_phrased_for_a_user(self):
        self.assertIn("rate-limiting", reason_text(Reason.RATE_LIMITED))
        self.assertIn("premium", reason_text(Reason.PREMIUM_ONLY))
        self.assertIn("reached", reason_text(Reason.NETWORK))


if __name__ == "__main__":
    unittest.main()
