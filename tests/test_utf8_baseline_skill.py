import importlib
import sys
import types
import unittest


def _install_tool_layer_stubs():
    agents_mod = types.ModuleType("openspace.agents")
    agents_mod.GroundingAgent = type("GroundingAgent", (), {})

    llm_mod = types.ModuleType("openspace.llm")
    llm_mod.LLMClient = type("LLMClient", (), {"__init__": lambda self, *a, **k: None})

    grounding_client_mod = types.ModuleType("openspace.grounding.core.grounding_client")
    grounding_client_mod.GroundingClient = type("GroundingClient", (), {})

    config_mod = types.ModuleType("openspace.config")
    config_mod.get_config = lambda: None
    config_mod.load_config = lambda *args, **kwargs: None

    config_loader_mod = types.ModuleType("openspace.config.loader")
    config_loader_mod.get_agent_config = lambda *args, **kwargs: None

    recording_mod = types.ModuleType("openspace.recording")
    recording_mod.RecordingManager = type("RecordingManager", (), {})

    skill_engine_mod = types.ModuleType("openspace.skill_engine")
    skill_engine_mod.SkillRegistry = type("SkillRegistry", (), {})
    skill_engine_mod.ExecutionAnalyzer = type("ExecutionAnalyzer", (), {})
    skill_engine_mod.SkillStore = type("SkillStore", (), {})

    evolver_mod = types.ModuleType("openspace.skill_engine.evolver")
    evolver_mod.SkillEvolver = type("SkillEvolver", (), {})

    logging_mod = types.ModuleType("openspace.utils.logging")

    class Logger:
        @staticmethod
        def get_logger(name):
            return types.SimpleNamespace(
                info=lambda *a, **k: None,
                debug=lambda *a, **k: None,
                warning=lambda *a, **k: None,
                error=lambda *a, **k: None,
            )

    logging_mod.Logger = Logger

    stubs = {
        "openspace.agents": agents_mod,
        "openspace.llm": llm_mod,
        "openspace.grounding.core.grounding_client": grounding_client_mod,
        "openspace.config": config_mod,
        "openspace.config.loader": config_loader_mod,
        "openspace.recording": recording_mod,
        "openspace.skill_engine": skill_engine_mod,
        "openspace.skill_engine.evolver": evolver_mod,
        "openspace.utils.logging": logging_mod,
    }

    originals = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    return originals


class UTF8BaselineSkillTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls._originals = _install_tool_layer_stubs()
        sys.modules.pop("openspace.tool_layer", None)
        cls.tool_layer = importlib.import_module("openspace.tool_layer")

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("openspace.tool_layer", None)
        for name, module in cls._originals.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    async def test_select_and_inject_skills_always_includes_utf8_baseline(self):
        config = self.tool_layer.OpenSpaceConfig()
        openspace = self.tool_layer.OpenSpace(config)

        forced_meta = types.SimpleNamespace(
            skill_id="default-utf8-encoding__imp_abc12345",
            name="default-utf8-encoding",
            description="baseline",
        )

        class FakeRegistry:
            async def select_skills_with_llm(self, task, llm_client, max_skills, skill_quality):
                return [], {"method": "llm", "selected": []}

            def get_skill_by_name(self, name):
                if name == "default-utf8-encoding":
                    return forced_meta
                return None

            def build_context_injection(self, skills, backends=None):
                return "|".join(s.skill_id for s in skills)

        injected = {}

        class FakeAgent:
            backend_scope = ["shell"]

            def set_skill_context(self, context_text, skill_ids):
                injected["context_text"] = context_text
                injected["skill_ids"] = list(skill_ids)

            def clear_skill_context(self):
                injected["cleared"] = True

        openspace._skill_registry = FakeRegistry()
        openspace._grounding_agent = FakeAgent()
        openspace._recording_manager = None
        openspace._skill_store = None
        openspace._get_skill_selection_llm = lambda: object()

        selected = await openspace._select_and_inject_skills("any task")

        self.assertTrue(selected)
        self.assertIn(forced_meta.skill_id, injected["skill_ids"])


if __name__ == "__main__":
    unittest.main()
