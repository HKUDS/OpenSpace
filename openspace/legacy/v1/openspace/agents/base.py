from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Dict, List, Optional, Type, Any

from openspace.utils.logging import Logger

if TYPE_CHECKING:
    from openspace.llm import LLMClient
    from openspace.grounding.core.grounding_client import GroundingClient
    from openspace.recording import RecordingManager

logger = Logger.get_logger(__name__)


class BaseAgent(ABC):
    def __init__(
        self,
        name: str,
        backend_scope: Optional[List[str]] = None,
        llm_client: Optional[LLMClient] = None,
        grounding_client: Optional[GroundingClient] = None,
        recording_manager: Optional[RecordingManager] = None,
    ) -> None:
        """
        Initialize the BaseAgent.
        
        Args:
            name: Unique name for the agent
            backend_scope: List of backend types this agent can access (e.g., ["gui", "shell", "mcp", "web", "system"])
            llm_client: LLM client for agent reasoning (optional, can be set later)
            grounding_client: Reference to GroundingClient for tool execution
            recording_manager: RecordingManager for recording execution
        """
        self._name = name
        self._grounding_client: Optional[GroundingClient] = grounding_client
        self._backend_scope = backend_scope or []
        self._llm_client = llm_client
        self._recording_manager: Optional[RecordingManager] = recording_manager
        self._step = 0
        self._status = AgentStatus.ACTIVE
        
        self._register_self()
        logger.info(f"Initialized {self.__class__.__name__}: {name}")

    @property
    def name(self) -> str:
        return self._name
    
    @property
    def grounding_client(self) -> Optional[GroundingClient]:
        """Get the grounding client."""
        return self._grounding_client

    @property
    def backend_scope(self) -> List[str]:
        return self._backend_scope

    @property
    def llm_client(self) -> Optional[LLMClient]:
        return self._llm_client

    @llm_client.setter
    def llm_client(self, client: LLMClient) -> None:
        self._llm_client = client

    @property
    def recording_manager(self) -> Optional[RecordingManager]:
        """Get the recording manager."""
        return self._recording_manager

    @property
    def step(self) -> int:
        return self._step

    @property
    def status(self) -> str:
        return self._status

    @abstractmethod
    async def process(self, context: Dict[str, Any]) -> Dict[str, Any]:
        pass

    @abstractmethod
    def construct_messages(self, context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Construct messages for LLM reasoning.
        Context must contain 'instruction' key.
        """
        pass

    async def get_llm_response(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List] = None,
        **kwargs
    ) -> Dict[str, Any]:
        if not self._llm_client:
            raise ValueError(f"LLM client not initialized for agent {self.name}")
        
        try:
            response = await self._llm_client.complete(
                messages=messages,
                tools=tools,
                **kwargs
            )
            return response
        except Exception as e:
            logger.error(f"{self.name}: LLM call failed: {e}", exc_info=True)
            raise

    def response_to_dict(self, response: str) -> Dict[str, Any]:
        try:
            if response.strip().startswith("```json") or response.strip().startswith("```"):
                lines = response.strip().split('\n')
                if lines and lines[0].startswith('```'):
                    lines = lines[1:]
                end_idx = len(lines)
                for i, line in enumerate(lines):
                    if line.strip() == '```':
                        end_idx = i
                        break
                response = '\n'.join(lines[:end_idx])
            
            return json.loads(response)
        except json.JSONDecodeError as e:
            # If parsing fails, try to find and extract just the JSON object/array
            if "Extra data" in str(e):
                try:
                    decoder = json.JSONDecoder()
                    obj, idx = decoder.raw_decode(response)
                    logger.warning(
                        f"{self.name}: Successfully extracted JSON but found extra text after position {idx}. "
                        f"Extra text: {response[idx:idx+100]}..."
                    )
                    return obj
                except Exception as e2:
                    logger.error(f"{self.name}: Failed to extract JSON even with raw_decode: {e2}")
            
            logger.error(f"{self.name}: Failed to parse response: {e}")
            logger.error(f"{self.name}: Response content: {response[:500]}")
            return {"error": "Failed to parse response", "raw": response}

    def increment_step(self) -> None:
        self._step += 1

    @classmethod
    def _register_self(cls) -> None:
        """Register the agent class in the registry upon instantiation."""
        # Get the actual instance class, not BaseAgent
        if cls.__name__ != "BaseAgent" and cls.__name__ not in AgentRegistry._registry:
            AgentRegistry.register(cls.__name__, cls)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}(name={self.name}, step={self.step}, status={self.status})>"


class AgentStatus:
    """Constants for agent status."""
    ACTIVE = "active"
    IDLE = "idle"
    WAITING = "waiting"


class AgentRegistry:
    """
    Registry for managing agent classes.
    Allows dynamic registration and retrieval of agent types.
    """

    _registry: Dict[str, Type[BaseAgent]] = {}

    @classmethod
    def register(cls, name: str, agent_cls: Type[BaseAgent]) -> None:
        if name in cls._registry:
            logger.warning(f"Agent class '{name}' already registered, overwriting")
        cls._registry[name] = agent_cls
        logger.debug(f"Registered agent class: {name}")

    @classmethod
    def get_cls(cls, name: str) -> Type[BaseAgent]:
        if name not in cls._registry:
            raise ValueError(f"No agent class registered under '{name}'")
        return cls._registry[name]

    @classmethod
    def list_registered(cls) -> List[str]:
        return list(cls._registry.keys())

    @classmethod
    def clear(cls) -> None:
        cls._registry.clear()
        logger.debug("Agent registry cleared")