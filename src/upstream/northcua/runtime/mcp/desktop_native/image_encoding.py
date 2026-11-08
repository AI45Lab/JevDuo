"""Configurable desktop screenshot encoding helpers."""

from __future__ import annotations

import base64
import io
import os
from dataclasses import dataclass

from PIL import Image

SCREENSHOT_FORMAT_ENV = "NORTHCUA_DESKTOP_SCREENSHOT_FORMAT"
SCREENSHOT_JPEG_QUALITY_ENV = "NORTHCUA_DESKTOP_SCREENSHOT_JPEG_QUALITY"
SCREENSHOT_WEBP_QUALITY_ENV = "NORTHCUA_DESKTOP_SCREENSHOT_WEBP_QUALITY"

_DEFAULT_JPEG_QUALITY = 80
_DEFAULT_WEBP_QUALITY = 80


@dataclass(frozen=True, slots=True)
class EncodedImage:
    """Encoded image payload and MIME metadata.

    Attributes:
        data_url: Base64 data URL for model/image transport.
        mime_type: MIME type of the encoded image.
        extension: Conventional file extension for this encoding.
        size_bytes: Encoded payload size in bytes.
    """

    data_url: str
    mime_type: str
    extension: str
    size_bytes: int


def encode_desktop_image_to_data_url(image: Image.Image) -> str:
    """Encode a desktop screenshot according to environment settings.

    Args:
        image: Pillow image to encode.

    Returns:
        Base64 data URL for the encoded image.
    """

    return encode_desktop_image(image).data_url


def encode_desktop_image(image: Image.Image) -> EncodedImage:
    """Encode a desktop screenshot using PNG by default.

    ``NORTHCUA_DESKTOP_SCREENSHOT_FORMAT`` may be set to ``jpeg``/``jpg`` or
    ``webp`` to reduce websocket payload size in remote-device runs. The image
    dimensions are intentionally preserved so desktop coordinate mapping stays
    aligned with screenshot pixels.

    Args:
        image: Pillow image to encode.

    Returns:
        Encoded image payload and metadata.
    """

    requested_format = os.getenv(SCREENSHOT_FORMAT_ENV, "png").strip().lower()
    if requested_format in {"jpg", "jpeg"}:
        return _encode_jpeg(image)
    if requested_format == "webp":
        return _encode_webp(image)
    return _encode_png(image)


def image_data_url_file_extension(data_url: str) -> str:
    """Return a conventional file extension for an image data URL.

    Args:
        data_url: Image data URL.

    Returns:
        ``.jpg``, ``.webp``, or ``.png``.
    """

    if data_url.startswith("data:image/jpeg;base64,"):
        return ".jpg"
    if data_url.startswith("data:image/webp;base64,"):
        return ".webp"
    return ".png"


def _encode_png(image: Image.Image) -> EncodedImage:
    payload = _save_image_bytes(image, image_format="PNG")
    return EncodedImage(
        data_url=_data_url(payload, "image/png"),
        mime_type="image/png",
        extension=".png",
        size_bytes=len(payload),
    )


def _encode_jpeg(image: Image.Image) -> EncodedImage:
    payload = _save_image_bytes(
        _rgb_image(image),
        image_format="JPEG",
        quality=_quality(SCREENSHOT_JPEG_QUALITY_ENV, _DEFAULT_JPEG_QUALITY),
        optimize=True,
    )
    return EncodedImage(
        data_url=_data_url(payload, "image/jpeg"),
        mime_type="image/jpeg",
        extension=".jpg",
        size_bytes=len(payload),
    )


def _encode_webp(image: Image.Image) -> EncodedImage:
    payload = _save_image_bytes(
        _rgb_image(image),
        image_format="WEBP",
        quality=_quality(SCREENSHOT_WEBP_QUALITY_ENV, _DEFAULT_WEBP_QUALITY),
        method=4,
    )
    return EncodedImage(
        data_url=_data_url(payload, "image/webp"),
        mime_type="image/webp",
        extension=".webp",
        size_bytes=len(payload),
    )


def _rgb_image(image: Image.Image) -> Image.Image:
    if image.mode in {"RGB", "L"}:
        return image
    return image.convert("RGB")


def _save_image_bytes(image: Image.Image, *, image_format: str, **save_options: object) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=image_format, **save_options)
    return buffer.getvalue()


def _data_url(payload: bytes, mime_type: str) -> str:
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _quality(env_name: str, default: int) -> int:
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return min(max(parsed, 1), 100)
