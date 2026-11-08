"""Image inspection and transcoding helpers for provider payloads."""

from __future__ import annotations

import base64
import io
import logging
from typing import Sequence, cast

from nexau.core.messages import ImageBlock, Message, TextBlock, ToolResultBlock, parse_base64_data_url
from PIL import Image, ImageFile

from northcua.core.json_types import JsonObject

# pyright: reportMissingImports=false


logger = logging.getLogger(__name__)


class ProviderImageInspector:
    """Inspect OpenAI-chat-shaped payloads for image inputs."""

    def contains_images(self, openai_chat_messages: list[JsonObject]) -> bool:
        """Return whether OpenAI-chat-shaped messages contain images.

        Args:
            openai_chat_messages: OpenAI-chat-shaped payload messages.

        Returns:
            ``True`` when at least one image URL block is present.
        """

        return self.count_openai_chat_images(openai_chat_messages) > 0

    def count_openai_chat_images(self, messages: list[JsonObject]) -> int:
        """Count image URL blocks in OpenAI-chat-shaped messages.

        Args:
            messages: OpenAI-chat-shaped payload messages.

        Returns:
            Number of ``image_url`` content blocks.
        """

        count = 0
        for item in messages:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                count += sum(1 for block in content if isinstance(block, dict) and block.get("type") == "image_url")
        return count


class ProviderImageTranscoder:
    """Transcode inline image blocks to JPEG for strict gateway paths."""

    def transcode_messages(self, messages: list[Message]) -> list[Message]:
        """Transcode inline image blocks to JPEG for gateway paths that require it.

        Args:
            messages: UMP messages whose content blocks may include images.

        Returns:
            Messages with convertible inline image blocks rewritten to JPEG.
        """

        rewritten: list[Message] = []
        for message in messages:
            rewritten.append(message.model_copy(update={"content": self.transcode_blocks(message.content)}))
        return rewritten

    def transcode_blocks(self, blocks: Sequence[object]) -> list[object]:
        """Transcode image blocks nested inside one message content list.

        Args:
            blocks: UMP content blocks from one message or tool result.

        Returns:
            Content blocks with inline images converted when possible.
        """

        rewritten: list[object] = []
        for block in blocks:
            if isinstance(block, ImageBlock):
                rewritten.append(self.transcode_image_block(block))
                continue
            content = getattr(block, "content", None)
            model_copy = getattr(block, "model_copy", None)
            if isinstance(block, ToolResultBlock) and isinstance(content, list) and callable(model_copy):
                rewritten.append(
                    model_copy(
                        update={
                            "content": [
                                (
                                    cast(TextBlock | ImageBlock, self.transcode_image_block(part))
                                    if isinstance(part, ImageBlock)
                                    else part
                                )
                                for part in content
                            ]
                        }
                    )
                )
                continue
            rewritten.append(block)
        return rewritten

    def transcode_image_block(self, block: ImageBlock) -> ImageBlock:
        """Best-effort convert one inline image block to JPEG base64.

        Args:
            block: UMP image block backed by base64 data or a data URL.

        Returns:
            JPEG-backed image block when conversion succeeds; otherwise the
            original block.
        """

        if block.base64 and block.mime_type == "image/jpeg":
            return block
        if block.url and not block.url.startswith("data:"):
            return block

        image_bytes = self.image_block_bytes(block)
        if image_bytes is None:
            return block

        previous_allow_truncated = ImageFile.LOAD_TRUNCATED_IMAGES
        try:
            ImageFile.LOAD_TRUNCATED_IMAGES = True
            with Image.open(io.BytesIO(image_bytes)) as image:
                converted = image.convert("RGBA") if image.mode in {"RGBA", "LA", "P"} else image.convert("RGB")
                if converted.mode == "RGBA":
                    background = Image.new("RGB", converted.size, (255, 255, 255))
                    background.paste(converted, mask=converted.getchannel("A"))
                    converted = background
                output = io.BytesIO()
                converted.save(output, format="JPEG", quality=92)
            return ImageBlock(
                url=None,
                base64=base64.b64encode(output.getvalue()).decode("ascii"),
                mime_type="image/jpeg",
                detail=block.detail,
            )
        except Exception:
            logger.debug(
                "Failed to transcode inline image block to JPEG; preserving original image block.", exc_info=True
            )
            return block
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = previous_allow_truncated

    def image_block_bytes(self, block: ImageBlock) -> bytes | None:
        """Decode bytes from a base64-backed image block.

        Args:
            block: UMP image block backed by base64 data or a data URL.

        Returns:
            Decoded bytes, or ``None`` when local decoding is not possible.
        """

        if block.base64:
            try:
                return base64.b64decode(block.base64)
            except Exception:
                logger.debug("Failed to decode inline image block base64 payload.", exc_info=True)
                return None
        if block.url:
            parsed = parse_base64_data_url(block.url)
            if parsed is None:
                return None
            _mime_type, encoded = parsed
            try:
                return base64.b64decode(encoded)
            except Exception:
                logger.debug("Failed to decode data URL image payload.", exc_info=True)
                return None
        return None
