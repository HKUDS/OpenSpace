import os


_AZURE_V1_PATH = "/openai/v1"


def azure_openai_api_key() -> str:
    return _required_env("AZURE_OPENAI_API_KEY")


def azure_api_version() -> str:
    return _required_env("AZURE_API_VERSION")


def azure_resource_endpoint() -> str:
    endpoint = _required_env("AZURE_API_ENDPOINT").rstrip("/")
    if endpoint.endswith(_AZURE_V1_PATH):
        return endpoint.removesuffix(_AZURE_V1_PATH)
    return endpoint


def azure_v1_base_url() -> str:
    return azure_resource_endpoint() + _AZURE_V1_PATH + "/"


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required for Azure OpenAI models.")
    return value
