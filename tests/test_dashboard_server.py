import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _install_dashboard_stubs():
    flask_mod = types.ModuleType("flask")

    class Flask:
        def __init__(self, *args, **kwargs):
            pass

        def route(self, *args, **kwargs):
            def decorator(fn):
                return fn

            return decorator

    flask_mod.Flask = Flask
    flask_mod.abort = lambda *args, **kwargs: None
    flask_mod.jsonify = lambda payload=None, **kwargs: payload if payload is not None else kwargs
    flask_mod.send_from_directory = lambda *args, **kwargs: None
    flask_mod.url_for = lambda *args, **kwargs: "/artifact"

    action_recorder_mod = types.ModuleType("openspace.recording.action_recorder")
    action_recorder_mod.analyze_agent_actions = lambda actions: {}
    action_recorder_mod.load_agent_actions = lambda path: []

    recording_utils_mod = types.ModuleType("openspace.recording.utils")
    recording_utils_mod.load_recording_session = lambda path: {
        "metadata": {},
        "trajectory": [],
        "plans": [],
        "decisions": [],
        "statistics": {},
    }

    skill_engine_mod = types.ModuleType("openspace.skill_engine")
    skill_engine_mod.SkillStore = type("SkillStore", (), {})

    skill_types_mod = types.ModuleType("openspace.skill_engine.types")
    skill_types_mod.SkillRecord = type("SkillRecord", (), {})

    stubs = {
        "flask": flask_mod,
        "openspace.recording.action_recorder": action_recorder_mod,
        "openspace.recording.utils": recording_utils_mod,
        "openspace.skill_engine": skill_engine_mod,
        "openspace.skill_engine.types": skill_types_mod,
    }
    originals = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    return originals


class DashboardServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._originals = _install_dashboard_stubs()
        sys.modules.pop("openspace.dashboard_server", None)
        cls.dashboard = importlib.import_module("openspace.dashboard_server")

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("openspace.dashboard_server", None)
        for name, module in cls._originals.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def test_discover_workflow_dirs_keeps_duplicate_leaf_names_from_different_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root_a = base / "recordings"
            root_b = base / "results"
            dir_a = root_a / "shared-name"
            dir_b = root_b / "shared-name"
            dir_a.mkdir(parents=True)
            dir_b.mkdir(parents=True)
            (dir_a / "metadata.json").write_text("{}", encoding="utf-8")
            (dir_b / "metadata.json").write_text("{}", encoding="utf-8")

            original_roots = self.dashboard.WORKFLOW_ROOTS
            self.dashboard.WORKFLOW_ROOTS = [root_a, root_b]
            try:
                workflows = self.dashboard._discover_workflow_dirs()
                workflow_ids = {self.dashboard._workflow_id(path) for path in workflows}
            finally:
                self.dashboard.WORKFLOW_ROOTS = original_roots

        self.assertEqual(len(workflows), 2)
        self.assertEqual(len(workflow_ids), 2)


if __name__ == "__main__":
    unittest.main()
