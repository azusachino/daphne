import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from daphne import booru
from daphne.booru import contains_booru_link, extract_booru_url, handle_booru_links


class _Slot:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class TestBooruLinks(unittest.TestCase):
    def test_single_post_urls_are_recognised_on_every_site(self):
        for url in (
            "https://yande.re/post/show/1234",
            "https://konachan.com/post/show/99",
            "https://konachan.net/post/show/99",
            "https://lolibooru.moe/post/show/5",
            "https://danbooru.donmai.us/posts/7654321",
        ):
            with self.subTest(url=url):
                self.assertEqual(extract_booru_url(f"look {url}!"), url)

    def test_tag_and_search_pages_are_not_converted(self):
        for url in (
            "https://yande.re/post?tags=cat",
            "https://danbooru.donmai.us/posts?tags=cat",
            "https://konachan.com/post/popular_recent",
            "https://example.com/post/show/1",
        ):
            with self.subTest(url=url):
                self.assertFalse(contains_booru_link(url))

    def test_site_label_names_the_host(self):
        self.assertEqual(booru._site_label("https://yande.re/post/show/1"), "Yande")
        self.assertEqual(
            booru._site_label("https://danbooru.donmai.us/posts/1"), "Danbooru"
        )


class TestBooruHandler(unittest.IsolatedAsyncioTestCase):
    def _update(self, text):
        update = MagicMock()
        update.message.text = text
        update.message.chat_id = 42
        update.message.reply_text = AsyncMock()
        update.message.delete = AsyncMock()
        update.message.reply_to_message = None
        update.message.is_topic_message = False
        update.effective_user = None
        context = MagicMock()
        context.bot.send_media_group = AsyncMock()
        context.bot.send_photo = AsyncMock()
        context.bot.send_document = AsyncMock()
        return update, context

    async def test_several_images_go_out_as_one_media_group(self):
        update, context = self._update("https://yande.re/post/show/1")
        images = [(b"a" * 10, "1_a.jpg"), (b"b" * 10, "1_b.png")]
        with (
            patch("daphne.booru._download_images", return_value=images),
            patch("daphne.bot.download_slot", return_value=_Slot()),
            patch("daphne.bot.set_reaction", new=AsyncMock()),
        ):
            await handle_booru_links(update, context)
        context.bot.send_media_group.assert_awaited_once()
        media = context.bot.send_media_group.call_args.kwargs["media"]
        self.assertEqual(len(media), 2)
        self.assertIn("Source (Yande)", media[0].caption)
        self.assertIsNone(media[1].caption)
        update.message.delete.assert_awaited_once()

    async def test_an_oversized_image_is_sent_as_a_document(self):
        update, context = self._update("https://danbooru.donmai.us/posts/9")
        big = (b"x" * (booru.PHOTO_LIMIT_BYTES + 1), "9_big.png")
        with (
            patch("daphne.booru._download_images", return_value=[big]),
            patch("daphne.bot.download_slot", return_value=_Slot()),
            patch("daphne.bot.set_reaction", new=AsyncMock()),
        ):
            await handle_booru_links(update, context)
        context.bot.send_document.assert_awaited_once()
        context.bot.send_photo.assert_not_called()

    async def test_no_images_replies_and_marks_the_failure(self):
        update, context = self._update("https://konachan.com/post/show/3")
        with (
            patch("daphne.booru._download_images", return_value=[]),
            patch("daphne.bot.download_slot", return_value=_Slot()),
            patch("daphne.bot.set_reaction", new=AsyncMock()) as set_reaction,
        ):
            await handle_booru_links(update, context)
        set_reaction.assert_awaited_once_with(update.message, "😢")
        self.assertIn(
            "could not be downloaded", update.message.reply_text.call_args[0][0]
        )
        update.message.delete.assert_not_awaited()


class TestBooruDownload(unittest.TestCase):
    def test_images_are_read_from_gallery_dl_output(self):
        def fake_download(url, out_dir, extra_args=()):
            for name in ("1_p0.jpg", "1_p1.jpg"):
                with open(os.path.join(out_dir, name), "wb") as handle:
                    handle.write(name.encode())
            return [os.path.join(out_dir, n) for n in ("1_p0.jpg", "1_p1.jpg")]

        with patch("daphne.booru.download_gallery", side_effect=fake_download):
            images = booru._download_images("https://yande.re/post/show/1")
        self.assertEqual(images, [(b"1_p0.jpg", "1_p0.jpg"), (b"1_p1.jpg", "1_p1.jpg")])


class TestBooruGuard(unittest.TestCase):
    def test_the_downloader_refuses_tag_pages(self):
        with patch("daphne.booru.download_gallery") as download:
            with self.assertRaises(ValueError):
                booru._download_images("https://yande.re/post?tags=cat")
        download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
