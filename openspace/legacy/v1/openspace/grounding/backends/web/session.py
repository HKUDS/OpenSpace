import os
from pathlib import Path
from typing import Dict, Any, Optional
from openspace.grounding.core.session import BaseSession
from openspace.grounding.core.types import BackendType, SessionConfig
from openspace.grounding.core.tool import BaseTool
from openspace.grounding.core.transport.connectors import BaseConnector
from openspace.llm import LLMClient
from openspace.utils.logging import Logger
from dotenv import load_dotenv

# Load .env from openspace package root (4 levels up), then CWD fallback.
_PKG_ENV = Path(__file__).resolve().parent.parent.parent.parent / ".env"  # openspace/.env
if _PKG_ENV.is_file():
    load_dotenv(_PKG_ENV)
load_dotenv()
logger = Logger.get_logger(__name__)


try:
    from openai import AsyncOpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False


class WebConnector(BaseConnector):
    def __init__(self, api_key: str, base_url: str):
        self.api_key = api_key
        self.base_url = base_url
        self.client: Optional[AsyncOpenAI] = None
        self._connected = False
    
    async def connect(self) -> None:
        if self._connected:
            return
        
        if not OPENAI_AVAILABLE:
            raise RuntimeError(
                "OpenAI library not available. Install with: pip install openai"
            )
        
        if not self.api_key:
            raise RuntimeError(
                "API key not provided. Set OPENROUTER_API_KEY environment variable "
                "or provide deep_research_api_key in config."
            )
        
        self.client = AsyncOpenAI(
            base_url=self.base_url,
            api_key=self.api_key
        )
        self._connected = True
        logger.info(f"Web connector connected to {self.base_url}")
    
    async def disconnect(self) -> None:
        if not self._connected:
            return
        
        self.client = None
        self._connected = False
        logger.info("Web connector disconnected")
    
    @property
    def is_connected(self) -> bool:
        return self._connected
    
    async def invoke(self, name: str, params: dict) -> Any:
        if name == "chat_completion":
            if not self.client:
                raise RuntimeError("Client not connected")
            return await self.client.chat.completions.create(**params)
        raise NotImplementedError(f"Unknown method: {name}")
    
    async def request(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("Web backend uses invoke() instead of request()")


class WebSession(BaseSession):
    
    backend_type = BackendType.WEB
    
    def __init__(
        self,
        *,
        session_id: str,
        config: SessionConfig,
        deep_research_api_key: Optional[str] = None,
        deep_research_base_url: str = "https://openrouter.ai/api/v1",
        auto_connect: bool = True,
        auto_initialize: bool = True
    ):
        api_key = deep_research_api_key or os.getenv("OPENROUTER_API_KEY")
        connector = WebConnector(
            api_key=api_key or "",  # Empty string will raise an error when connect
            base_url=deep_research_base_url
        )
        
        super().__init__(
            connector=connector,
            session_id=session_id,
            backend_type=BackendType.WEB,
            auto_connect=auto_connect,
            auto_initialize=auto_initialize
        )
        self.config = config
    
    @property
    def web_connector(self) -> WebConnector:
        return self.connector
    
    async def initialize(self) -> Dict[str, Any]:
        """Connect to WebConnector and register tools.

        BaseSession in __aenter__ will call connect() according to auto_connect,
        but in provider.create_session directly instantiating Session will not trigger this logic.
        Therefore, we need to explicitly ensure that the connection is established, avoiding AttributeError
        when DeepResearchTool is called and `self.web_connector.client` is still None.
        """

        # If the connection is not established, connect explicitly
        if not self.is_connected:
            try:
                await self.connect()
            except Exception as e:
                logger.error(f"Failed to connect WebSession {self.session_id}: {e}")
                raise

        if self.tools:
            logger.debug(f"Web session {self.session_id} already initialized, skipping")
            return {
                "tools": [t.name for t in self.tools],
                "backend": BackendType.WEB.value
            }

        self.tools = [DeepResearchTool(session=self)]
        
        logger.info(f"Initialized Web session {self.session_id} with AI Deep Research tool")
        
        return {
            "tools": [t.name for t in self.tools],
            "backend": BackendType.WEB.value
        }


class DeepResearchTool(BaseTool):
    
    backend_type = BackendType.WEB
    _name = "deep_research_agent"
    _description = """Knowledge Research Tool - Primary tool for acquiring external knowledge

PURPOSE:
Acquires comprehensive knowledge from the web through deep research and analysis.
Powered by Perplexity AI's sonar-deep-research model, then post-processed to extract
actionable insights and concise summaries. The main tool for gathering information
beyond existing knowledge base.

WHEN TO USE:
- Information needed on professional/technical topics
- Research on technical problems, concepts, or implementations  
- Understanding of latest developments, trends, or news
- Comparison of different approaches, tools, or solutions
- Factual information, definitions, or explanations required
- Synthesis from multiple authoritative sources needed

HOW IT WORKS:
1. Conducts deep web search using Perplexity's sonar-deep-research
2. Analyzes and synthesizes information from multiple sources
3. Post-processes to distill knowledge-dense summary retaining critical details
4. Returns comprehensive summary ready for immediate use

RETURNS:
Knowledge-dense comprehensive summary (400-600 words) that:
- Retains important details and technical specifics
- Focuses on substantive knowledge without losing critical information
- Organized and structured for clarity
- Directly usable by agents for decision-making and task execution

NOT DESIGNED FOR:
- Tasks requiring browser interaction or UI manipulation
- Direct file downloads or web scraping operations
- Real-time system operations or executions

USAGE GUIDELINES:
- Frame clear, specific questions (e.g., "Explain the architecture of Transformer models")
- Specify context when needed (e.g., "Compare PostgreSQL vs MySQL for high-concurrency scenarios")
- Suitable for any knowledge or information acquisition needs
"""
    
    def __init__(
        self,
        session: WebSession
    ):
        super().__init__()
        self._session = session
        self._llm = LLMClient()
        
    async def _arun(self, query: str) -> str:
        if not query:
            return "ERROR: Missing required parameter: query"
        
        try:
            # Step 1: Deep research
            logger.info(f"Start deep research: {query}")
            
            completion = await self._session.web_connector.client.chat.completions.create(
                model="perplexity/sonar-deep-research",
                messages=[{"role": "user", "content": query}]
            )
            
            full_answer = completion.choices[0].message.content
            logger.info(f"Research completed, length: {len(full_answer)} characters")
            
            # Step 2: Use LLMClient to generate summary and distill key points
            logger.info(f"Begin to distill key points...")
            
            SUMMARY_AGENT_PROMPT = f"""Please distill the following deep research results into a knowledge-dense summary. Requirements:

Provide a comprehensive yet concise summary (400-600 words):
- Focus on SUBSTANTIVE knowledge and key information
- Retain important details, technical specifics, and concrete facts
- Do NOT sacrifice critical information for brevity
- Organize information clearly and logically with proper structure
- Remove only redundancy and verbose explanations
- Include actionable insights and decision-relevant information
- Make it directly usable for task execution and decision-making

Output ONLY the summary text, no additional formatting or JSON structure needed.

Deep Research Results:
{full_answer}
"""
            
            summary_response = await self._llm.complete(SUMMARY_AGENT_PROMPT)
            summary = summary_response["message"]["content"].strip()
            
            logger.info(f"Summary generation completed")
            
            return summary
            
        except Exception as e:
            logger.error(f"Deep research failed: {e}")
            return f"ERROR: AI research failed: {e}"