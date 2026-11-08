"""Helpers for lazily materializing prompt image payloads from disk."""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path


def image_data_url_from_path(path: str) -> str:
    """Return a data URL for an existing image path.

    Args:
        path: Local image artifact path.

    Returns:
        Image data URL, or an empty string when the path cannot be read.
    """

    if not path:
        return ""
    image_path = Path(path)
    if not image_path.exists() or not image_path.is_file():
        return ""
    mime_type = mimetypes.guess_type(str(image_path))[0] or "image/png"
    try:
        return f"data:{mime_type};base64,{base64.b64encode(image_path.read_bytes()).decode('ascii')}"
    except OSError:
        return ""


__all__ = ["image_data_url_from_path"]
