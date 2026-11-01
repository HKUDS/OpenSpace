from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from openspace.grounding.core.types import ToolResult
from openspace.platforms.screenshot import ScreenshotClient
from openspace.prompts import GroundingAgentPrompts
from openspace.utils.logging import Logger

if TYPE_CHECKING:
    from openspace.llm import LLMClient

logger = Logger.get_logger(__name__)


class VisualAnalyzer:
    """Handles screenshot capture and LLM-based visual analysis of tool results."""

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        visual_analysis_model: Optional[str] = None,
        visual_analysis_timeout: float = 30.0,
    ) -> None:
        self._llm_client = llm_client
        self._visual_analysis_model = visual_analysis_model
        self._visual_analysis_timeout = visual_analysis_timeout

    async def analyze_tool_result(
        self,
        result: ToolResult,
        tool_name: str,
        tool_call: Dict,
        backend: str,
        task_description: str = "",
    ) -> ToolResult:
        """Callback for LLMClient to handle visual analysis after tool execution."""
        skip_visual_analysis = False
        try:
            arguments = tool_call.function.arguments
            if isinstance(arguments, str):
                args = json.loads(arguments.strip() or "{}")
            else:
                args = arguments

            if isinstance(args, dict) and args.get("skip_visual_analysis"):
                skip_visual_analysis = True
                logger.info(f"Visual analysis skipped for {tool_name} (meta-parameter set by LLM)")
        except Exception as e:
            logger.debug(f"Could not parse tool arguments: {e}")

        if skip_visual_analysis:
            return result

        if backend != "gui":
            return result

        metadata = getattr(result, "metadata", None)
        has_screenshots = metadata and (
            metadata.get("screenshot") or metadata.get("screenshots")
        )

        if not has_screenshots:
            try:
                logger.info(f"No visual data from {tool_name}, capturing screenshot...")
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
            except Exception as e:
                logger.warning(f"Error capturing screenshot: {e}")

        if not has_screenshots:
            logger.debug(f"No visual data available for {tool_name}")
            return result

        return await self._enhance_result(result, tool_name, task_description)

    async def _enhance_result(
        self,
        result: ToolResult,
        tool_name: str,
        task_description: str = "",
    ) -> ToolResult:
        """Enhance tool result with LLM-based visual analysis."""
        import asyncio
        import base64
        import litellm

        try:
            metadata = getattr(result, "metadata", None)
            if not metadata:
                return result

            screenshots_bytes: List[bytes] = []
            if metadata.get("screenshots"):
                screenshots_list = metadata["screenshots"]
                if isinstance(screenshots_list, list):
                    screenshots_bytes = [s for s in screenshots_list if s]
            elif metadata.get("screenshot"):
                screenshots_bytes = [metadata["screenshot"]]

            if not screenshots_bytes:
                return result

            selected_screenshots = self._select_key_screenshots(
                screenshots_bytes, max_count=3
            )

            visual_b64_list = []
            for visual_data in selected_screenshots:
                if isinstance(visual_data, bytes):
                    visual_b64_list.append(
                        base64.b64encode(visual_data).decode("utf-8")
                    )
                else:
                    visual_b64_list.append(visual_data)

            num_screenshots = len(visual_b64_list)

            prompt = GroundingAgentPrompts.visual_analysis(
                tool_name=tool_name,
                num_screenshots=num_screenshots,
                task_description=task_description,
            )

            content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
            for visual_b64 in visual_b64_list:
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{visual_b64}"},
                    }
                )

            visual_model = self._visual_analysis_model or (
                self._llm_client.model
                if self._llm_client
                else "openrouter/anthropic/claude-sonnet-4.5"
            )
            _llm_extra: Dict[str, Any] = {}
            if self._llm_client and visual_model == self._llm_client.model:
                _llm_extra = (
                    getattr(self._llm_client, "litellm_kwargs", {}) or {}
                )
            elif self._visual_analysis_model:
                try:
                    from openspace.host_detection import build_llm_kwargs

                    visual_model, _llm_extra = build_llm_kwargs(visual_model)
                except Exception as e:
                    logger.debug(
                        f"Failed to resolve dedicated visual model credentials: {e}"
                    )
                    _llm_extra = {}

            response = await asyncio.wait_for(
                litellm.acompletion(
                    model=visual_model,
                    messages=[{"role": "user", "content": content}],
                    timeout=self._visual_analysis_timeout,
                    **_llm_extra,
                ),
                timeout=self._visual_analysis_timeout + 5,
            )

            analysis = response.choices[0].message.content.strip()

            original_content = result.content or "(no text output)"
            enhanced_content = (
                f"{original_content}\n\n**Visual content**: {analysis}"
            )

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
                f"Enhanced {tool_name} result with visual analysis "
                f"({num_screenshots} screenshot(s))"
            )
            return enhanced_result

        except asyncio.TimeoutError:
            logger.warning(
                f"Visual analysis timed out for {tool_name}, returning original result"
            )
            return result
        except Exception as e:
            logger.warning(
                f"Failed to analyze visual content for {tool_name}: {e}"
            )
            return result

    @staticmethod
    def _select_key_screenshots(
        screenshots: List[bytes],
        max_count: int = 3,
    ) -> List[bytes]:
        """Select key screenshots from a sequence, preferring first/last/evenly-spaced."""
        if len(screenshots) <= max_count:
            return screenshots

        selected_indices: set[int] = set()

        selected_indices.add(len(screenshots) - 1)

        if max_count >= 2:
            selected_indices.add(0)

        remaining_slots = max_count - len(selected_indices)
        if remaining_slots > 0:
            available_indices = [
                i
                for i in range(1, len(screenshots) - 1)
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
            f"Selected {len(selected)} screenshots at indices "
            f"{sorted(selected_indices)} from total of {len(screenshots)}"
        )

        return selected
