"""API error classification, independent of credentials and request scheduling."""

_MINIMAX_TRANSIENT_CODES = {"1000", "1001", "1024", "1033", "2064", "2066"}


def classify_api_error(status: int, raw: dict) -> str:
    """Recognize transient HTTP errors and MiniMax's HTTP 200 error envelope."""
    if status >= 500 or status in {408, 429}:
        return "transient"
    base = raw.get("base_resp")
    if (status == 200 and isinstance(base, dict)
            and str(base.get("status_code")) in _MINIMAX_TRANSIENT_CODES):
        return "transient"
    return "fatal"
