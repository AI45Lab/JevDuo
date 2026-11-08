"""Streamlit viewer for web-agent and web-evaluator LLM traces."""


def main() -> None:
    """Load the optional Streamlit UI only when the viewer is launched."""

    from .app import main as run

    run()


__all__ = ["main"]
