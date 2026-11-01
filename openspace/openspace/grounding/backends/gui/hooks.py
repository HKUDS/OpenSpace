from __future__ import annotations

import asyncio
import base64
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from openspace.config import get_config
from openspace.grounding.core.types import ToolResult
from openspace.services.conversation.content_blocks import (
    content_has_multimodal_block,
    extract_text_from_content,
    is_image_block,
    make_image_block,
    make_image_block_from_bytes,
    make_text_block,
    model_supports_images,
)
from openspace.platforms.screenshot import ScreenshotClient
from openspace.prompts import GroundingAgentPrompts
from openspace.utils.logging import Logger

if TYPE_CHECKING:
    from openspace.llm import LLMClient

logger = Logger.get_logger(__name__)

DEFAULT_VISUAL_ANALYSIS_MODEL = "openrouter/qwen/qwen3-vl-8b-instruct"
_litellm_module: Any | None = None


def _get_litellm() -> Any:
    global _litellm_module
    if _litellm_module is None:
        import litellm as imported_litellm

        _litellm_module = imported_litellm
    return _litellm_module


class _LiteLLMProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(_get_litellm(), name)


litellm = _LiteLLMProxy()


class VisualAnalysisHook:
    """GUI PostToolUse hook that enhances screenshot-bearing results.

    This is an OS-specific extension. OpenSpace has no separate visual analysis agent;
    multimodal content is passed directly to the model. Keeping this logic in
    the GUI backend prevents the core agent loop from owning GUI-only behavior.
    """

    def __init__(
        self,
        llm_client: Optional["LLMClient"] = None,
        visual_analysis_model: Optional[str] = None,
        visual_analysis_timeout: Optional[float] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        self._llm_client = llm_client
        self._visual_analysis_model = visual_analysis_model
        self._visual_analysis_timeout = visual_analysis_timeout
        self._enabled = enabled

    async def analyze_tool_result(
        self,
        result: ToolResult,
        tool_name: str,
        tool_call: Any,
        backend: str,
        task_description: str = "",
        context: Any | None = None,
    ) -> ToolResult:
        """Analyze GUI screenshots and return an enhanced tool result."""
        if backend != "gui":
            return result

        mode = self._mode()
        if mode == "off":
            return result

        main_model = self._main_model(context)
        if mode == "fallback" and model_supports_images(main_model):
            return self._ensure_latest_screenshot_block(result)

        metadata = getattr(result, "metadata", None)
        has_screenshots = metadata and (
            metadata.get("screenshot") or metadata.get("screenshots")
        )
        has_visual_blocks = content_has_multimodal_block(getattr(result, "content", None))

        if not has_screenshots and not has_visual_blocks:
            try:
                logger.info("No visual data from %s, capturing screenshot...", tool_name)
                screenshot_client = ScreenshotClient()
                screenshot_bytes = await screenshot_client.capture()

                if screenshot_bytes:
                    if metadata is None:
                        result.metadata = {}
                        metadata = result.metadata
                    metadata["screenshot"] = screenshot_bytes
                    has_screenshots = True
                    logger.info("Screenshot captured for visual analysis")
                else:
                    logger.warning("Failed to capture screenshot")
            except Exception as exc:
                logger.warning("Error capturing screenshot: %s", exc)

        if not has_screenshots and not has_visual_blocks:
            logger.debug("No visual data available for %s", tool_name)
            return result

        return await self._enhance_result(
            result=result,
            tool_name=tool_name,
            task_description=task_description,
            context=context,
            mode=mode,
        )

    def _is_enabled(self) -> bool:
        if self._enabled is not None:
            return bool(self._enabled)
        gui_config = self._gui_config()
        return bool(getattr(gui_config, "enable_visual_analysis", True))

    def _mode(self) -> str:
        if not self._is_enabled():
            return "off"
        gui_config = self._gui_config()
        mode = str(getattr(gui_config, "visual_analysis_mode", "fallback") or "fallback").lower()
        return mode if mode in {"fallback", "always", "off"} else "fallback"

    def _timeout(self) -> float:
        if self._visual_analysis_timeout is not None:
            return float(self._visual_analysis_timeout)
        gui_config = self._gui_config()
        return float(getattr(gui_config, "visual_analysis_timeout", 30.0) or 30.0)

    def _model(self, context: Any | None) -> str:
        if self._visual_analysis_model:
            return self._visual_analysis_model

        gui_config = self._gui_config()
        configured_model = getattr(gui_config, "visual_analysis_model", None)
        if configured_model:
            return str(configured_model)

        llm_client = self._resolve_llm_client(context)
        if llm_client is not None and getattr(llm_client, "model", None):
            candidate = str(llm_client.model)
            if model_supports_images(candidate):
                return candidate

        return DEFAULT_VISUAL_ANALYSIS_MODEL

    def _main_model(self, context: Any | None) -> str | None:
        context_model = getattr(context, "model", None)
        if context_model:
            return str(context_model)
        llm_client = self._resolve_llm_client(context)
        if llm_client is not None and getattr(llm_client, "model", None):
            return str(llm_client.model)
        return None

    def _resolve_llm_client(self, context: Any | None) -> Optional["LLMClient"]:
        if self._llm_client is not None:
            return self._llm_client
        return getattr(context, "llm_client", None) if context is not None else None

    @staticmethod
    def _gui_config() -> Any:
        try:
            return get_config().get_backend_config("gui")
        except Exception:
            return object()

    async def _enhance_result(
        self,
        *,
        result: ToolResult,
        tool_name: str,
        task_description: str = "",
        context: Any | None = None,
        mode: str = "fallback",
    ) -> ToolResult:
        try:
            metadata = getattr(result, "metadata", None) or {}

            screenshots_bytes = self._extract_visual_inputs(result, metadata)

            if not screenshots_bytes:
                return result

            selected_screenshots = self._select_key_screenshots(
                screenshots_bytes, max_count=3,
            )

            visual_b64_list = []
            for visual_data in selected_screenshots:
                if isinstance(visual_data, bytes):
                    visual_b64_list.append(
                        base64.b64encode(visual_data).decode("utf-8")
                    )
                else:
                    visual_b64_list.append(str(visual_data))

            prompt = GroundingAgentPrompts.visual_analysis(
                tool_name=tool_name,
                num_screenshots=len(visual_b64_list),
                task_description=task_description,
            )

            content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
            for visual_b64 in visual_b64_list:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{visual_b64}"},
                })

            visual_model = self._model(context)
            llm_client = self._resolve_llm_client(context)
            llm_extra: Dict[str, Any] = {}
            if llm_client is not None and visual_model == getattr(llm_client, "model", None):
                llm_extra = getattr(llm_client, "litellm_kwargs", {}) or {}
            elif self._visual_analysis_model or getattr(
                self._gui_config(), "visual_analysis_model", None,
            ):
                try:
                    from openspace.host_detection import build_llm_kwargs

                    visual_model, llm_extra = build_llm_kwargs(visual_model)
                except Exception as exc:
                    logger.debug(
                        "Failed to resolve dedicated visual model credentials: %s",
                        exc,
                    )

            timeout = self._timeout()
            response = await asyncio.wait_for(
                litellm.acompletion(
                    model=visual_model,
                    messages=[{"role": "user", "content": content}],
                    timeout=timeout,
                    **llm_extra,
                ),
                timeout=timeout + 5,
            )

            analysis = response.choices[0].message.content.strip()
            original_text = extract_text_from_content(result.content) or "(no text output)"
            visual_text = f"{original_text}\n\n**Visual content**: {analysis}"
            if mode == "always" and isinstance(result.content, list):
                enhanced_content: Any = list(result.content) + [make_text_block(f"**Visual content**: {analysis}")]
            else:
                enhanced_content = visual_text

            enhanced_result = ToolResult(
                status=result.status,
                content=enhanced_content,
                error=result.error,
                metadata={
                    **metadata,
                    "visual_analyzed": True,
                    "visual_analysis": analysis,
                },
                execution_time=result.execution_time,
            )

            logger.info(
                "Enhanced %s result with visual analysis (%d screenshot(s))",
                tool_name,
                len(visual_b64_list),
            )
            return enhanced_result

        except asyncio.TimeoutError:
            logger.warning(
                "Visual analysis timed out for %s, returning original result",
                tool_name,
            )
            return result
        except Exception as exc:
            logger.warning(
                "Failed to analyze visual content for %s: %s",
                tool_name,
                exc,
            )
            return result

    @staticmethod
    def _extract_visual_inputs(
        result: ToolResult,
        metadata: Dict[str, Any],
    ) -> List[bytes | str]:
        screenshots: List[bytes | str] = []
        if metadata.get("screenshots"):
            screenshots_list = metadata["screenshots"]
            if isinstance(screenshots_list, list):
                screenshots.extend(s for s in screenshots_list if s)
        elif metadata.get("screenshot"):
            screenshots.append(metadata["screenshot"])

        content = getattr(result, "content", None)
        if isinstance(content, list):
            for block in content:
                if not is_image_block(block) or not isinstance(block, dict):
                    continue
                if block.get("type") == "image":
                    source = block.get("source")
                    if isinstance(source, dict) and source.get("data"):
                        screenshots.append(str(source["data"]))
                elif block.get("type") == "image_url":
                    image_url = block.get("image_url")
                    if isinstance(image_url, dict):
                        url = str(image_url.get("url") or "")
                        if ";base64," in url:
                            screenshots.append(url.split(";base64,", 1)[1])
        return screenshots

    @staticmethod
    def _ensure_latest_screenshot_block(result: ToolResult) -> ToolResult:
        """Promote metadata-only GUI screenshots into model-visible content."""
        if content_has_multimodal_block(getattr(result, "content", None)):
            return result
        metadata = getattr(result, "metadata", None) or {}
        screenshots = VisualAnalysisHook._extract_visual_inputs(result, metadata)
        if not screenshots:
            return result
        latest = screenshots[-1]
        if isinstance(latest, bytes):
            image_block = make_image_block_from_bytes(latest, "image/png")
        else:
            image_block = make_image_block(str(latest), "image/png")
        text = extract_text_from_content(result.content) or str(result.error or "(no text output)")
        return ToolResult(
            status=result.status,
            content=[make_text_block(text), image_block],
            error=result.error,
            metadata=metadata,
            execution_time=result.execution_time,
        )

    @staticmethod
    def _select_key_screenshots(
        screenshots: List[bytes | str],
        max_count: int = 3,
    ) -> List[bytes | str]:
        """Select key screenshots, preferring first, last, and even spacing."""
        if len(screenshots) <= max_count:
            return screenshots

        selected_indices: set[int] = {len(screenshots) - 1}
        if max_count >= 2:
            selected_indices.add(0)

        remaining_slots = max_count - len(selected_indices)
        if remaining_slots > 0:
            available_indices = [
                i for i in range(1, len(screenshots) - 1)
                if i not in selected_indices
            ]
            if available_indices:
                step = max(1, len(available_indices) // (remaining_slots + 1))
                for i in range(remaining_slots):
                    idx = min((i + 1) * step, len(available_indices) - 1)
                    if idx < len(available_indices):
                        selected_indices.add(available_indices[idx])

        selected = [screenshots[i] for i in sorted(selected_indices)]
        logger.debug(
            "Selected %d screenshots at indices %s from total of %d",
            len(selected),
            sorted(selected_indices),
            len(screenshots),
        )
        return selected
