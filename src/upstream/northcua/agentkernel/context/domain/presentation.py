"""Typed presentation-only notices excluded from semantic context identity."""

from __future__ import annotations

from enum import StrEnum


class ContextPresentationNotice(StrEnum):
    """Trusted notices emitted by prompt image-window projectors."""

    DESKTOP_HISTORY_IMAGE_OMITTED = (
        "[image omitted from this replay turn because the prompt image-window budget was exceeded; "
        "the original tool result included a screenshot/image artifact.]"
    )
    WEB_HISTORY_IMAGE_OMITTED = (
        "[image omitted from this replay turn because the web prompt image-window budget was exceeded; "
        "use the retained observation refs, annotation metadata, or request a fresh screenshot if visual "
        "confirmation is still needed.]"
    )
    WEB_TOP_LEVEL_IMAGE_OMITTED = (
        "[top-level observation image omitted from this replay turn because only the Step 1 initial observation "
        "may carry a current observation image; subsequent visual state is carried by tool results.]"
    )
    WEB_INITIAL_IMAGE_OMITTED = (
        "[initial observation image omitted from this replay turn because the web prompt image-window budget "
        "was exceeded; use the retained current refs, latest tool-result images, or request a fresh screenshot "
        "if visual confirmation is still needed.]"
    )
    ACTION_OVERLAY_HISTORY_IMAGE = (
        "Action-overlay history image: shows where a previous action was attempted. "
        "It is not current page state, and refs in it are expired."
    )
    CLEAN_PAGE_STATE_IMAGE = (
        "Clean page-state image after the retained tool result; it has no actionable refs by itself."
    )
    LATEST_SET_OF_MARK_IMAGE = "Latest Set-of-Mark image for actionable refs in this retained tool result."
    VISUAL_HISTORY_IMAGE = "Visual history image."


class ContextPresentationNoticeCatalog:
    """Normalize trusted display-only notices at the ToolResult boundary."""

    _EXACT = frozenset(notice.value for notice in ContextPresentationNotice)
    _STRING_SUFFIXES = (
        ContextPresentationNotice.DESKTOP_HISTORY_IMAGE_OMITTED.value,
        ContextPresentationNotice.WEB_HISTORY_IMAGE_OMITTED.value,
        ContextPresentationNotice.WEB_TOP_LEVEL_IMAGE_OMITTED.value,
        ContextPresentationNotice.WEB_INITIAL_IMAGE_OMITTED.value,
    )

    @classmethod
    def is_exact(cls, text: str) -> bool:
        """Return whether a complete ToolResult text block is presentation-only."""

        return text.strip() in cls._EXACT

    @classmethod
    def strip_appended_suffixes(cls, text: str) -> str:
        """Remove only trusted notices appended at a ToolResult string boundary."""

        normalized = text.strip()
        changed = True
        while changed:
            changed = False
            for notice in cls._STRING_SUFFIXES:
                if normalized == notice:
                    normalized = ""
                    changed = True
                    break
                marker = f"\n\n{notice}"
                if normalized.endswith(marker):
                    normalized = normalized[: -len(marker)].rstrip()
                    changed = True
                    break
        return normalized
