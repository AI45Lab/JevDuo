"""Controller defaults shared by case and experiment CLIs."""

from __future__ import annotations

import os

PUBLIC_CONTROLLER_URL_ENV = "NORTHCUA_PUBLIC_CONTROLLER_URL"
"""Environment variable used as the default public controller URL."""


def default_public_controller_url() -> str:
    """Return the configured public controller URL CLI default.

    The value is intentionally limited to a URL, not a tunnel token. Cloudflare
    connector credentials should stay in cloudflared/systemd configuration.

    Returns:
        Trimmed public controller URL from the environment, or an empty string.
    """

    return os.getenv(PUBLIC_CONTROLLER_URL_ENV, "").strip()
