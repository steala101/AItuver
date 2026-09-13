"""Image normalization and duplicate detection for local vision inputs."""
from __future__ import annotations

import io
import time

from neuro_voice.media import MediaInput, MediaType


def prepare_image(
    data: bytes, *, max_width: int = 1280, max_height: int = 1280,
    jpeg_quality: int = 85, media_type: MediaType = MediaType.IMAGE,
    timestamp: float | None = None,
) -> MediaInput:
    """Correct EXIF orientation, normalize RGB, bound dimensions and encode JPEG."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
        image.thumbnail((max(32, int(max_width)), max(32, int(max_height))), Image.LANCZOS)
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=max(30, min(95, int(jpeg_quality))), optimize=True)
        encoded = out.getvalue()
        fingerprint = average_hash(image)
        return MediaInput(
            media_type=media_type, data=encoded, mime_type="image/jpeg",
            timestamp=time.time() if timestamp is None else timestamp,
            metadata={"width": image.width, "height": image.height, "fingerprint": fingerprint},
        )


def average_hash(image) -> str:
    """Small deterministic perceptual hash; avoids another dependency."""
    from PIL import Image

    sample = image.convert("L").resize((16, 16), Image.Resampling.LANCZOS)
    flattened = getattr(sample, "get_flattened_data", sample.getdata)
    pixels = list(flattened())
    threshold = sum(pixels) / max(1, len(pixels))
    return "".join("1" if value >= threshold else "0" for value in pixels)


def hash_distance(left: str | None, right: str | None) -> float:
    if not left or not right or len(left) != len(right):
        return 1.0
    return sum(a != b for a, b in zip(left, right)) / len(left)
