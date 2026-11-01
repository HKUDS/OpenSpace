"""High-level OpenSpace cloud auth and agent-key provisioning flow."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from openspace.cloud.account import OpenSpaceAccountClient
from openspace.cloud.client import CloudError, OpenSpaceClient
from openspace.cloud.config import (
    CloudConfig,
    DEFAULT_CLOUD_BASE_URL,
    OPENSPACE_CLOUD_API_KEY_ENV,
    OPENSPACE_CLOUD_BASE_URL_ENV,
    load_cloud_config,
    normalize_cloud_base_url,
)
from openspace.cloud.credentials import (
    read_cloud_credentials,
    save_cloud_agent_credentials,
)
from openspace.cloud.redaction import (
    redact_cloud_payload,
    redact_cloud_secret,
    secret_preview,
)

RuntimeClientFactory = Callable[[CloudConfig], Any]

_ACTIONS = {
    "register_user",
    "login_user",
    "bootstrap_agent_key",
    "list_agents",
    "rotate_agent_key",
    "verify_agent_key",
}


class CloudAuthFlowError(CloudError):
    """Raised when the auth flow cannot complete safely."""


def cloud_auth_flow(
    *,
    action: str,
    email: str | None = None,
    password: str | None = None,
    name: str | None = None,
    agent_name: str = "openspace-local-agent",
    agent_id: str | None = None,
    persist: bool = True,
    credentials_path: str | None = None,
    config: CloudConfig | None = None,
    account_client: OpenSpaceAccountClient | None = None,
    runtime_client_factory: RuntimeClientFactory | None = None,
) -> dict[str, Any]:
    """Run one Step 1 auth/provisioning action and return redacted output."""

    normalized_action = action.strip()
    if normalized_action not in _ACTIONS:
        raise CloudAuthFlowError(
            f"Unsupported cloud auth action {action!r}; expected one of {sorted(_ACTIONS)}"
        )

    flow_config = _flow_config(config, credentials_path)
    client = account_client or OpenSpaceAccountClient(flow_config)
    runtime_factory = runtime_client_factory or (lambda cfg: OpenSpaceClient(cfg))

    try:
        if normalized_action == "register_user":
            return _register_user(client, email=email, password=password, name=name)
        if normalized_action == "login_user":
            login = _login_user(client, email=email, password=password)
            return _public_login_result(login, email=email)
        if normalized_action == "bootstrap_agent_key":
            return _bootstrap_agent_key(
                client,
                runtime_factory,
                flow_config,
                email=email,
                password=password,
                name=name,
                agent_name=agent_name,
                persist=persist,
                credentials_path=credentials_path,
            )
        if normalized_action == "list_agents":
            token = _resolve_user_token(
                client,
                email=email,
                password=password,
            )
            return {
                "status": "success",
                "action": "list_agents",
                "agents": redact_cloud_payload(client.list_agents(access_token=token)),
            }
        if normalized_action == "rotate_agent_key":
            return _rotate_agent_key(
                client,
                runtime_factory,
                flow_config,
                email=email,
                password=password,
                agent_id=agent_id,
                persist=persist,
                credentials_path=credentials_path,
            )
        return _verify_agent_key(
            client,
            runtime_factory,
            flow_config,
            api_key=None,
            credentials_path=credentials_path,
        )
    except CloudError as exc:
        raise _redacted_cloud_error(exc) from exc


def _flow_config(config: CloudConfig | None, credentials_path: str | None) -> CloudConfig:
    cfg = config or load_cloud_config()
    stored = read_cloud_credentials(credentials_path) if credentials_path else {}
    explicit_base_url = config.base_url if config is not None and config.base_url else ""
    base_url = normalize_cloud_base_url(
        explicit_base_url
        or stored.get(OPENSPACE_CLOUD_BASE_URL_ENV)
        or cfg.base_url
        or DEFAULT_CLOUD_BASE_URL
    )
    api_key = (
        stored.get(OPENSPACE_CLOUD_API_KEY_ENV, "")
        if credentials_path
        else cfg.api_key
    ) or cfg.api_key
    return replace(cfg, mode="live", base_url=base_url, api_key=api_key)


def _register_user(
    client: OpenSpaceAccountClient,
    *,
    email: str | None,
    password: str | None,
    name: str | None,
) -> dict[str, Any]:
    email_value, password_value = _require_email_password(email, password)
    result = client.register_user(email=email_value, password=password_value, name=name)
    return {
        "status": "success",
        "action": "register_user",
        **_public_user_payload(result),
    }


def _login_user(
    client: OpenSpaceAccountClient,
    *,
    email: str | None,
    password: str | None,
) -> dict[str, Any]:
    email_value, password_value = _require_email_password(email, password)
    result = client.login_user(email=email_value, password=password_value)
    if not result.get("access_token"):
        raise CloudAuthFlowError("Login succeeded but response did not include access_token")
    return result


def _bootstrap_agent_key(
    client: OpenSpaceAccountClient,
    runtime_factory: RuntimeClientFactory,
    config: CloudConfig,
    *,
    email: str | None,
    password: str | None,
    name: str | None,
    agent_name: str,
    persist: bool,
    credentials_path: str | None,
) -> dict[str, Any]:
    email_value, password_value = _require_email_password(email, password)
    agent_name_value = _require_agent_name(agent_name)
    registered = _register_or_reuse_user(
        client,
        email=email_value,
        password=password_value,
        name=name,
    )
    try:
        bootstrapped_agent = client.agent_bootstrap(
            email=email_value,
            password=password_value,
            agent_name=agent_name_value,
        )
        saved = _persist_key_if_requested(
            bootstrapped_agent.get("api_key"),
            config=config,
            persist=persist,
            credentials_path=credentials_path,
        )
        verification = _verify_agent_key(
            client,
            runtime_factory,
            config,
            api_key=bootstrapped_agent.get("api_key"),
            credentials_path=credentials_path,
        )
        return {
            "status": "success",
            "action": "bootstrap_agent_key",
            "registered": _public_user_payload(registered),
            "owner": redact_cloud_payload(bootstrapped_agent.get("owner", {})),
            "agent": redact_cloud_payload(bootstrapped_agent.get("agent", {})),
            **saved,
            "verification": verification,
            "recovered_existing_agent": False,
        }
    except CloudError as exc:
        if exc.status_code != 409:
            raise

    recovered = _recover_existing_agent_key(
        client,
        runtime_factory,
        config,
        email=email_value,
        password=password_value,
        agent_name=agent_name_value,
        persist=persist,
        credentials_path=credentials_path,
    )
    recovered["registered"] = _public_user_payload(registered)
    return recovered


def _register_or_reuse_user(
    client: OpenSpaceAccountClient,
    *,
    email: str,
    password: str,
    name: str | None,
) -> dict[str, Any]:
    try:
        return client.register_user(email=email, password=password, name=name)
    except CloudError as exc:
        if exc.status_code != 409:
            raise
    login = _login_user(client, email=email, password=password)
    return {
        "user_id": login.get("user_id"),
        "identity_id": login.get("identity_id"),
        "email": email,
        "name": login.get("name"),
        "reused_existing": True,
    }


def _recover_existing_agent_key(
    client: OpenSpaceAccountClient,
    runtime_factory: RuntimeClientFactory,
    config: CloudConfig,
    *,
    email: str,
    password: str,
    agent_name: str,
    persist: bool,
    credentials_path: str | None,
) -> dict[str, Any]:
    login = client.login_user(email=email, password=password)
    token = str(login.get("access_token") or "")
    if not token:
        raise CloudAuthFlowError("Cannot recover agent key because login did not return access_token")
    agents = client.list_agents(access_token=token)
    matches = [agent for agent in agents if agent.get("name") == agent_name]
    if not matches:
        raise CloudAuthFlowError(
            "AGENT_NAME_CONFLICT_NOT_OWNED_OR_NOT_LISTED: bootstrap reported an "
            "agent name conflict, but the owner agent list does not contain that name."
        )
    if len(matches) > 1:
        raise CloudAuthFlowError(
            "AGENT_NAME_AMBIGUOUS_FOR_OWNER: multiple owner agents have the requested name."
        )
    rotated = client.rotate_agent_key(
        access_token=token,
        agent_id=str(matches[0].get("agent_id") or ""),
    )
    saved = _persist_key_if_requested(
        rotated.get("api_key"),
        config=config,
        persist=persist,
        credentials_path=credentials_path,
    )
    verification = _verify_agent_key(
        client,
        runtime_factory,
        config,
        api_key=rotated.get("api_key"),
        credentials_path=credentials_path,
    )
    return {
        "status": "success",
        "action": "bootstrap_agent_key",
        "owner": {
            "user_id": login.get("user_id"),
            "identity_id": login.get("identity_id"),
            "email": email,
            "name": login.get("name"),
        },
        "agent": redact_cloud_payload({**matches[0], **{k: v for k, v in rotated.items() if k != "api_key"}}),
        **saved,
        "verification": verification,
        "recovered_existing_agent": True,
    }


def _rotate_agent_key(
    client: OpenSpaceAccountClient,
    runtime_factory: RuntimeClientFactory,
    config: CloudConfig,
    *,
    email: str | None,
    password: str | None,
    agent_id: str | None,
    persist: bool,
    credentials_path: str | None,
) -> dict[str, Any]:
    agent_id_value = str(agent_id or "").strip()
    if not agent_id_value:
        raise CloudAuthFlowError("agent_id is required for rotate_agent_key")
    token = _resolve_user_token(
        client,
        email=email,
        password=password,
    )
    rotated = client.rotate_agent_key(access_token=token, agent_id=agent_id_value)
    saved = _persist_key_if_requested(
        rotated.get("api_key"),
        config=config,
        persist=persist,
        credentials_path=credentials_path,
    )
    verification = _verify_agent_key(
        client,
        runtime_factory,
        config,
        api_key=rotated.get("api_key"),
        credentials_path=credentials_path,
    )
    return {
        "status": "success",
        "action": "rotate_agent_key",
        "agent": redact_cloud_payload({k: v for k, v in rotated.items() if k != "api_key"}),
        **saved,
        "verification": verification,
    }


def _verify_agent_key(
    client: OpenSpaceAccountClient,
    runtime_factory: RuntimeClientFactory,
    config: CloudConfig,
    *,
    api_key: str | None,
    credentials_path: str | None,
) -> dict[str, Any]:
    key = _resolve_agent_key(api_key=api_key, config=config, credentials_path=credentials_path)
    runtime_config = replace(config, mode="live", api_key=key)
    try:
        smoke = runtime_factory(runtime_config).smoke()
        return {
            "status": "success",
            "action": "verify_agent_key",
            "method": "v2_smoke",
            "has_api_key": True,
            "api_key_preview": secret_preview(key),
            "principal": redact_cloud_payload(smoke),
        }
    except CloudError as exc:
        if exc.status_code in {401, 403}:
            raise CloudAuthFlowError(
                "Current agent key is invalid or revoked. Run "
                'cloud_auth_flow(action="bootstrap_agent_key") or '
                'cloud_auth_flow(action="rotate_agent_key").',
                status_code=exc.status_code,
                body=redact_cloud_secret(exc.body),
            ) from exc
        if exc.status_code not in {404, 405, 501}:
            raise
    principal = client.me(api_key=key)
    return {
        "status": "success",
        "action": "verify_agent_key",
        "method": "v1_auth_me",
        "has_api_key": True,
        "api_key_preview": secret_preview(key),
        "principal": redact_cloud_payload(principal),
    }


def _resolve_user_token(
    client: OpenSpaceAccountClient,
    *,
    email: str | None,
    password: str | None,
) -> str:
    login = _login_user(client, email=email, password=password)
    return str(login["access_token"])


def _persist_key_if_requested(
    api_key: Any,
    *,
    config: CloudConfig,
    persist: bool,
    credentials_path: str | None,
) -> dict[str, Any]:
    key = str(api_key or "")
    if not key:
        raise CloudAuthFlowError("Agent key response did not include api_key")
    result = {
        "has_api_key": True,
        "api_key_preview": secret_preview(key),
        "api_key_saved": False,
    }
    if persist:
        path = save_cloud_agent_credentials(
            api_key=key,
            base_url=config.base_url,
            path=credentials_path,
        )
        result["api_key_saved"] = True
        result["credentials_path"] = str(path)
    return result


def _resolve_agent_key(
    *,
    api_key: str | None,
    config: CloudConfig,
    credentials_path: str | None,
) -> str:
    explicit = str(api_key or "").strip()
    if explicit:
        return explicit
    if config.api_key:
        return config.api_key
    stored = read_cloud_credentials(credentials_path) if credentials_path else {}
    key = stored.get(OPENSPACE_CLOUD_API_KEY_ENV, "")
    if key:
        return key
    raise CloudAuthFlowError("OPENSPACE_CLOUD_API_KEY is required to verify the agent key")


def _require_email_password(email: str | None, password: str | None) -> tuple[str, str]:
    email_value = str(email or "").strip()
    if not email_value:
        raise CloudAuthFlowError("email is required")
    password_value = "" if password is None else str(password)
    if not (8 <= len(password_value) <= 72):
        raise CloudAuthFlowError("password must be 8 to 72 characters")
    return email_value, password_value


def _require_agent_name(agent_name: str | None) -> str:
    value = str(agent_name or "").strip()
    if not value:
        raise CloudAuthFlowError("agent_name is required")
    return value


def _public_login_result(login: dict[str, Any], *, email: str | None) -> dict[str, Any]:
    return {
        "status": "success",
        "action": "login_user",
        "user_id": login.get("user_id"),
        "identity_id": login.get("identity_id"),
        "email": email,
        "name": login.get("name"),
        "token_type": login.get("token_type"),
        "expires_at": login.get("expires_at"),
        "has_access_token": bool(login.get("access_token")),
    }


def _public_user_payload(payload: dict[str, Any] | list[Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    clean = dict(payload)
    clean.pop("password", None)
    if isinstance(clean.get("profile"), dict):
        clean["profile"] = _public_user_payload(clean["profile"])
    return redact_cloud_payload(clean)


def _redacted_cloud_error(exc: CloudError) -> CloudError:
    if isinstance(exc, CloudAuthFlowError):
        return exc
    return CloudError(
        redact_cloud_secret(str(exc)),
        status_code=exc.status_code,
        body=redact_cloud_secret(exc.body),
        code=exc.code,
        kind=exc.kind,
        retryable=exc.retryable,
        field_errors=exc.field_errors,
        suggested_action=exc.suggested_action,
        request_id=exc.request_id,
        details=exc.details,
    )
