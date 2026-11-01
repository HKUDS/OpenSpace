"""Provider-neutral multimodal content blocks.

OpenSpace keeps Anthropic-shaped blocks internally because OpenSpace's tool results
use that shape. Provider adapters convert them immediately before LiteLLM calls.
"""
from __future__ import annotations

import base64
import copy
import re
from collections.abc import Mapping, Sequence
from typing import Any, TypeAlias


ContentBlock: TypeAlias = dict[str, Any]
TextBlock: TypeAlias = dict[str, str]
ImageBlock: TypeAlias = dict[str, Any]
DocumentBlock: TypeAlias = dict[str, Any]

IMAGE_OMITTED_MARKER = "[image omitted: current model does not support image inputs]"
DOCUMENT_OMITTED_MARKER = "[document omitted: current model does not support document inputs]"

_IMAGE_DATA_URL_RE = re.compile(r"^data:(image/[^;,]+);base64,(.*)$", re.IGNORECASE | re.DOTALL)


def make_text_block(text: str) -> TextBlock:
    return {"type": "text", "text": text}


def make_image_block(data: str, media_type: str = "image/png") -> ImageBlock:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": data,
        },
    }


def make_image_block_from_bytes(
    data: bytes,
    media_type: str = "image/png",
) -> ImageBlock:
    return make_image_block(base64.b64encode(data).decode("ascii"), media_type)


def make_document_block(
    data: str,
    media_type: str = "application/pdf",
) -> DocumentBlock:
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": data,
        },
    }


def is_text_block(block: Any) -> bool:
    return isinstance(block, Mapping) and block.get("type") == "text"


def is_image_block(block: Any) -> bool:
    if not isinstance(block, Mapping):
        return False
    if block.get("type") == "image":
        source = block.get("source")
        return isinstance(source, Mapping) and source.get("type") == "base64"
    if block.get("type") == "image_url":
        image_url = block.get("image_url")
        return isinstance(image_url, Mapping) and isinstance(image_url.get("url"), str)
    return False


def is_document_block(block: Any) -> bool:
    return isinstance(block, Mapping) and block.get("type") == "document"


def to_openai_image_url_block(block: Mapping[str, Any]) -> dict[str, Any]:
    """Convert an Anthropic image block to OpenAI image_url form."""
    if block.get("type") == "image_url":
        return copy.deepcopy(dict(block))
    source = block.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("image block missing source")
    media_type = str(source.get("media_type") or "image/png")
    data = str(source.get("data") or "")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{media_type};base64,{data}"},
    }


def from_openai_image_url_block(block: Mapping[str, Any]) -> ImageBlock | None:
    image_url = block.get("image_url")
    if not isinstance(image_url, Mapping):
        return None
    url = image_url.get("url")
    if not isinstance(url, str):
        return None
    match = _IMAGE_DATA_URL_RE.match(url)
    if not match:
        return None
    return make_image_block(match.group(2), match.group(1))


def block_text_size(block: Any) -> int:
    """Count only model-visible text; binary payloads use fixed budgets elsewhere."""
    if isinstance(block, str):
        return len(block)
    if not isinstance(block, Mapping):
        return len(str(block))
    block_type = block.get("type")
    if block_type == "text":
        return len(str(block.get("text") or ""))
    if block_type == "tool_result":
        return content_text_size(block.get("content"))
    if block_type in {"image", "image_url", "document"}:
        return 0
    return len(str(block))


def content_text_size(content: Any) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        return sum(block_text_size(block) for block in content)
    return len(str(content))


def content_has_multimodal_block(content: Any) -> bool:
    if isinstance(content, Mapping):
        if is_image_block(content) or is_document_block(content):
            return True
        if content.get("type") == "tool_result":
            return content_has_multimodal_block(content.get("content"))
        return False
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        return any(content_has_multimodal_block(item) for item in content)
    return False


def extract_text_from_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif is_text_block(block):
                text = str(block.get("text") or "")
                if text:
                    parts.append(text)
            elif isinstance(block, Mapping) and block.get("type") == "tool_result":
                text = extract_text_from_content(block.get("content"))
                if text:
                    parts.append(text)
        return "\n".join(parts)
    return str(content)


def _normalized_model_name(model: str | None) -> str:
    return (model or "").strip().lower()


def _model_has_any(model: str, needles: Sequence[str]) -> bool:
    return any(needle in model for needle in needles)


def model_supports_images(model: str | None) -> bool:
    """Best-effort LiteLLM model capability check for direct image inputs."""
    name = _normalized_model_name(model)
    if not name:
        return False
    if name.startswith("ollama/"):
        return _model_has_any(name, ("llava", "bakllava", "vision", "moondream", "qwen3-vl", "qwen2.5vl", "qwen2-vl", "qwen-vl"))
    if _model_has_any(name, ("claude-3", "claude-sonnet-4", "claude-opus-4", "claude-4")):
        return True
    if _model_has_any(name, ("gpt-4o", "gpt-4.1", "gpt-5", "o3", "o4", "vision")):
        return True
    if _model_has_any(name, ("gemini", "pixtral", "llava", "qwen-vl", "qwen2-vl", "qwen2.5-vl", "qwen3-vl")):
        return True
    return False


def model_supports_documents(model: str | None) -> bool:
    name = _normalized_model_name(model)
    if not name:
        return False
    if _model_has_any(name, ("claude-3", "claude-sonnet-4", "claude-opus-4", "claude-4", "gemini")):
        return True
    return False


def _provider_family(model: str | None) -> str:
    name = _normalized_model_name(model)
    if "anthropic" in name or "claude" in name:
        return "anthropic"
    if "gemini" in name or "google" in name:
        return "gemini"
    if name.startswith("ollama/"):
        return "ollama"
    if "openai" in name or name.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    return "openai"


def normalize_content_blocks_for_model(content: Any, model: str | None) -> Any:
    """Convert multimodal blocks for the target model without mutating input."""
    if not isinstance(content, list):
        return content

    family = _provider_family(model)
    supports_images = model_supports_images(model)
    supports_documents = model_supports_documents(model)
    normalized: list[Any] = []

    for block in content:
        if not isinstance(block, Mapping):
            normalized.append(block)
            continue

        block_type = block.get("type")
        if block_type == "image" or block_type == "image_url":
            if not supports_images:
                normalized.append(make_text_block(IMAGE_OMITTED_MARKER))
            elif family == "anthropic" and block_type == "image_url":
                normalized.append(from_openai_image_url_block(block) or make_text_block(IMAGE_OMITTED_MARKER))
            elif family == "anthropic":
                normalized.append(copy.deepcopy(dict(block)))
            else:
                normalized.append(to_openai_image_url_block(block))
        elif block_type == "document":
            if supports_documents:
                normalized.append(copy.deepcopy(dict(block)))
            else:
                normalized.append(make_text_block(DOCUMENT_OMITTED_MARKER))
        elif block_type == "tool_result" and isinstance(block.get("content"), list):
            nested = normalize_content_blocks_for_model(block["content"], model)
            normalized.append({**copy.deepcopy(dict(block)), "content": nested})
        else:
            normalized.append(copy.deepcopy(dict(block)))

    return normalized


def normalize_multimodal_messages_for_model(
    messages: Sequence[Mapping[str, Any]],
    model: str | None,
) -> list[dict[str, Any]]:
    """Normalize every message's block content for the target provider."""
    result: list[dict[str, Any]] = []
    for message in messages:
        msg = dict(message)
        if isinstance(msg.get("content"), list):
            msg["content"] = normalize_content_blocks_for_model(msg["content"], model)
        result.append(msg)
    return result


__all__ = [
    "ContentBlock",
    "TextBlock",
    "ImageBlock",
    "DocumentBlock",
    "IMAGE_OMITTED_MARKER",
    "DOCUMENT_OMITTED_MARKER",
    "make_text_block",
    "make_image_block",
    "make_image_block_from_bytes",
    "make_document_block",
    "is_text_block",
    "is_image_block",
    "is_document_block",
    "to_openai_image_url_block",
    "from_openai_image_url_block",
    "block_text_size",
    "content_text_size",
    "content_has_multimodal_block",
    "extract_text_from_content",
    "model_supports_images",
    "model_supports_documents",
    "normalize_content_blocks_for_model",
    "normalize_multimodal_messages_for_model",
]
