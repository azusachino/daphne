import os
import subprocess
import logging

logger = logging.getLogger(__name__)

# gallery-dl writes arbitrary image types; we only forward the ones Telegram can
# render as a photo/media group.
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

# Telegram caps a single media group at 10 items.
MEDIA_GROUP_LIMIT = 10


def scan_images(out_dir: str) -> list[str]:
    """Return image files under ``out_dir`` sorted by path for stable ordering."""
    images: list[str] = []
    for root, _, files in os.walk(out_dir):
        for file in files:
            _, ext = os.path.splitext(file)
            if ext.lower() in IMAGE_EXTS:
                images.append(os.path.join(root, file))
    images.sort()
    return images


def download_gallery(url: str, out_dir: str) -> list[str]:
    """
    Download an image gallery with gallery-dl into ``out_dir`` and return the
    resulting image paths (empty list on failure). ``-D`` flattens everything
    into ``out_dir`` so no per-site subdirectories are created.
    """
    os.makedirs(out_dir, exist_ok=True)
    cmd = ["uvx", "gallery-dl", "-D", out_dir, "--", url]
    logger.info("Running command: %s", " ".join(cmd))
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=180.0)
    except subprocess.CalledProcessError as e:
        logger.warning(
            "gallery-dl failed with exit code %s. stderr: %s", e.returncode, e.stderr
        )
    except Exception as e:
        logger.warning("Failed to run gallery-dl: %s", e)
    return scan_images(out_dir)


def chunk_images(images: list[str], size: int = MEDIA_GROUP_LIMIT) -> list[list[str]]:
    """Split image paths into Telegram-sized media groups."""
    return [images[i : i + size] for i in range(0, len(images), size)]
