import unittest
from unittest.mock import patch, MagicMock, AsyncMock

from daphne.reddit import (
    contains_reddit_link,
    extract_reddit_link,
    is_video_post,
    gallery_image_urls,
    single_image_url,
    build_caption,
    handle_reddit_links,
    fetch_post,
    resolve_share_link,
    RedditBlocked,
)


class TestReddit(unittest.IsolatedAsyncioTestCase):
    def test_contains_reddit_link(self):
        self.assertTrue(
            contains_reddit_link(
                "https://www.reddit.com/r/aww/comments/abc123/cute_cat/"
            )
        )
        self.assertTrue(contains_reddit_link("check this https://redd.it/abc123"))
        self.assertTrue(
            contains_reddit_link("https://old.reddit.com/r/aww/comments/abc123/")
        )
        self.assertTrue(
            contains_reddit_link("https://www.reddit.com/r/rickandmorty/s/7jlByn5RL5")
        )
        self.assertFalse(contains_reddit_link("https://twitter.com/user/status/123"))

    def test_extract_reddit_link(self):
        url = extract_reddit_link(
            "look https://www.reddit.com/r/aww/comments/abc123/cute_cat/ nice"
        )
        self.assertEqual(url, "https://www.reddit.com/r/aww/comments/abc123/cute_cat/")
        self.assertIsNone(extract_reddit_link("no link here"))

    def test_is_video_post(self):
        self.assertTrue(is_video_post({"is_video": True}))
        self.assertTrue(
            is_video_post({"secure_media": {"reddit_video": {"fallback_url": "x"}}})
        )
        self.assertFalse(is_video_post({"is_video": False}))
        self.assertFalse(is_video_post({}))

    def test_gallery_image_urls(self):
        post = {
            "gallery_data": {"items": [{"media_id": "a1"}, {"media_id": "a2"}]},
            "media_metadata": {
                "a1": {"s": {"u": "https://preview.redd.it/a1.jpg?a=1&amp;b=2"}},
                "a2": {"s": {"gif": "https://preview.redd.it/a2.gif?a=1&amp;b=2"}},
            },
        }
        urls = gallery_image_urls(post)
        self.assertEqual(
            urls,
            [
                "https://preview.redd.it/a1.jpg?a=1&b=2",
                "https://preview.redd.it/a2.gif?a=1&b=2",
            ],
        )

    def test_gallery_image_urls_empty(self):
        self.assertEqual(gallery_image_urls({}), [])

    def test_single_image_url_direct(self):
        post = {"url_overridden_by_dest": "https://i.redd.it/photo.png"}
        self.assertEqual(single_image_url(post), "https://i.redd.it/photo.png")

    def test_single_image_url_from_preview(self):
        post = {
            "url": "https://example.com/not-an-image-page",
            "preview": {
                "images": [
                    {"source": {"url": "https://preview.redd.it/p.jpg?a=1&amp;b=2"}}
                ]
            },
        }
        self.assertEqual(
            single_image_url(post), "https://preview.redd.it/p.jpg?a=1&b=2"
        )

    def test_single_image_url_none(self):
        self.assertIsNone(single_image_url({"url": "https://example.com/text-post"}))

    def test_build_caption(self):
        post = {
            "title": "Cute Cat",
            "subreddit_name_prefixed": "r/aww",
            "author": "someone",
        }
        caption = build_caption(
            post, "https://www.reddit.com/r/aww/comments/abc123/", "via @haru"
        )
        self.assertIn("<b>Cute Cat</b>", caption)
        self.assertIn("r/aww", caption)
        self.assertIn("u/someone", caption)
        self.assertIn("#reddit #aww", caption)
        self.assertIn("via @haru", caption)

    async def test_resolve_share_link_follows_redirect(self):
        resolved_response = MagicMock()
        resolved_response.url = (
            "https://www.reddit.com/r/aww/comments/abc123/cute_cat/?utm_source=share"
        )
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=resolved_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("daphne.reddit.httpx.AsyncClient", return_value=mock_client):
            resolved = await resolve_share_link(
                "https://www.reddit.com/r/aww/s/7jlByn5RL5"
            )

        self.assertEqual(
            resolved, "https://www.reddit.com/r/aww/comments/abc123/cute_cat"
        )

    async def test_fetch_post_resolves_share_link_before_json_fetch(self):
        json_response = MagicMock()
        json_response.status_code = 200
        json_response.json.return_value = [
            {"data": {"children": [{"data": {"title": "Cute Cat"}}]}}
        ]
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=json_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "daphne.reddit.resolve_share_link",
                new_callable=AsyncMock,
                return_value="https://www.reddit.com/r/aww/comments/abc123/cute_cat",
            ) as mock_resolve,
            patch("daphne.reddit.httpx.AsyncClient", return_value=mock_client),
        ):
            post = await fetch_post("https://www.reddit.com/r/aww/s/7jlByn5RL5")

        mock_resolve.assert_called_once()
        self.assertEqual(post, {"title": "Cute Cat"})
        called_json_url = mock_client.get.call_args[0][0]
        self.assertEqual(
            called_json_url,
            "https://www.reddit.com/r/aww/comments/abc123/cute_cat.json",
        )

    @patch("daphne.reddit.fetch_post")
    @patch("daphne.reddit.send_photos", new_callable=AsyncMock)
    @patch("daphne.reddit.try_delete_message", new_callable=AsyncMock)
    async def test_handle_reddit_links_gallery(
        self, mock_delete, mock_send_photos, mock_fetch
    ):
        mock_fetch.return_value = {
            "title": "Gallery Post",
            "subreddit_name_prefixed": "r/pics",
            "author": "poster",
            "permalink": "/r/pics/comments/abc123/gallery_post/",
            "is_video": False,
            "gallery_data": {"items": [{"media_id": "a1"}]},
            "media_metadata": {"a1": {"s": {"u": "https://preview.redd.it/a1.jpg"}}},
        }
        mock_send_photos.return_value = True

        update = MagicMock()
        update.message.text = (
            "https://www.reddit.com/r/pics/comments/abc123/gallery_post/"
        )
        update.message.chat_id = 999
        update.effective_user.username = "haru"
        update.effective_user.full_name = "Haru"

        context = MagicMock()
        context.bot.send_chat_action = AsyncMock()

        await handle_reddit_links(update, context)

        mock_send_photos.assert_called_once()
        args = mock_send_photos.call_args[0]
        self.assertEqual(args[2], ["https://preview.redd.it/a1.jpg"])
        mock_delete.assert_called_once_with(update)

    @patch("daphne.bot.handle_video_link", new_callable=AsyncMock)
    @patch("daphne.reddit.fetch_post")
    async def test_handle_reddit_links_video_delegates_to_generic_video(
        self, mock_fetch, mock_handle_video_link
    ):
        mock_fetch.return_value = {
            "title": "Video Post",
            "subreddit_name_prefixed": "r/videos",
            "author": "poster",
            "permalink": "/r/videos/comments/abc123/video_post/",
            "is_video": True,
        }

        update = MagicMock()
        update.message.text = (
            "https://www.reddit.com/r/videos/comments/abc123/video_post/"
        )
        update.message.chat_id = 999
        update.effective_user.username = "haru"
        update.effective_user.full_name = "Haru"

        context = MagicMock()
        context.bot.send_chat_action = AsyncMock()

        await handle_reddit_links(update, context)

        mock_handle_video_link.assert_called_once()
        called_url = mock_handle_video_link.call_args[0][2]
        self.assertEqual(
            called_url, "https://www.reddit.com/r/videos/comments/abc123/video_post/"
        )

    @patch("daphne.bot.handle_video_link", new_callable=AsyncMock)
    @patch("daphne.reddit.fetch_post", return_value=None)
    async def test_handle_reddit_links_unreachable_post_falls_back(
        self, mock_fetch, mock_handle_video_link
    ):
        update = MagicMock()
        update.message.text = "https://www.reddit.com/r/private/comments/abc123/x/"
        update.message.chat_id = 999
        update.effective_user.username = "haru"
        update.effective_user.full_name = "Haru"

        context = MagicMock()
        context.bot.send_chat_action = AsyncMock()

        await handle_reddit_links(update, context)

        mock_handle_video_link.assert_called_once()

    @patch("daphne.bot.handle_video_link", new_callable=AsyncMock)
    @patch("daphne.reddit.fetch_post", side_effect=RedditBlocked("status=429"))
    async def test_handle_reddit_links_blocked_fails_fast(
        self, mock_fetch, mock_handle_video_link
    ):
        update = MagicMock()
        update.message.text = "https://www.reddit.com/r/aww/comments/abc123/x/"
        update.message.chat_id = 999
        update.effective_user.username = "haru"
        update.effective_user.full_name = "Haru"

        context = MagicMock()
        context.bot.send_chat_action = AsyncMock()
        context.bot.send_message = AsyncMock()

        await handle_reddit_links(update, context)

        # Must not burn the full multi-engine video pipeline on a request
        # Reddit itself already refused — that's just retrying into the same
        # wall four more times.
        mock_handle_video_link.assert_not_called()
        context.bot.send_message.assert_called_once()
        text = context.bot.send_message.call_args[1]["text"]
        self.assertIn("rate-limiting", text)

    @patch("daphne.reddit.fetch_post")
    @patch("daphne.reddit.try_delete_message", new_callable=AsyncMock)
    async def test_handle_reddit_links_text_post_sends_link(
        self, mock_delete, mock_fetch
    ):
        mock_fetch.return_value = {
            "title": "Text Post",
            "subreddit_name_prefixed": "r/AskReddit",
            "author": "poster",
            "permalink": "/r/AskReddit/comments/abc123/text_post/",
            "is_video": False,
            "url": "https://www.reddit.com/r/AskReddit/comments/abc123/text_post/",
        }

        update = MagicMock()
        update.message.text = (
            "https://www.reddit.com/r/AskReddit/comments/abc123/text_post/"
        )
        update.message.chat_id = 999
        update.effective_user.username = "haru"
        update.effective_user.full_name = "Haru"

        context = MagicMock()
        context.bot.send_chat_action = AsyncMock()
        context.bot.send_message = AsyncMock()

        await handle_reddit_links(update, context)

        context.bot.send_message.assert_called_once()
        mock_delete.assert_called_once_with(update)


if __name__ == "__main__":
    unittest.main()
