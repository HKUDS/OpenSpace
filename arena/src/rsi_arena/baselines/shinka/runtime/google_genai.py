import logging
import os
import socket
import threading
from typing import Any

logger = logging.getLogger(__name__)

GOOGLE_GENAI_IP_FAMILY_ENV = "SHINKA_GOOGLE_GENAI_IP_FAMILY"
GOOGLE_GENAI_IP_PROBE_TIMEOUT_ENV = "SHINKA_GOOGLE_GENAI_IP_PROBE_TIMEOUT"

_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}
_GOOGLE_GENAI_IP_POLICIES = {"auto", "system", "ipv4", "ipv4_first"}
_GOOGLE_GENAI_IP_POLICY_ALIASES = {
    "ipv4-first": "ipv4_first",
    "prefer_ipv4": "ipv4_first",
    "prefer-ipv4": "ipv4_first",
    "v4": "ipv4",
    "v4_first": "ipv4_first",
    "v4-first": "ipv4_first",
}
_GOOGLE_GENAI_PROBE_HOST = "generativelanguage.googleapis.com"
_GOOGLE_GENAI_PROBE_PORT = 443
_GOOGLE_GENAI_PROBE_TIMEOUT_SECONDS = 0.3
_GOOGLE_GENAI_PROBE_CANDIDATES = 2

_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_NETWORK_LOCK = threading.Lock()
_ACTIVE_GETADDRINFO_POLICY: str | None = None
_DETECTED_IP_POLICY: str | None = None
_LOGGED_AUTO_FALLBACK = False


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUE_ENV_VALUES


def _google_genai_timeout_ms(timeout_seconds: int) -> int:
    """Convert a second-based timeout to google-genai milliseconds."""
    return int(timeout_seconds * 1000)


def google_genai_auth_mode() -> str:
    """Return the configured Google GenAI auth mode."""
    return "vertexai" if _env_flag("GOOGLE_GENAI_USE_VERTEXAI") else "api_key"


def configure_google_genai_network() -> str:
    """Configure Google API address ordering and return the active policy."""
    requested_policy = _google_genai_ip_policy()
    active_policy = (
        _detect_google_genai_ip_policy()
        if requested_policy == "auto"
        else requested_policy
    )

    if requested_policy == "auto" and active_policy == "ipv4_first":
        _log_auto_fallback_once()

    if active_policy == "system":
        _restore_google_api_getaddrinfo()
    else:
        _install_google_api_getaddrinfo_policy(active_policy)
    return active_policy


def _google_genai_ip_policy() -> str:
    raw_policy = os.getenv(GOOGLE_GENAI_IP_FAMILY_ENV, "auto").strip().lower()
    policy = _GOOGLE_GENAI_IP_POLICY_ALIASES.get(raw_policy, raw_policy)
    if policy not in _GOOGLE_GENAI_IP_POLICIES:
        valid_policies = ", ".join(sorted(_GOOGLE_GENAI_IP_POLICIES))
        raise ValueError(
            f"{GOOGLE_GENAI_IP_FAMILY_ENV} must be one of: {valid_policies}."
        )
    return policy


def _detect_google_genai_ip_policy() -> str:
    global _DETECTED_IP_POLICY

    with _NETWORK_LOCK:
        if _DETECTED_IP_POLICY is None:
            _DETECTED_IP_POLICY = _probe_google_genai_ip_policy()
        return _DETECTED_IP_POLICY


def _probe_google_genai_ip_policy() -> str:
    try:
        addresses = _ORIGINAL_GETADDRINFO(
            _GOOGLE_GENAI_PROBE_HOST,
            _GOOGLE_GENAI_PROBE_PORT,
            type=socket.SOCK_STREAM,
        )
    except OSError:
        return "system"

    has_ipv6 = any(address[0] == socket.AF_INET6 for address in addresses)
    has_ipv4 = any(address[0] == socket.AF_INET for address in addresses)
    if not has_ipv6 or not has_ipv4:
        return "system"
    if _address_family_connects(addresses, socket.AF_INET6):
        return "system"
    if _address_family_connects(addresses, socket.AF_INET):
        return "ipv4_first"
    return "system"


def _address_family_connects(addresses: list[Any], family: socket.AddressFamily) -> bool:
    timeout = _google_genai_ip_probe_timeout_seconds()
    checked = 0
    for address in addresses:
        if address[0] != family:
            continue
        checked += 1
        _, socktype, proto, _, sockaddr = address
        try:
            with socket.socket(family, socktype, proto) as sock:
                sock.settimeout(timeout)
                sock.connect(sockaddr)
            return True
        except OSError:
            if checked >= _GOOGLE_GENAI_PROBE_CANDIDATES:
                return False
    return False


def _google_genai_ip_probe_timeout_seconds() -> float:
    raw_timeout = os.getenv(GOOGLE_GENAI_IP_PROBE_TIMEOUT_ENV, "").strip()
    if not raw_timeout:
        return _GOOGLE_GENAI_PROBE_TIMEOUT_SECONDS
    try:
        timeout = float(raw_timeout)
    except ValueError as exc:
        raise ValueError(
            f"{GOOGLE_GENAI_IP_PROBE_TIMEOUT_ENV} must be a positive number."
        ) from exc
    if timeout <= 0:
        raise ValueError(
            f"{GOOGLE_GENAI_IP_PROBE_TIMEOUT_ENV} must be a positive number."
        )
    return timeout


def _install_google_api_getaddrinfo_policy(policy: str) -> None:
    global _ACTIVE_GETADDRINFO_POLICY

    with _NETWORK_LOCK:
        _ACTIVE_GETADDRINFO_POLICY = policy
        if socket.getaddrinfo is not _google_api_getaddrinfo:
            socket.getaddrinfo = _google_api_getaddrinfo


def _restore_google_api_getaddrinfo() -> None:
    global _ACTIVE_GETADDRINFO_POLICY

    with _NETWORK_LOCK:
        _ACTIVE_GETADDRINFO_POLICY = None
        if socket.getaddrinfo is _google_api_getaddrinfo:
            socket.getaddrinfo = _ORIGINAL_GETADDRINFO


def _google_api_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    addresses = _ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)
    policy = _ACTIVE_GETADDRINFO_POLICY
    if (
        policy is None
        or family not in (0, socket.AF_UNSPEC)
        or not _is_google_api_host(host)
    ):
        return addresses
    return _google_api_addresses_for_policy(addresses, policy)


def _google_api_addresses_for_policy(addresses: list[Any], policy: str) -> list[Any]:
    ipv4_addresses = [address for address in addresses if address[0] == socket.AF_INET]
    if policy == "ipv4":
        return ipv4_addresses or addresses
    if policy == "ipv4_first" and ipv4_addresses:
        non_ipv4_addresses = [
            address for address in addresses if address[0] != socket.AF_INET
        ]
        return ipv4_addresses + non_ipv4_addresses
    return addresses


def _is_google_api_host(host: object) -> bool:
    normalized_host = _normalize_host(host)
    return normalized_host == "googleapis.com" or normalized_host.endswith(
        ".googleapis.com"
    )


def _normalize_host(host: object) -> str:
    if host is None:
        return ""
    if isinstance(host, bytes):
        try:
            host = host.decode("idna")
        except UnicodeDecodeError:
            return ""
    return str(host).rstrip(".").lower()


def _log_auto_fallback_once() -> None:
    global _LOGGED_AUTO_FALLBACK

    if _LOGGED_AUTO_FALLBACK:
        return
    logger.warning(
        "Google GenAI IPv6 probe failed while IPv4 succeeded; preferring IPv4 "
        "for googleapis.com hosts. Set %s=system to use resolver order.",
        GOOGLE_GENAI_IP_FAMILY_ENV,
    )
    _LOGGED_AUTO_FALLBACK = True


def build_google_genai_client(timeout_ms: int | None = None) -> Any:
    """Build a Google GenAI client for either direct Gemini API or Vertex AI."""
    from google import genai

    kwargs: dict[str, Any] = {}
    if timeout_ms is not None:
        kwargs["http_options"] = genai.types.HttpOptions(timeout=timeout_ms)

    if google_genai_auth_mode() == "vertexai":
        project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
        location = os.getenv("GOOGLE_CLOUD_LOCATION", "").strip()
        if not project:
            raise ValueError(
                "GOOGLE_CLOUD_PROJECT is required when GOOGLE_GENAI_USE_VERTEXAI is enabled."
            )
        if not location:
            raise ValueError(
                "GOOGLE_CLOUD_LOCATION is required when GOOGLE_GENAI_USE_VERTEXAI is enabled."
            )
        configure_google_genai_network()
        return genai.Client(
            vertexai=True,
            project=project,
            location=location,
            **kwargs,
        )

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ValueError(
            "Set GEMINI_API_KEY for Gemini API mode, or set "
            "GOOGLE_GENAI_USE_VERTEXAI, GOOGLE_CLOUD_PROJECT, and "
            "GOOGLE_CLOUD_LOCATION for Vertex AI mode."
        )
    configure_google_genai_network()
    return genai.Client(api_key=api_key, **kwargs)
