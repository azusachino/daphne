import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import io
from daphne.twitter import (
    article_media_lists,
    article_preview_text,
    contains_twitter_link,
    extract_twitter_link,
    handle_twitter_links,
    send_media_group_helper,
    select_twitter_video_url,
)


class TestTwitterExtraction(unittest.TestCase):
    def test_article_preview_uses_passages_and_truncates(self):
        preview, truncated = article_preview_text(
            {
                "title": "Ignored title",
                "preview_text": "fallback preview",
                "content": {
                    "blocks": [
                        {"type": "header-one", "text": "Ignored title"},
                        {"type": "unstyled", "text": "First passage."},
                        {"type": "atomic", "text": "media placeholder"},
                        {"type": "unstyled", "text": "x" * 800},
                    ]
                },
            }
        )

        self.assertTrue(truncated)
        self.assertIn("First passage.", preview)
        self.assertNotIn("media placeholder", preview)
        self.assertLessEqual(len(preview), 650)

    def test_article_media_lists_supports_images_videos_and_gifs(self):
        photos, videos, gifs = article_media_lists(
            {
                "media_entities": [
                    {
                        "media_info": {
                            "__typename": "ApiImage",
                            "original_img_url": "https://pbs.twimg.com/1.jpg",
                        }
                    },
                    {
                        "media_info": {
                            "__typename": "ApiVideo",
                            "variants": [
                                {
                                    "url": "https://video.twimg.com/live.m3u8",
                                    "bitrate": 1,
                                },
                                {
                                    "url": "https://video.twimg.com/low.mp4",
                                    "bitrate": 1,
                                },
                                {
                                    "url": "https://video.twimg.com/high.mp4",
                                    "bitrate": 2,
                                },
                            ],
                        }
                    },
                    {
                        "media_info": {
                            "__typename": "ApiGif",
                            "variants": [{"url": "https://video.twimg.com/gif.mp4"}],
                        }
                    },
                ]
            }
        )

        self.assertEqual(photos, ["https://pbs.twimg.com/1.jpg"])
        self.assertEqual(videos, ["https://video.twimg.com/high.mp4"])
        self.assertEqual(gifs, ["https://video.twimg.com/gif.mp4"])

    def test_contains_twitter_link(self):
        self.assertTrue(
            contains_twitter_link("Check this out: https://twitter.com/jack/status/20")
        )
        self.assertTrue(
            contains_twitter_link("Check this out: https://x.com/jack/status/20?s=19")
        )
        self.assertTrue(
            contains_twitter_link(
                "Check this out: https://fxtwitter.com/jack/status/20"
            )
        )
        self.assertTrue(
            contains_twitter_link(
                "Check this out: https://vxtwitter.com/jack/status/20"
            )
        )
        self.assertTrue(
            contains_twitter_link("Check this out: https://fixupx.com/jack/status/20")
        )
        self.assertTrue(
            contains_twitter_link("Check this out: https://www.x.com/jack/status/20")
        )
        self.assertFalse(
            contains_twitter_link("Check this out: https://google.com/jack/status/20")
        )
        self.assertFalse(
            contains_twitter_link("Check this out: https://twitter.com/jack")
        )

    def test_extract_twitter_link(self):
        res = extract_twitter_link(
            "Check this out: https://twitter.com/jack/status/20?s=20"
        )
        self.assertIsNotNone(res)
        domain, username, tweet_id = res
        self.assertEqual(domain, "twitter.com")
        self.assertEqual(username, "jack")
        self.assertEqual(tweet_id, "20")

        res = extract_twitter_link("https://x.com/some_user/status/1234567890")
        self.assertIsNotNone(res)
        domain, username, tweet_id = res
        self.assertEqual(domain, "x.com")
        self.assertEqual(username, "some_user")
        self.assertEqual(tweet_id, "1234567890")

    def test_select_twitter_video_url_uses_bounded_h264_rendition(self):
        video = {
            "url": "https://video.twimg.com/vid/avc1/2160x3840/high.mp4?tag=29",
            "formats": [
                {
                    "url": "https://video.twimg.com/vid/avc1/480x852/low.mp4?tag=29",
                    "container": "mp4",
                    "codec": "h264",
                    "bitrate": 640000,
                },
                {
                    "url": "https://video.twimg.com/vid/avc1/720x1280/safe.mp4?tag=29",
                    "container": "mp4",
                    "codec": "h264",
                    "bitrate": 1280000,
                },
                {
                    "url": "https://video.twimg.com/vid/avc1/2160x3840/high.mp4?tag=29",
                    "container": "mp4",
                    "codec": "h264",
                    "bitrate": 4000000,
                },
            ],
        }

        self.assertEqual(
            select_twitter_video_url(video),
            "https://video.twimg.com/vid/avc1/720x1280/safe.mp4?tag=29",
        )


class TestTwitterHandler(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.update = MagicMock()
        self.update.message = MagicMock()
        self.update.message.chat_id = 123456
        self.update.message.reply_to_message = None
        self.update.message.is_topic_message = False
        self.update.message.delete = AsyncMock()

        self.user = MagicMock()
        self.user.username = "test_user"
        self.user.full_name = "Test User Full Name"
        self.update.effective_user = self.user

        self.context = MagicMock()
        self.context.bot = MagicMock()
        self.context.bot.send_photo = AsyncMock()
        self.context.bot.send_video = AsyncMock()
        self.context.bot.send_animation = AsyncMock()
        self.context.bot.send_media_group = AsyncMock()
        self.context.bot.send_message = AsyncMock()

    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_handle_no_media_sends_html_message(self, mock_get):
        # API succeeds, but tweet has no media
        self.update.message.text = "Here: https://x.com/jack/status/20"

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 200,
            "tweet": {
                "id": "20",
                "text": "just setting up my twttr",
                "url": "https://x.com/jack/status/20",
                "author": {"screen_name": "jack", "name": "jack"},
                "media": {"photos": [], "videos": []},
            },
        }
        mock_get.return_value = mock_response

        await handle_twitter_links(self.update, self.context)

        # It should send an HTML text message, not a bare fxtwitter URL
        self.context.bot.send_message.assert_called_once()
        _, kwargs = self.context.bot.send_message.call_args
        self.assertEqual(kwargs["chat_id"], 123456)
        self.assertEqual(kwargs["parse_mode"], "HTML")
        self.assertIn("just setting up my twttr", kwargs["text"])
        self.assertIn("https://x.com/jack/status/20", kwargs["text"])
        self.update.message.delete.assert_called_once()

    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_handle_single_photo_success(self, mock_get):
        self.update.message.text = "Check: https://twitter.com/nasa/status/999"

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 200,
            "tweet": {
                "id": "999",
                "text": "Beautiful space view #NASA #Hubble",
                "author": {"screen_name": "nasa", "name": "NASA"},
                "media": {
                    "photos": [{"url": "https://pbs.twimg.com/media/test.jpg"}],
                    "videos": [],
                },
            },
        }
        mock_get.return_value = mock_response

        await handle_twitter_links(self.update, self.context)

        # Should send single photo
        self.context.bot.send_photo.assert_called_once()
        kwargs = self.context.bot.send_photo.call_args[1]
        self.assertEqual(kwargs["chat_id"], 123456)
        self.assertEqual(kwargs["photo"], "https://pbs.twimg.com/media/test.jpg")
        self.assertEqual(kwargs["parse_mode"], "HTML")
        self.assertIn("Beautiful space view #NASA #Hubble", kwargs["caption"])
        self.assertIn("#twitter #nasa #hubble", kwargs["caption"])
        self.assertIn("via @test_user", kwargs["caption"])
        self.assertIn("https://twitter.com/nasa/status/999", kwargs["caption"])
        self.update.message.delete.assert_called_once()

    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_handle_multi_photo_success(self, mock_get):
        self.update.message.text = "https://x.com/nasa/status/999"

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 200,
            "tweet": {
                "id": "999",
                "text": "Check these photos",
                "author": {"screen_name": "nasa", "name": "NASA"},
                "media": {
                    "photos": [
                        {"url": "https://pbs.twimg.com/media/1.jpg"},
                        {"url": "https://pbs.twimg.com/media/2.jpg"},
                    ],
                    "videos": [],
                },
            },
        }
        mock_get.return_value = mock_response

        await handle_twitter_links(self.update, self.context)

        # Should send media group
        self.context.bot.send_media_group.assert_called_once()
        kwargs = self.context.bot.send_media_group.call_args[1]
        self.assertEqual(kwargs["chat_id"], 123456)
        media_group = kwargs["media"]
        self.assertEqual(len(media_group), 2)
        self.assertEqual(media_group[0].media, "https://pbs.twimg.com/media/1.jpg")
        self.assertEqual(media_group[1].media, "https://pbs.twimg.com/media/2.jpg")
        self.assertIn("Check these photos", media_group[0].caption)
        self.update.message.delete.assert_called_once()

    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_handle_video_success(self, mock_get):
        self.update.message.text = "https://x.com/nasa/status/999"

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 200,
            "tweet": {
                "id": "999",
                "text": "Rocket launch video",
                "author": {"screen_name": "nasa", "name": "NASA"},
                "media": {
                    "photos": [],
                    "videos": [
                        {
                            "url": "https://video.twimg.com/vid/avc1/2160x3840/high.mp4?tag=29",
                            "type": "video",
                            "formats": [
                                {
                                    "url": "https://video.twimg.com/vid/avc1/720x1280/safe.mp4?tag=29",
                                    "container": "mp4",
                                    "codec": "h264",
                                    "bitrate": 1280000,
                                },
                                {
                                    "url": "https://video.twimg.com/vid/avc1/2160x3840/high.mp4?tag=29",
                                    "container": "mp4",
                                    "codec": "h264",
                                    "bitrate": 4000000,
                                },
                            ],
                        }
                    ],
                },
            },
        }
        mock_get.return_value = mock_response

        await handle_twitter_links(self.update, self.context)

        # Should send video
        self.context.bot.send_video.assert_called_once()
        kwargs = self.context.bot.send_video.call_args[1]
        self.assertEqual(kwargs["chat_id"], 123456)
        self.assertEqual(
            kwargs["video"],
            "https://video.twimg.com/vid/avc1/720x1280/safe.mp4?tag=29",
        )
        self.update.message.delete.assert_called_once()

    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_handle_mixed_media_sends_photos_and_video(self, mock_get):
        self.update.message.text = "https://x.com/nasa/status/999"

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 200,
            "tweet": {
                "id": "999",
                "text": "Mixed media #NASA",
                "url": "https://twitter.com/nasa/status/999",
                "media": {
                    "all": [
                        {"url": "https://pbs.twimg.com/media/1.jpg", "type": "photo"},
                        {"url": "https://pbs.twimg.com/media/2.jpg", "type": "photo"},
                        {"url": "https://video.twimg.com/test.mp4", "type": "video"},
                    ]
                },
            },
        }
        mock_get.return_value = mock_response

        await handle_twitter_links(self.update, self.context)

        self.context.bot.send_media_group.assert_called_once()
        self.context.bot.send_video.assert_called_once()
        media_group = self.context.bot.send_media_group.call_args[1]["media"]
        self.assertIn("Mixed media #NASA", media_group[0].caption)
        self.assertEqual(
            self.context.bot.send_video.call_args[1]["video"],
            "https://video.twimg.com/test.mp4",
        )
        self.assertEqual(self.context.bot.send_video.call_args[1]["caption"], "")
        self.update.message.delete.assert_called_once()

    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_handle_gif_success(self, mock_get):
        self.update.message.text = "https://x.com/nasa/status/999"

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 200,
            "tweet": {
                "id": "999",
                "text": "Rocket launch gif",
                "author": {"screen_name": "nasa", "name": "NASA"},
                "media": {
                    "photos": [],
                    "videos": [
                        {"url": "https://video.twimg.com/test.gif", "type": "gif"}
                    ],
                },
            },
        }
        mock_get.return_value = mock_response

        await handle_twitter_links(self.update, self.context)

        # Should send animation
        self.context.bot.send_animation.assert_called_once()
        kwargs = self.context.bot.send_animation.call_args[1]
        self.assertEqual(kwargs["chat_id"], 123456)
        self.assertEqual(kwargs["animation"], "https://video.twimg.com/test.gif")
        self.update.message.delete.assert_called_once()

    @patch("daphne.twitter.logger.warning")
    @patch("daphne.twitter.httpx.AsyncClient.get")
    @patch("daphne.twitter.download_bytes")
    async def test_handle_media_send_by_url_fails_downloads_bytes(
        self, mock_download, mock_get, mock_log_warn
    ):
        self.update.message.text = "https://x.com/nasa/status/999"

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 200,
            "tweet": {
                "id": "999",
                "text": "A photo",
                "author": {"screen_name": "nasa", "name": "NASA"},
                "media": {
                    "photos": [{"url": "https://pbs.twimg.com/media/test.jpg"}],
                    "videos": [],
                },
            },
        }
        mock_get.return_value = mock_response

        # First call to send_photo raises exception, second succeeds
        self.context.bot.send_photo.side_effect = [
            Exception("Failed to send by URL"),
            None,
        ]
        mock_download.return_value = b"image_data"

        await handle_twitter_links(self.update, self.context)

        # Verified that send_photo is called twice
        self.assertEqual(self.context.bot.send_photo.call_count, 2)
        # First call tried with URL
        self.assertEqual(
            self.context.bot.send_photo.call_args_list[0][1]["photo"],
            "https://pbs.twimg.com/media/test.jpg",
        )
        # Second call tried with downloaded BytesIO
        photo_arg = self.context.bot.send_photo.call_args_list[1][1]["photo"]
        self.assertTrue(isinstance(photo_arg, io.BytesIO))
        self.assertEqual(photo_arg.getvalue(), b"image_data")
        self.assertEqual(photo_arg.name, "photo.jpg")

        mock_download.assert_called_once_with("https://pbs.twimg.com/media/test.jpg")
        self.update.message.delete.assert_called_once()

    @patch("daphne.twitter.download_bytes", return_value=b"image_data")
    async def test_media_group_fallback_uses_aiogram_input_files(self, mock_download):
        bot = MagicMock()
        bot.send_media_group = AsyncMock(side_effect=[Exception("URL rejected"), None])

        await send_media_group_helper(
            bot,
            123456,
            ["https://pbs.twimg.com/media/1.jpg"],
            "caption",
            "HTML",
        )

        media = bot.send_media_group.call_args_list[1].kwargs["media"]
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0].media.filename, "photo_0.jpg")
        mock_download.assert_called_once_with("https://pbs.twimg.com/media/1.jpg")

    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_handle_single_photo_uses_api_author_not_url_username(self, mock_get):
        # URL says "nasa", but X redirects any username segment to the tweet
        # by ID regardless of what's typed - the API's author record is
        # authoritative, and can differ (renamed handle, wrong guess, etc).
        self.update.message.text = "https://twitter.com/nasa/status/999"

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 200,
            "tweet": {
                "id": "999",
                "text": "Beautiful space view",
                "author": {"screen_name": "NASAHubble", "name": "Hubble"},
                "media": {
                    "photos": [{"url": "https://pbs.twimg.com/media/test.jpg"}],
                    "videos": [],
                },
            },
        }
        mock_get.return_value = mock_response

        await handle_twitter_links(self.update, self.context)

        kwargs = self.context.bot.send_photo.call_args[1]
        self.assertIn("#twitter #nasahubble", kwargs["caption"])

    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_handle_no_author_skips_author_hashtag(self, mock_get):
        self.update.message.text = "https://x.com/jack/status/20"

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 200,
            "tweet": {
                "id": "20",
                "text": "no author record at all",
                "media": {"photos": [], "videos": []},
            },
        }
        mock_get.return_value = mock_response

        await handle_twitter_links(self.update, self.context)

        _, kwargs = self.context.bot.send_message.call_args
        # No author record from the API -> no author hashtag at all. No
        # fallback to the URL's username ("jack") for the tag.
        self.assertNotIn("#jack", kwargs["text"])
        self.assertIn("#twitter", kwargs["text"])

    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_handle_author_hashtag_not_duplicated_with_text_hashtag(
        self, mock_get
    ):
        # X handles can't contain spaces/non-ASCII (that edge case is covered
        # for Instagram/YouTube uploader display names instead, which can).
        # The Twitter-specific edge case is dedup: the author's own handle
        # already appearing as a hashtag in the tweet text shouldn't be
        # added twice.
        self.update.message.text = "https://x.com/nasa/status/999"

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 200,
            "tweet": {
                "id": "999",
                "text": "Check this out #NASA",
                "author": {"screen_name": "nasa", "name": "NASA"},
                "media": {"photos": [], "videos": []},
            },
        }
        mock_get.return_value = mock_response

        await handle_twitter_links(self.update, self.context)

        _, kwargs = self.context.bot.send_message.call_args
        self.assertIn("#twitter #nasa", kwargs["text"])
        self.assertEqual(kwargs["text"].count("#nasa"), 1)

    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_handle_article_sends_cover_photo_with_title_and_preview(
        self, mock_get
    ):
        # X Articles (long-form posts) carry an empty `text` (just the t.co
        # link) and `media: null` — content lives under `article` instead.
        self.update.message.text = (
            "https://x.com/waterloo_intern/status/2081762065392541951"
        )

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 200,
            "tweet": {
                "id": "2081762065392541951",
                "text": "",
                "url": "https://x.com/waterloo_intern/status/2081762065392541951",
                "author": {"screen_name": "waterloo_intern", "name": "ali"},
                "media": None,
                "article": {
                    "title": "22580: From GPT2 to Kimi3, Explained",
                    "preview_text": "Twenty-two thousand five hundred and eighty...",
                    "cover_media": {
                        "media_info": {
                            "original_img_url": "https://pbs.twimg.com/media/HOPJVdUb0AEabce.jpg"
                        }
                    },
                },
            },
        }
        mock_get.return_value = mock_response

        await handle_twitter_links(self.update, self.context)

        self.context.bot.send_photo.assert_called_once()
        kwargs = self.context.bot.send_photo.call_args[1]
        self.assertEqual(kwargs["chat_id"], 123456)
        self.assertEqual(
            kwargs["photo"], "https://pbs.twimg.com/media/HOPJVdUb0AEabce.jpg"
        )
        self.assertIn("22580: From GPT2 to Kimi3, Explained", kwargs["caption"])
        self.assertIn("Twenty-two thousand five hundred and eighty", kwargs["caption"])
        self.assertIn(
            "https://x.com/waterloo_intern/status/2081762065392541951",
            kwargs["caption"],
        )
        self.context.bot.send_message.assert_not_called()
        self.update.message.delete.assert_called_once()

    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_handle_article_with_promotional_tweet_text(self, mock_get):
        self.update.message.text = (
            "https://x.com/reactiverobot/status/2092638003789439075?s=20"
        )

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 200,
            "tweet": {
                "id": "2092638003789439075",
                "text": (
                    "Here's my advice for engineers looking to up their design game. "
                    "https://x.com/i/article/2092403772534460416"
                ),
                "url": "https://x.com/reactiverobot/status/2092638003789439075",
                "author": {"screen_name": "reactiverobot", "name": "Reactive Robot"},
                "media": None,
                "article": {
                    "title": "How I Design with AI.",
                    "preview_text": (
                        "As an engineer who is not a designer and hates slop."
                    ),
                    "cover_media": {
                        "media_info": {
                            "original_img_url": "https://pbs.twimg.com/media/HQqK_LMaEAAUXxC.jpg"
                        }
                    },
                },
            },
        }
        mock_get.return_value = mock_response

        await handle_twitter_links(self.update, self.context)

        self.context.bot.send_photo.assert_called_once()
        kwargs = self.context.bot.send_photo.call_args.kwargs
        self.assertIn("How I Design with AI.", kwargs["caption"])
        self.assertIn("As an engineer who is not a designer", kwargs["caption"])
        self.assertNotIn("Here's my advice for engineers", kwargs["caption"])
        self.context.bot.send_message.assert_not_called()

    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_handle_article_sends_cover_and_first_three_media(self, mock_get):
        self.update.message.text = "https://x.com/writer/status/999"

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 200,
            "tweet": {
                "text": "https://x.com/i/article/123",
                "url": "https://x.com/writer/status/999",
                "author": {"screen_name": "writer"},
                "media": None,
                "article": {
                    "title": "A visual article",
                    "preview_text": "A short fallback.",
                    "content": {"blocks": [{"type": "unstyled", "text": "The body."}]},
                    "cover_media": {
                        "media_info": {
                            "original_img_url": "https://pbs.twimg.com/cover.jpg"
                        }
                    },
                    "media_entities": [
                        {
                            "media_info": {
                                "__typename": "ApiImage",
                                "original_img_url": f"https://pbs.twimg.com/{i}.jpg",
                            }
                        }
                        for i in range(1, 5)
                    ],
                },
            },
        }
        mock_get.return_value = mock_response

        await handle_twitter_links(self.update, self.context)

        self.context.bot.send_media_group.assert_called_once()
        media = self.context.bot.send_media_group.call_args.kwargs["media"]
        self.assertEqual(
            [item.media for item in media],
            [
                "https://pbs.twimg.com/cover.jpg",
                "https://pbs.twimg.com/1.jpg",
                "https://pbs.twimg.com/2.jpg",
                "https://pbs.twimg.com/3.jpg",
            ],
        )
        self.assertIn("+1 media omitted", media[0].caption)
        self.assertIn("The body.", media[0].caption)
        self.update.message.delete.assert_called_once()

    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_handle_article_without_cover_sends_text_message(self, mock_get):
        self.update.message.text = (
            "https://x.com/waterloo_intern/status/2081762065392541951"
        )

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 200,
            "tweet": {
                "id": "2081762065392541951",
                "text": "",
                "url": "https://x.com/waterloo_intern/status/2081762065392541951",
                "author": {"screen_name": "waterloo_intern", "name": "ali"},
                "media": None,
                "article": {
                    "title": "A cover-less article",
                    "preview_text": "No cover image here.",
                },
            },
        }
        mock_get.return_value = mock_response

        await handle_twitter_links(self.update, self.context)

        self.context.bot.send_photo.assert_not_called()
        self.context.bot.send_message.assert_called_once()
        kwargs = self.context.bot.send_message.call_args[1]
        self.assertIn("A cover-less article", kwargs["text"])
        self.assertIn("No cover image here.", kwargs["text"])
        self.update.message.delete.assert_called_once()

    @patch("daphne.twitter.logger.warning")
    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_handle_api_404_sends_fallback_url(self, mock_get, mock_log_warn):
        self.update.message.text = "https://x.com/nasa/status/999"

        # API returns 404
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        await handle_twitter_links(self.update, self.context)

        # Sends fallback URL
        self.context.bot.send_message.assert_called_once_with(
            chat_id=123456,
            text="https://fxtwitter.com/nasa/status/999",
            parse_mode=None,
        )
        self.update.message.delete.assert_called_once()

    @patch("daphne.twitter.logger.exception")
    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_handle_api_exception_sends_fallback_url(
        self, mock_get, mock_log_exc
    ):
        self.update.message.text = "https://x.com/nasa/status/999"

        # API raises connection error
        mock_get.side_effect = httpx.RequestError("Connection failed")

        await handle_twitter_links(self.update, self.context)

        # Sends fallback URL
        self.context.bot.send_message.assert_called_once_with(
            chat_id=123456,
            text="https://fxtwitter.com/nasa/status/999",
            parse_mode=None,
        )
        self.update.message.delete.assert_called_once()

    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_reply_prevents_message_deletion(self, mock_get):
        self.update.message.text = "https://x.com/jack/status/20"
        self.update.message.reply_to_message = MagicMock()  # This message is a reply

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 200,
            "tweet": {
                "id": "20",
                "text": "No media tweet",
                "author": {"screen_name": "jack", "name": "jack"},
                "media": {},
            },
        }
        mock_get.return_value = mock_response

        await handle_twitter_links(self.update, self.context)

        self.context.bot.send_message.assert_called_once()
        self.update.message.delete.assert_not_called()  # delete must not be called

    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_topic_message_prevents_message_deletion(self, mock_get):
        self.update.message.text = "https://x.com/jack/status/20"
        self.update.message.is_topic_message = True  # This is a topic message

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 200,
            "tweet": {
                "id": "20",
                "text": "No media tweet",
                "author": {"screen_name": "jack", "name": "jack"},
                "media": {},
            },
        }
        mock_get.return_value = mock_response

        await handle_twitter_links(self.update, self.context)

        self.context.bot.send_message.assert_called_once()
        self.update.message.delete.assert_not_called()  # delete must not be called


if __name__ == "__main__":
    unittest.main()
