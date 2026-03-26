import importlib
import importlib.util
import unittest
from unittest.mock import patch


MODULE_NAME = "openspace.grounding.backends.mcp.transport.connectors"


class MCPConnectorsOptionalDepsTests(unittest.TestCase):
    def test_websocket_connector_available_when_dependency_installed(self):
        connectors = importlib.import_module(MODULE_NAME)
        self.assertEqual(
            connectors.WebSocketConnector.__module__,
            f"{MODULE_NAME}.websocket",
        )

    def test_websocket_connector_fallback_when_dependency_missing(self):
        connectors = importlib.import_module(MODULE_NAME)
        original_find_spec = importlib.util.find_spec

        def fake_find_spec(name: str, *args, **kwargs):
            if name == "websockets":
                return None
            return original_find_spec(name, *args, **kwargs)

        with patch("importlib.util.find_spec", side_effect=fake_find_spec):
            connectors = importlib.reload(connectors)
            with self.assertRaisesRegex(ImportError, "pip install websockets"):
                connectors.WebSocketConnector(url="ws://localhost:1234")

        importlib.reload(connectors)


if __name__ == "__main__":
    unittest.main()
