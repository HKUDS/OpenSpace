"""
LocalTool.
Executes entirely inside this Python process.
"""
import asyncio
from typing import Any
from .base import BaseTool


class LocalTool(BaseTool):
    def _run(self, **kwargs):  
        raise NotImplementedError
    
    async def _dispatch_run(self, **kwargs) -> Any:
        # Prefer subclass's own _arun if it was overridden
        if self.__class__._arun is not LocalTool._arun:
            return await super()._arun(**kwargs)

        # Else fall back to thread-pooled _run if provided
        if self.__class__._run is not LocalTool._run:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, lambda: self._run(**kwargs))

        raise NotImplementedError(
            f"{self.__class__.__name__} must implement _run() or _arun()"
        )

    async def _arun(self, **kwargs):
        return await self._dispatch_run(**kwargs)