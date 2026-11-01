from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from openspace.host_detection import load_runtime_env
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


class GatewayServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(8765, ge=1, le=65535)
    health_path: str = "/health"

    @field_validator("health_path")
    @classmethod
    def validate_health_path(cls, value: str) -> str:
        value = value.strip() or "/health"
        if not value.startswith("/"):
            value = "/" + value
        return value


class AgentExecutionConfig(BaseModel):
    max_iterations: int = Field(20, ge=1, le=200)
    enable_recording: bool = True
    recording_backends: List[str] = Field(default_factory=lambda: ["shell"])
    backend_scope: Optional[List[str]] = None
    grounding_config_path: Optional[str] = None
    workspace_root: Optional[str] = None
    llm_timeout: float = Field(120.0, ge=1.0, le=3600.0)


class SessionProcessingConfig(BaseModel):
    history_max_turns: int = Field(12, ge=1, le=100)
    max_parallel_sessions: int = Field(2, ge=1, le=64)
    idle_ttl_seconds: int = Field(900, ge=30, le=86400)
    per_session_queue_size: int = Field(32, ge=1, le=512)
    whatsapp_poll_interval_seconds: float = Field(1.0, ge=0.1, le=60.0)
    max_attachment_bytes: int = Field(25 * 1024 * 1024, ge=1, le=512 * 1024 * 1024)
    max_session_attachment_bytes: int = Field(
        100 * 1024 * 1024,
        ge=1,
        le=10 * 1024 * 1024 * 1024,
    )


class ChannelAccessConfig(BaseModel):
    enabled: bool = False
    allow_all_users: bool = False
    allowed_users: List[str] = Field(default_factory=list)
    allow_dm: bool = True
    allow_groups: bool = True
    group_policy: Literal["disabled", "mention_only", "reply_or_mention", "all"] = "reply_or_mention"


class WhatsAppBridgeConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(3000, ge=1, le=65535)
    script_path: Optional[str] = None
    session_dir: Optional[str] = None
    mode: Literal["self-chat", "bot"] = "self-chat"
    auto_install_dependencies: bool = True
    token: Optional[str] = None
    enforce_loopback: bool = True

    @model_validator(mode="after")
    def validate_loopback_constraints(self) -> "WhatsAppBridgeConfig":
        host = self.host.strip().lower() or "127.0.0.1"
        if self.enforce_loopback and host not in {"127.0.0.1", "localhost"}:
            raise ValueError(
                "WhatsApp bridge host must be loopback when enforce_loopback is enabled"
            )
        self.host = host
        return self

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://{self.listen_host}:{self.port}"

    @property
    def listen_host(self) -> str:
        return "127.0.0.1" if self.host == "localhost" else self.host


class WhatsAppConfig(ChannelAccessConfig):
    bridge: WhatsAppBridgeConfig = Field(default_factory=WhatsAppBridgeConfig)
    reply_prefix: Optional[str] = None


class FeishuConfig(ChannelAccessConfig):
    app_id: Optional[str] = None
    app_secret: Optional[str] = None
    domain: Literal["feishu", "lark"] = "feishu"
    connection_mode: Literal["webhook", "websocket"] = "webhook"
    verification_token: Optional[str] = None
    encrypt_key: Optional[str] = None
    bot_open_id: Optional[str] = None
    webhook_path: str = "/feishu/webhook"

    @model_validator(mode="after")
    def validate_webhook_requirements(self) -> "FeishuConfig":
        if self.enabled and self.connection_mode == "webhook" and not (self.verification_token or "").strip():
            raise ValueError("Feishu webhook mode requires verification_token")
        return self

    @field_validator("webhook_path")
    @classmethod
    def validate_webhook_path(cls, value: str) -> str:
        value = value.strip() or "/feishu/webhook"
        if not value.startswith("/"):
            value = "/" + value
        return value

    @model_validator(mode="after")
    def validate_webhook_security(self) -> "FeishuConfig":
        if self.enabled and self.connection_mode == "webhook":
            token = (self.verification_token or "").strip()
            if not token:
                raise ValueError(
                    "Feishu webhook mode requires verification_token when enabled"
                )
            self.verification_token = token
        if self.encrypt_key is not None:
            self.encrypt_key = self.encrypt_key.strip() or None
        if self.bot_open_id is not None:
            self.bot_open_id = self.bot_open_id.strip() or None
        return self


class CommunicationConfig(BaseModel):
    data_dir: str = Field(
        default_factory=lambda: str(
            Path(__file__).resolve().parents[2] / "logs" / "communication"
        )
    )
    server: GatewayServerConfig = Field(default_factory=GatewayServerConfig)
    agent: AgentExecutionConfig = Field(default_factory=AgentExecutionConfig)
    sessions: SessionProcessingConfig = Field(default_factory=SessionProcessingConfig)
    whatsapp: WhatsAppConfig = Field(default_factory=WhatsAppConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)

    @property
    def openspace(self) -> AgentExecutionConfig:
        return self.agent

    @property
    def runtime(self) -> SessionProcessingConfig:
        return self.sessions

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir).expanduser().resolve()

    @property
    def sessions_dir(self) -> Path:
        return self.data_path / "sessions"

    @property
    def bridge_assets_dir(self) -> Path:
        return Path(__file__).resolve().parent / "bridges" / "whatsapp"

    @property
    def runtime_status_path(self) -> Path:
        return self.data_path / "runtime_status.json"

    @property
    def locks_dir(self) -> Path:
        return self.data_path / "locks"

    @property
    def bridge_tokens_dir(self) -> Path:
        return self.data_path / "bridge_tokens"

    @property
    def whatsapp_bridge_token_path(self) -> Path:
        return self.bridge_tokens_dir / "whatsapp.token"

    @property
    def outbound_media_dir(self) -> Path:
        return self.data_path / "outbound_media"

    @property
    def feishu_seen_message_ids_path(self) -> Path:
        return self.data_path / "feishu_seen_message_ids.json"

    @property
    def enabled_platforms(self) -> List[str]:
        platforms: List[str] = []
        if self.whatsapp.enabled:
            platforms.append("whatsapp")
        if self.feishu.enabled:
            platforms.append("feishu")
        return platforms


def load_communication_config(path: Optional[str] = None) -> CommunicationConfig:
    load_runtime_env()
    config_path = _resolve_config_path(path)
    raw: Dict[str, Any] = {}

    if config_path and config_path.is_file():
        with open(config_path, "r", encoding="utf-8") as handle:
            raw = json.load(handle) or {}
        raw = _normalize_legacy_keys(raw)
        logger.info("Loaded communication config: %s", config_path)

    config = CommunicationConfig.model_validate(raw)
    _apply_env_overrides(config)
    return CommunicationConfig.model_validate(config.model_dump(mode="python"))


def _resolve_config_path(path: Optional[str]) -> Optional[Path]:
    explicit_path = Path(path).expanduser() if path else None
    if explicit_path is not None:
        if not explicit_path.is_file():
            raise FileNotFoundError(f"Communication config file not found: {explicit_path}")
        return explicit_path

    env_path = (
        Path(os.environ["OPENSPACE_COMMUNICATION_CONFIG"]).expanduser()
        if os.environ.get("OPENSPACE_COMMUNICATION_CONFIG")
        else None
    )
    if env_path is not None:
        if not env_path.is_file():
            raise FileNotFoundError(f"Communication config file not found: {env_path}")
        return env_path

    default_path = Path(__file__).resolve().parents[1] / "config" / "config_communication.json"
    return default_path if default_path.is_file() else None


def _apply_env_overrides(config: CommunicationConfig) -> None:
    _maybe_set_bool(config.whatsapp, "enabled", os.getenv("WHATSAPP_ENABLED"))
    _maybe_set_bool(config.whatsapp, "allow_all_users", os.getenv("WHATSAPP_ALLOW_ALL_USERS"))
    _maybe_set_list(config.whatsapp, "allowed_users", os.getenv("WHATSAPP_ALLOWED_USERS"))
    _maybe_set_bool(config.whatsapp, "allow_dm", os.getenv("WHATSAPP_ALLOW_DM"))
    _maybe_set_bool(config.whatsapp, "allow_groups", os.getenv("WHATSAPP_ALLOW_GROUPS"))
    _maybe_set_str(config.whatsapp, "group_policy", os.getenv("WHATSAPP_GROUP_POLICY"))
    _maybe_set_str(config.whatsapp.bridge, "host", os.getenv("WHATSAPP_BRIDGE_HOST"))
    _maybe_set_int(config.whatsapp.bridge, "port", os.getenv("WHATSAPP_BRIDGE_PORT"))
    _maybe_set_str(config.whatsapp.bridge, "script_path", os.getenv("WHATSAPP_BRIDGE_SCRIPT"))
    _maybe_set_str(config.whatsapp.bridge, "session_dir", os.getenv("WHATSAPP_SESSION_DIR"))
    _maybe_set_str(config.whatsapp.bridge, "mode", os.getenv("WHATSAPP_MODE"))
    _maybe_set_str(config.whatsapp.bridge, "token", os.getenv("WHATSAPP_BRIDGE_TOKEN"))
    _maybe_set_bool(config.whatsapp.bridge, "enforce_loopback", os.getenv("WHATSAPP_BRIDGE_ENFORCE_LOOPBACK"))
    _maybe_set_str(config.whatsapp, "reply_prefix", os.getenv("WHATSAPP_REPLY_PREFIX"))

    _maybe_set_bool(config.feishu, "enabled", os.getenv("FEISHU_ENABLED"))
    _maybe_set_bool(config.feishu, "allow_all_users", os.getenv("FEISHU_ALLOW_ALL_USERS"))
    _maybe_set_list(config.feishu, "allowed_users", os.getenv("FEISHU_ALLOWED_USERS"))
    _maybe_set_bool(config.feishu, "allow_dm", os.getenv("FEISHU_ALLOW_DM"))
    _maybe_set_bool(config.feishu, "allow_groups", os.getenv("FEISHU_ALLOW_GROUPS"))
    _maybe_set_str(config.feishu, "group_policy", os.getenv("FEISHU_GROUP_POLICY"))
    _maybe_set_str(config.feishu, "app_id", os.getenv("FEISHU_APP_ID"))
    _maybe_set_str(config.feishu, "app_secret", os.getenv("FEISHU_APP_SECRET"))
    _maybe_set_str(config.feishu, "verification_token", os.getenv("FEISHU_VERIFICATION_TOKEN"))
    _maybe_set_str(config.feishu, "encrypt_key", os.getenv("FEISHU_ENCRYPT_KEY"))
    _maybe_set_str(config.feishu, "bot_open_id", os.getenv("FEISHU_BOT_OPEN_ID"))
    _maybe_set_str(config.feishu, "domain", os.getenv("FEISHU_DOMAIN"))
    _maybe_set_str(config.feishu, "connection_mode", os.getenv("FEISHU_CONNECTION_MODE"))
    _maybe_set_str(config.feishu, "webhook_path", os.getenv("FEISHU_WEBHOOK_PATH"))

    _maybe_set_str(config, "data_dir", os.getenv("OPENSPACE_COMMUNICATION_DATA_DIR"))
    _maybe_set_str(config.server, "host", os.getenv("OPENSPACE_COMMUNICATION_HOST"))
    _maybe_set_int(config.server, "port", os.getenv("OPENSPACE_COMMUNICATION_PORT"))
    _maybe_set_int(
        config.agent,
        "max_iterations",
        os.getenv("OPENSPACE_COMMUNICATION_MAX_ITERATIONS") or os.getenv("OPENSPACE_MAX_ITERATIONS"),
    )
    _maybe_set_bool(
        config.agent,
        "enable_recording",
        os.getenv("OPENSPACE_COMMUNICATION_ENABLE_RECORDING") or os.getenv("OPENSPACE_ENABLE_RECORDING"),
    )
    _maybe_set_list(
        config.agent,
        "recording_backends",
        os.getenv("OPENSPACE_COMMUNICATION_RECORDING_BACKENDS"),
    )
    _maybe_set_list(
        config.agent,
        "backend_scope",
        os.getenv("OPENSPACE_COMMUNICATION_BACKEND_SCOPE") or os.getenv("OPENSPACE_BACKEND_SCOPE"),
    )
    _maybe_set_str(
        config.agent,
        "grounding_config_path",
        os.getenv("OPENSPACE_COMMUNICATION_GROUNDING_CONFIG_PATH") or os.getenv("OPENSPACE_CONFIG_PATH"),
    )
    _maybe_set_str(config.agent, "workspace_root", os.getenv("OPENSPACE_COMMUNICATION_WORKSPACE_ROOT"))
    _maybe_set_float(config.agent, "llm_timeout", os.getenv("OPENSPACE_COMMUNICATION_LLM_TIMEOUT"))
    _maybe_set_int(config.sessions, "history_max_turns", os.getenv("OPENSPACE_COMMUNICATION_HISTORY_TURNS"))
    _maybe_set_int(config.sessions, "max_parallel_sessions", os.getenv("OPENSPACE_COMMUNICATION_MAX_PARALLEL"))
    _maybe_set_int(config.sessions, "idle_ttl_seconds", os.getenv("OPENSPACE_COMMUNICATION_IDLE_TTL"))
    _maybe_set_int(config.sessions, "per_session_queue_size", os.getenv("OPENSPACE_COMMUNICATION_QUEUE_SIZE"))
    _maybe_set_int(
        config.sessions,
        "max_attachment_bytes",
        os.getenv("OPENSPACE_COMMUNICATION_MAX_ATTACHMENT_BYTES"),
    )
    _maybe_set_int(
        config.sessions,
        "max_session_attachment_bytes",
        os.getenv("OPENSPACE_COMMUNICATION_MAX_SESSION_ATTACHMENT_BYTES"),
    )
    _maybe_set_float(
        config.sessions,
        "whatsapp_poll_interval_seconds",
        os.getenv("OPENSPACE_COMMUNICATION_WHATSAPP_POLL_INTERVAL"),
    )


def _normalize_legacy_keys(raw: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(raw)
    if "agent" not in normalized and "openspace" in normalized:
        normalized["agent"] = normalized["openspace"]
    if "sessions" not in normalized and "runtime" in normalized:
        normalized["sessions"] = normalized["runtime"]
    return normalized


def _maybe_set_bool(target: Any, field_name: str, raw: Optional[str]) -> None:
    if raw is None:
        return
    lowered = raw.strip().lower()
    if lowered in {"true", "1", "yes", "on"}:
        setattr(target, field_name, True)
    elif lowered in {"false", "0", "no", "off"}:
        setattr(target, field_name, False)


def _maybe_set_int(target: Any, field_name: str, raw: Optional[str]) -> None:
    if raw is None or not raw.strip():
        return
    try:
        setattr(target, field_name, int(raw))
    except ValueError:
        logger.warning("Invalid integer for %s: %r", field_name, raw)


def _maybe_set_list(target: Any, field_name: str, raw: Optional[str]) -> None:
    if raw is None:
        return
    values = [item.strip() for item in raw.split(",") if item.strip()]
    setattr(target, field_name, values)


def _maybe_set_float(target: Any, field_name: str, raw: Optional[str]) -> None:
    if raw is None or not raw.strip():
        return
    try:
        setattr(target, field_name, float(raw))
    except ValueError:
        logger.warning("Invalid float for %s: %r", field_name, raw)


def _maybe_set_str(target: Any, field_name: str, raw: Optional[str]) -> None:
    if raw is None:
        return
    value = raw.strip()
    if value:
        setattr(target, field_name, value)
