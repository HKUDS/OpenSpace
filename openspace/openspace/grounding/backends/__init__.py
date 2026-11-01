# Use lazy imports to avoid loading all backends unconditionally

def _lazy_import_provider(provider_name: str):
    """Lazy import provider class"""
    if provider_name == 'mcp':
        from .mcp.provider import MCPProvider
        return MCPProvider
    elif provider_name == 'shell':
        from .shell.provider import ShellProvider
        return ShellProvider
    elif provider_name == 'web':
        from .web.provider import WebProvider
        return WebProvider
    elif provider_name == 'gui':
        from .gui.provider import GUIProvider
        return GUIProvider
    else:
        raise ImportError(f"Unknown provider: {provider_name}")


class _ProviderRegistry:
    """Lazy provider registry"""
    def __getitem__(self, key):
        return _lazy_import_provider(key)
    
    def __contains__(self, key):
        return key in ['mcp', 'shell', 'web', 'gui']

BACKEND_PROVIDERS = _ProviderRegistry()

__all__ = [
    'BACKEND_PROVIDERS',
    '_lazy_import_provider'
]