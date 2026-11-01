from typing import Any, Dict, Optional
from abc import ABC, abstractmethod

from ..types import SandboxOptions, BackendType


class BaseSandbox(ABC):   
    def __init__(self, options: SandboxOptions):
        self.options = options
        self._active = False
    
    @abstractmethod
    async def start(self) -> bool:
        """Set self._active to True"""
        pass
    
    @abstractmethod
    async def stop(self) -> None:
        """Set self._active to False"""
        pass
    
    @abstractmethod
    async def execute_safe(self, command: str, **kwargs) -> Any:
        pass
    
    @abstractmethod
    def get_connector(self) -> Any:
        pass
    
    @property
    def is_active(self) -> bool:
        return self._active


class SandboxManager:
    def __init__(self):
        self._sandboxes: Dict[BackendType, BaseSandbox] = {}
    
    def register_sandbox(self, backend_type: BackendType, sandbox: BaseSandbox) -> None:
        self._sandboxes[backend_type] = sandbox
    
    def get_sandbox(self, backend_type: BackendType) -> Optional[BaseSandbox]:
        return self._sandboxes.get(backend_type)
    
    async def start_all(self) -> None:
        for sandbox in self._sandboxes.values():
            await sandbox.start()
    
    async def stop_all(self) -> None:
        for sandbox in self._sandboxes.values():
            await sandbox.stop()