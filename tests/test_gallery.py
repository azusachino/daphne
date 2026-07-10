import os
import tempfile
import shutil
import unittest
from unittest.mock import patch, MagicMock

from daphne.gallery import scan_images, chunk_images, download_gallery


class TestGallery(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def _touch(self, name: str):
        path = os.path.join(self.test_dir, name)
        with open(path, "wb") as f:
            f.write(b"0")
        return path

    def test_scan_images_filters_and_sorts(self):
        self._touch("b.jpg")
        self._touch("a.png")
        self._touch("notes.txt")
        self._touch("clip.mp4")
        result = scan_images(self.test_dir)
        self.assertEqual([os.path.basename(p) for p in result], ["a.png", "b.jpg"])

    def test_chunk_images(self):
        images = [f"{i}.jpg" for i in range(23)]
        chunks = chunk_images(images, size=10)
        self.assertEqual([len(c) for c in chunks], [10, 10, 3])

    @patch("daphne.gallery.subprocess.run")
    def test_download_gallery_returns_scanned_images(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        self._touch("img1.jpg")
        self._touch("img2.webp")
        result = download_gallery("http://example.com/gallery", self.test_dir)
        self.assertEqual(len(result), 2)
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[:2], ["uvx", "gallery-dl"])
        self.assertIn("-D", cmd)

    @patch("daphne.gallery.subprocess.run", side_effect=Exception("boom"))
    def test_download_gallery_handles_failure(self, mock_run):
        result = download_gallery("http://example.com/gallery", self.test_dir)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
