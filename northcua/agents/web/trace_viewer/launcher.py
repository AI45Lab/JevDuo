"""Installed entry point for the optional Streamlit trace viewer."""

from __future__ import annotations
import sys
from pathlib import Path


def main() -> None:
    try:
        from streamlit.web.cli import main as streamlit_main
    except ImportError as exc:
        raise SystemExit('Install the viewer extra: pip install "northcua[local,viewer]"') from exc
    sys.argv = ["streamlit", "run", str(Path(__file__).with_name("app.py")), "--", *sys.argv[1:]]
    streamlit_main()
