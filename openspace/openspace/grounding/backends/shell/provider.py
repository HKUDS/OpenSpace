from openspace.grounding.core.provider import Provider
from openspace.grounding.core.types import BackendType, SessionConfig
from .session import ShellSession
from .transport.local_connector import LocalShellConnector
from openspace.config import get_config
from openspace.config.utils import get_config_value
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


class ShellProvider(Provider[ShellSession]):
    
    DEFAULT_SID = BackendType.SHELL.value
    
    def __init__(self, config: dict | None = None):
        super().__init__(BackendType.SHELL, config)
        # Note: _setup_security_policy() is already called by parent class __init__

    def _setup_security_policy(self, config: dict | None = None):
        security_policy = get_config().get_security_policy(self.backend_type.value)
    
        if config:
            security_config = get_config_value(config, "security", None)
            if security_config:
                for key, value in security_config.items():
                    if hasattr(security_policy, key):
                        setattr(security_policy, key, value)
            
            sandbox_enabled = get_config_value(config, "sandbox_enabled", None)
            if sandbox_enabled is not None:
                security_policy.sandbox_enabled = sandbox_enabled
        
        logger.info(f"Shell security policy: allow_shell_commands={security_policy.allow_shell_commands}, "
                   f"blocked_commands={security_policy.blocked_commands}")
        
        self.security_manager.set_backend_policy(BackendType.SHELL, security_policy)

    async def initialize(self) -> None:
        if not self.is_initialized:
            await self.create_session(SessionConfig(
                session_name=self.DEFAULT_SID,
                backend_type=BackendType.SHELL,
                connection_params={}
            ))
            self.is_initialized = True

    async def create_session(self, session_config: SessionConfig) -> ShellSession:
        del session_config
        sid = self.DEFAULT_SID
        if sid in self._sessions:
            return self._sessions[sid]
        
        # Use the config passed to ShellProvider (from GroundingClient),
        # falling back to global config only if not available.
        shell_config = self.config if self.config else get_config().get_backend_config("shell")
        
        # Shell execution must stay local so sandbox decisions and background
        # task lifecycle share the same process-level TaskManager.
        mode = getattr(shell_config, "mode", "local")
        if mode != "local":
            raise ValueError(
                "Shell backend server mode is unsupported. Use mode='local'; "
                "local_server is a private transport and does not provide the "
                "shell task lifecycle/sandbox contract."
            )

        logger.info("Shell backend using LOCAL mode (no server required)")
        connector = LocalShellConnector(
            retry_times=shell_config.max_retries,
            retry_interval=shell_config.retry_interval,
            security_manager=self.security_manager,
        )
        
        # Create session with config parameters
        session = ShellSession(
            connector=connector,
            session_id=sid,
            security_manager=self.security_manager,
            default_working_dir=shell_config.working_dir,
            default_env=shell_config.env,
            default_conda_env=shell_config.conda_env,
            use_clawwork_productivity=getattr(shell_config, "use_clawwork_productivity", False),
            productivity_date=getattr(shell_config, "productivity_date", "default"),
        )
        
        await session.initialize()
        self._sessions[sid] = session
        return session

    async def close_session(self, session_id: str) -> None:
        sess = self._sessions.pop(session_id, None)
        if sess:
            await sess.disconnect()
