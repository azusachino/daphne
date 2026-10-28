import unittest
from unittest.mock import patch, MagicMock, AsyncMock

from daphne.bluesky import (
    contains_bluesky_link,
    extract_bluesky_link,
    extract_media_from_embed,
    build_bluesky_caption,
    handle_bluesky_links,
)


class TestBluesky(unittest.IsolatedAsyncioTestCase):
    def test_contains_bluesky_link(self):
        self.assertTrue(
            contains_bluesky_link(
                "https://bsky.app/profile/alice.bsky.social/post/3livnrzudt22s"
            )
        )
        self.assertTrue(
            contains_bluesky_link(
                "Look at this: http://www.bsky.app/profile/bob/post/12345"
            )
        )
        self.assertFalse(contains_bluesky_link("https://twitter.com/user/status/123"))

    def test_extract_bluesky_link(self):
        res = extract_bluesky_link(
            "https://bsky.app/profile/alice.bsky.social/post/3livnrzudt22s"
        )
        self.assertEqual(res, ("alice.bsky.social", "3livnrzudt22s"))

        res_none = extract_bluesky_link("https://bsky.app/profile/alice.bsky.social")
        self.assertIsNone(res_none)

    def test_extract_media_from_embed_images(self):
        embed = {
            "$type": "app.bsky.embed.images#view",
            "images": [
                {"fullsize": "https://cdn.bsky.app/img/fullsize/1.jpg"},
                {"fullsize": "https://cdn.bsky.app/img/fullsize/2.jpg"},
            ],
        }
        photos, video = extract_media_from_embed(embed)
        self.assertEqual(
            photos,
            [
                "https://cdn.bsky.app/img/fullsize/1.jpg",
                "https://cdn.bsky.app/img/fullsize/2.jpg",
            ],
        )
        self.assertIsNone(video)

    def test_extract_media_from_embed_video(self):
        embed = {
            "$type": "app.bsky.embed.video#view",
            "playlist": "https://video.cdn.bsky.app/raw/playlist.m3u8",
        }
        photos, video = extract_media_from_embed(embed)
        self.assertEqual(photos, [])
        self.assertEqual(video, "https://video.cdn.bsky.app/raw/playlist.m3u8")

    def test_build_bluesky_caption(self):
        caption = build_bluesky_caption(
            "Hello World #cool",
            "https://bsky.app/profile/user/post/1",
            "User Display Name",
            "user.handle",
            "via @sender",
        )
        self.assertIn("<b>User Display Name (@user.handle)</b>", caption)
        self.assertIn("Hello World #cool", caption)
        self.assertIn("#bluesky #cool", caption)
        self.assertIn("via @sender", caption)

    @patch("daphne.bluesky.resolve_handle")
    @patch("daphne.bluesky.fetch_post_thread")
    @patch("daphne.bluesky.send_photos", new_callable=AsyncMock)
    @patch("daphne.bluesky.try_delete_message", new_callable=AsyncMock)
    async def test_handle_bluesky_links_images(
        self, mock_delete, mock_send_photos, mock_fetch, mock_resolve
    ):
        mock_resolve.return_value = "did:plc:alice"
        mock_fetch.return_value = {
            "thread": {
                "post": {
                    "author": {"displayName": "Alice", "handle": "alice.bsky.social"},
                    "record": {"text": "My Post"},
                    "embed": {
                        "$type": "app.bsky.embed.images#view",
                        "images": [{"fullsize": "https://cdn.bsky.app/img/1.jpg"}],
                    },
                }
            }
        }
        mock_send_photos.return_value = True

        update = MagicMock()
        update.message.text = "https://bsky.app/profile/alice.bsky.social/post/123"
        update.message.chat_id = 999
        update.effective_user.username = "haru"
        update.effective_user.full_name = "Haru"

        context = MagicMock()
        context.bot.send_chat_action = AsyncMock()

        await handle_bluesky_links(update, context)

        mock_resolve.assert_called_once_with("alice.bsky.social")
        mock_fetch.assert_called_once_with("did:plc:alice", "123")
        mock_send_photos.assert_called_once()
        mock_delete.assert_called_once_with(update)


if __name__ == "__main__":
    unittest.main()


class TestBlueskyFailureReporting(unittest.IsolatedAsyncioTestCase):
    def _update(self):
        update = MagicMock()
        update.message = MagicMock()
        update.message.text = "https://bsky.app/profile/alice.test/post/3kabc"
        update.message.chat_id = 1
        update.message.reply_to_message = None
        update.message.is_topic_message = False
        update.message.delete = AsyncMock()
        update.effective_user = MagicMock(username="u", full_name="U")
        return update

    async def test_unresolvable_handle_gets_failure_reaction_and_reply(self):
        context = MagicMock()
        context.bot.send_chat_action = AsyncMock()
        context.bot.send_message = AsyncMock()
        update = self._update()
        with (
            patch("daphne.bluesky.resolve_handle", AsyncMock(return_value=None)),
            patch("daphne.bot.set_reaction", new=AsyncMock()) as react,
        ):
            await handle_bluesky_links(update, context)
        react.assert_awaited_once()
        self.assertEqual(react.await_args.args[1], "😢")
        self.assertIn(
            "Could not convert", context.bot.send_message.await_args.kwargs["text"]
        )
        update.message.delete.assert_not_called()

    async def test_failed_photo_send_is_reported_not_raised(self):
        context = MagicMock()
        context.bot.send_chat_action = AsyncMock()
        context.bot.send_message = AsyncMock()
        update = self._update()
        thread = {
            "thread": {
                "post": {
                    "author": {"handle": "alice.test"},
                    "record": {"text": "hi"},
                    "embed": {},
                }
            }
        }
        with (
            patch("daphne.bluesky.resolve_handle", AsyncMock(return_value="did:plc:x")),
            patch("daphne.bluesky.fetch_post_thread", AsyncMock(return_value=thread)),
            patch(
                "daphne.bluesky.extract_media_from_embed",
                return_value=(["https://cdn.bsky.app/a.jpg"], None),
            ),
            patch(
                "daphne.bluesky.send_photos",
                AsyncMock(side_effect=RuntimeError("telegram down")),
            ),
            patch("daphne.bot.set_reaction", new=AsyncMock()) as react,
        ):
            await handle_bluesky_links(update, context)
        react.assert_awaited_once()
        update.message.delete.assert_not_called()


class TestBlueskyStageReplies(unittest.IsolatedAsyncioTestCase):
    def _update(self):
        update = MagicMock()
        update.message = MagicMock()
        update.message.text = "https://bsky.app/profile/alice.test/post/3kabc"
        update.message.chat_id = 1
        update.message.reply_to_message = None
        update.message.is_topic_message = False
        update.message.delete = AsyncMock()
        update.effective_user = MagicMock(username="u", full_name="U")
        return update

    async def test_no_media_post_gets_its_own_reply_and_failure_reaction(self):
        context = MagicMock()
        context.bot.send_chat_action = AsyncMock()
        context.bot.send_message = AsyncMock()
        thread = {
            "thread": {
                "post": {
                    "author": {"handle": "alice.test"},
                    "record": {"text": "hi"},
                    "embed": {},
                }
            }
        }
        update = self._update()
        with (
            patch("daphne.bluesky.resolve_handle", AsyncMock(return_value="did:plc:x")),
            patch("daphne.bluesky.fetch_post_thread", AsyncMock(return_value=thread)),
            patch("daphne.bluesky.extract_media_from_embed", return_value=([], None)),
            patch("daphne.bot.set_reaction", new=AsyncMock()) as react,
        ):
            await handle_bluesky_links(update, context)
        self.assertEqual(react.await_args.args[1], "😢")
        self.assertIn(
            "no downloadable media", context.bot.send_message.await_args.kwargs["text"]
        )
        update.message.delete.assert_not_called()

    async def test_malformed_thread_is_reported_not_raised(self):
        context = MagicMock()
        context.bot.send_chat_action = AsyncMock()
        context.bot.send_message = AsyncMock()
        thread = {
            "thread": {"post": {"author": "not-a-dict", "record": {}, "embed": {}}}
        }
        update = self._update()
        with (
            patch("daphne.bluesky.resolve_handle", AsyncMock(return_value="did:plc:x")),
            patch("daphne.bluesky.fetch_post_thread", AsyncMock(return_value=thread)),
            patch("daphne.bot.set_reaction", new=AsyncMock()) as react,
        ):
            await handle_bluesky_links(update, context)
        react.assert_awaited_once()
        self.assertIn(
            "Could not convert", context.bot.send_message.await_args.kwargs["text"]
        )
        update.message.delete.assert_not_called()
