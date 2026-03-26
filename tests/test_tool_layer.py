import asyncio
import importlib
import sys
import types
import unittest


def _install_tool_layer_stubs():
    agents_mod = types.ModuleType("openspace.agents")
    agents_mod.GroundingAgent = type("GroundingAgent", (), {})

    llm_mod = types.ModuleType("openspace.llm")

    class FakeLLMClient:
        instances = 0

        def __init__(self, *args, **kwargs):
            type(self).instances += 1
            self.model = kwargs.get("model")

    llm_mod.LLMClient = FakeLLMClient

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
    return originals, FakeLLMClient


class ToolLayerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls._originals, cls.fake_llm_class = _install_tool_layer_stubs()
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

    async def test_execute_passes_resolved_iterations_without_skills(self):
        config = self.tool_layer.OpenSpaceConfig()
        config.grounding_max_iterations = 7

        openspace = self.tool_layer.OpenSpace(config)
        openspace._initialized = True
        openspace._task_done = asyncio.Event()
        openspace._task_done.set()
        openspace._grounding_client = types.SimpleNamespace(_registry={})
        openspace._recording_manager = None
        openspace._skill_registry = None
        openspace._execution_analyzer = None
        openspace._skill_evolver = None

        recorded_contexts = []

        class FakeAgent:
            async def process(self, context):
                recorded_contexts.append(dict(context))
                return {"status": "success", "iterations": 1, "tool_executions": []}

        openspace._grounding_agent = FakeAgent()

        async def no_op(*args, **kwargs):
            return None

        openspace._maybe_analyze_execution = no_op
        openspace._maybe_evolve_quality = no_op

        result = await openspace.execute("do work", max_iterations=3)

        self.assertEqual(result["status"], "success")
        self.assertEqual(recorded_contexts[0]["max_iterations"], 7)

    def test_get_skill_selection_llm_reuses_dedicated_client(self):
        self.fake_llm_class.instances = 0
        config = self.tool_layer.OpenSpaceConfig()
        config.skill_registry_model = "selector-model"
        openspace = self.tool_layer.OpenSpace(config)

        first = openspace._get_skill_selection_llm()
        second = openspace._get_skill_selection_llm()

        self.assertIs(first, second)
        self.assertEqual(self.fake_llm_class.instances, 1)


if __name__ == "__main__":
    unittest.main()
