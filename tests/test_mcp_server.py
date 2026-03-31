import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _install_mcp_stubs():
    fastmcp_mod = types.ModuleType("mcp.server.fastmcp")

    class FastMCP:
        def __init__(self, *args, **kwargs):
            pass

        def tool(self):
            def decorator(fn):
                return fn

            return decorator

        def run(self, *args, **kwargs):
            return None

    fastmcp_mod.FastMCP = FastMCP

    stubs = {
        "mcp.server.fastmcp": fastmcp_mod,
    }
    originals = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    return originals


class MCPServerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls._originals = _install_mcp_stubs()
        sys.modules.pop("openspace.mcp_server", None)
        cls.mcp_server = importlib.import_module("openspace.mcp_server")

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("openspace.mcp_server", None)
        for name, module in cls._originals.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    async def test_auto_register_does_not_cache_missing_directories(self):
        self.mcp_server._registered_skill_dirs.clear()

        class Registry:
            def __init__(self):
                self.calls = []

            def discover_from_dirs(self, dirs):
                self.calls.append([str(d.resolve()) for d in dirs])
                return dirs

        registry = Registry()
        fake_store = types.SimpleNamespace()

        async def sync_from_registry(added):
            return len(added)

        fake_store.sync_from_registry = sync_from_registry
        fake_openspace = types.SimpleNamespace(_skill_registry=registry)

        async def fake_get_openspace():
            return fake_openspace

        def fake_get_store():
            return fake_store

        self.mcp_server._get_openspace = fake_get_openspace
        self.mcp_server._get_store = fake_get_store

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            valid_dir = base / "valid"
            valid_dir.mkdir()
            missing_dir = base / "missing"

            await self.mcp_server._auto_register_skill_dirs([str(missing_dir), str(valid_dir)])
            self.assertEqual(registry.calls[0], [str(valid_dir.resolve())])
            self.assertNotIn(str(missing_dir.resolve()), self.mcp_server._registered_skill_dirs)

            missing_dir.mkdir()
            await self.mcp_server._auto_register_skill_dirs([str(missing_dir)])

            self.assertEqual(registry.calls[1], [str(missing_dir.resolve())])


if __name__ == "__main__":
    unittest.main()
