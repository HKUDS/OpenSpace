import asyncio
import sys
import types
from datetime import datetime
from types import SimpleNamespace

aiohttp_stub = types.ModuleType("aiohttp")
aiohttp_stub.ClientSession = type("ClientSession", (), {})
aiohttp_stub.ClientResponse = type("ClientResponse", (), {})
aiohttp_stub.ClientResponseError = type("ClientResponseError", (Exception,), {})
aiohttp_stub.ClientTimeout = type(
    "ClientTimeout",
    (),
    {"__init__": lambda self, *args, **kwargs: None},
)
sys.modules.setdefault("aiohttp", aiohttp_stub)

yarl_stub = types.ModuleType("yarl")


class URL(str):
    def __truediv__(self, other):
        return URL(self.rstrip("/") + "/" + str(other).lstrip("/"))


yarl_stub.URL = URL
sys.modules.setdefault("yarl", yarl_stub)

import openspace.skill_engine.analyzer as analyzer_module
from openspace.skill_engine.analyzer import ExecutionAnalyzer
from openspace.skill_engine.evidence.types import (
    EvidencePacket,
    EvidenceScope,
    PacketBudget,
    ResourceRef,
)
from openspace.skill_engine.types import ExecutionAnalysis, SkillJudgment


class FakeStore:
    def __init__(self, *, fail_record=False):
        self.fail_record = fail_record
        self.events = []

    def load_analyses_for_task(self, task_id):
        return None

    async def record_analysis(self, analysis, observed_tool_keys=None):
        self.events.append("record")
        if self.fail_record:
            raise RuntimeError("record failed")

    def close(self):
        pass


class FakeReporter:
    def __init__(self, events, *, fail=False):
        self.events = events
        self.fail = fail

    async def maybe_report_analysis(self, analysis, *, session_id=None):
        self.events.append(("report", analysis.task_id, session_id))
        if self.fail:
            raise RuntimeError("reporter failed")
        return {"status": "reported"}


def _run(coro):
    return asyncio.run(coro)


def _analysis(task_id="task-1"):
    return ExecutionAnalysis(
        task_id=task_id,
        timestamp=datetime(2026, 1, 2, 3, 4, 5),
        task_completed=True,
        skill_judgments=[SkillJudgment("local-skill-1", True, "ignored")],
    )


async def _raw_json(*args, **kwargs):
    return {"ok": True}


def _execution_analyzer(store, analysis):
    analyzer = ExecutionAnalyzer(store=store, llm_client=object(), enabled=True)
    analyzer._load_recording_context = lambda rec_path, execution_result: {
        "selected_skills": ["local-skill-1"],
        "skill_contents": {"local-skill-1": "content"},
        "used_tool_keys": {"shell:bash"},
        "traj_records": [{"tool": "bash"}],
        "execution_status": "failed",
        "iterations": 1,
        "session_id": "context-session",
    }
    analyzer._build_analysis_prompt = lambda context: "prompt"
    analyzer._run_analysis_loop = _raw_json
    analyzer._parse_analysis = lambda task_id, raw_json, context: analysis
    return analyzer


def test_analyze_execution_reports_after_record_analysis(monkeypatch, tmp_path):
    store = FakeStore()
    analysis = _analysis()
    events = store.events
    monkeypatch.setattr(
        analyzer_module,
        "_make_skill_quality_reporter",
        lambda: FakeReporter(events),
    )
    analyzer = _execution_analyzer(store, analysis)
    result = _run(
        analyzer.analyze_execution(
            "task-1",
            str(tmp_path),
            {"status": "failed", "session_id": "execution-session"},
        )
    )
    assert result is analysis
    assert events == ["record", ("report", "task-1", "execution-session")]


def test_analyze_execution_does_not_report_when_parse_returns_none(
    monkeypatch,
    tmp_path,
):
    store = FakeStore()
    events = store.events
    monkeypatch.setattr(
        analyzer_module,
        "_make_skill_quality_reporter",
        lambda: FakeReporter(events),
    )
    analyzer = _execution_analyzer(store, _analysis())
    analyzer._parse_analysis = lambda task_id, raw_json, context: None
    result = _run(
        analyzer.analyze_execution(
            "task-1",
            str(tmp_path),
            {"status": "failed", "session_id": "execution-session"},
        )
    )
    assert result is None
    assert events == []


def test_analyze_execution_does_not_report_when_record_analysis_raises(
    monkeypatch,
    tmp_path,
):
    store = FakeStore(fail_record=True)
    events = store.events
    monkeypatch.setattr(
        analyzer_module,
        "_make_skill_quality_reporter",
        lambda: FakeReporter(events),
    )
    analyzer = _execution_analyzer(store, _analysis())
    result = _run(
        analyzer.analyze_execution(
            "task-1",
            str(tmp_path),
            {"status": "failed", "session_id": "execution-session"},
        )
    )
    assert result is None
    assert events == ["record"]


def test_reporter_exception_is_non_fatal_after_persistence(monkeypatch, tmp_path):
    store = FakeStore()
    analysis = _analysis()
    events = store.events
    monkeypatch.setattr(
        analyzer_module,
        "_make_skill_quality_reporter",
        lambda: FakeReporter(events, fail=True),
    )
    analyzer = _execution_analyzer(store, analysis)
    result = _run(
        analyzer.analyze_execution(
            "task-1",
            str(tmp_path),
            {"status": "failed", "session_id": "execution-session"},
        )
    )
    assert result is analysis
    assert events == ["record", ("report", "task-1", "execution-session")]


def _packet(task_id="packet-task", *, session_id=None, selected_refs=None):
    return SimpleNamespace(
        packet_type="analysis",
        packet_id="packet-1",
        scope=SimpleNamespace(task_id=task_id, session_id=session_id),
        selected_refs=selected_refs or {},
    )


def test_load_packet_context_uses_packet_session_id_without_execution_result():
    packet = EvidencePacket(
        packet_id="packet-ctx",
        trigger_job_id="trigger-1",
        packet_type="analysis",
        profile_name="quality_signal",
        subprofile="default",
        manifest_watermark=1,
        scope=EvidenceScope(task_id="packet-task", session_id="scope-session"),
        selected_refs={
            "runtime_snapshot": [
                ResourceRef(
                    "runtime-1",
                    "runtime_snapshot",
                    metadata={
                        "status": "failed",
                        "iterations": 2,
                        "active_skills": ["local-skill-1"],
                        "instruction_preview": "inspect packet context",
                    },
                )
            ],
            "tool_result": [
                ResourceRef(
                    "tool-1",
                    "tool_result",
                    metadata={
                        "tool_key": "shell:bash",
                        "status": "failed",
                        "result_preview": "boom",
                    },
                )
            ],
        },
        expanded_snippets=[],
        readable_paths=[],
        instructions={},
        budget=PacketBudget(max_chars=1000, used_chars=0),
        redaction_status="ok",
        build_status="ok",
        missing_ref_types=[],
    )
    analyzer = ExecutionAnalyzer(store=FakeStore(), llm_client=object(), enabled=True)

    context = analyzer._load_packet_context(packet, task_id="packet-task")

    assert context["session_id"] == "scope-session"
    assert context["task_id"] == "packet-task"
    assert context["packet"] is packet
    assert context["used_tool_keys"] == {"shell:bash"}


def _packet_analyzer(store, analysis, packet):
    analyzer = ExecutionAnalyzer(store=store, llm_client=object(), enabled=True)
    analyzer._load_packet_context = lambda packet, task_id: {
        "selected_skills": ["local-skill-1"],
        "skill_contents": {"local-skill-1": "content"},
        "used_tool_keys": {"shell:bash"},
        "packet_tool_records": [{"tool": "bash"}],
        "traj_records": [{"tool": "bash"}],
        "execution_status": "failed",
        "iterations": 1,
        "packet": packet,
    }
    analyzer._build_packet_analysis_prompt = lambda packet, context: "prompt"
    analyzer._run_analysis_loop = _raw_json
    analyzer._parse_analysis = lambda task_id, raw_json, context: analysis
    return analyzer


def test_analyze_packet_reports_after_record_analysis(monkeypatch):
    store = FakeStore()
    packet = _packet(session_id="scope-session")
    analysis = _analysis("packet-task")
    events = store.events
    monkeypatch.setattr(
        analyzer_module,
        "_make_skill_quality_reporter",
        lambda: FakeReporter(events),
    )
    analyzer = _packet_analyzer(store, analysis, packet)
    result = _run(analyzer.analyze_packet(packet))
    assert result is analysis
    assert events == ["record", ("report", "packet-task", "scope-session")]


def test_analyze_packet_reports_selected_ref_metadata_session_id(monkeypatch):
    store = FakeStore()
    packet = _packet(
        selected_refs={
            "tool_result": [
                ResourceRef(
                    "tool-1",
                    "tool_result",
                    metadata={"session_id": "metadata-session"},
                )
            ]
        }
    )
    analysis = _analysis("packet-task")
    events = store.events
    monkeypatch.setattr(
        analyzer_module,
        "_make_skill_quality_reporter",
        lambda: FakeReporter(events),
    )
    analyzer = _packet_analyzer(store, analysis, packet)

    result = _run(analyzer.analyze_packet(packet))

    assert result is analysis
    assert events == ["record", ("report", "packet-task", "metadata-session")]


def test_analyze_packet_reports_with_missing_session_id(monkeypatch):
    store = FakeStore()
    packet = _packet()
    analysis = _analysis("packet-task")
    events = store.events
    monkeypatch.setattr(
        analyzer_module,
        "_make_skill_quality_reporter",
        lambda: FakeReporter(events),
    )
    analyzer = _packet_analyzer(store, analysis, packet)

    result = _run(analyzer.analyze_packet(packet))

    assert result is analysis
    assert events == ["record", ("report", "packet-task", None)]


def test_session_id_extraction_order_and_missing_values():
    assert (
        analyzer_module._analysis_session_id(
            {"session_id": "context-session"},
            execution_result={"session_id": "execution-session"},
        )
        == "execution-session"
    )
    assert (
        analyzer_module._analysis_session_id(
            {"session_id": "context-session"},
            execution_result={},
        )
        == "context-session"
    )

    packet = SimpleNamespace(
        scope=EvidenceScope(session_id="scope-session"),
        selected_refs={},
    )
    assert analyzer_module._packet_session_id(packet) == "scope-session"

    packet = SimpleNamespace(
        scope=EvidenceScope(),
        selected_refs={
            "tool_result": [
                ResourceRef("ref-1", "tool_result", session_id="ref-session")
            ]
        },
    )
    assert analyzer_module._packet_session_id(packet) == "ref-session"

    packet = SimpleNamespace(
        scope=EvidenceScope(),
        selected_refs={
            "tool_result": [
                ResourceRef(
                    "ref-1",
                    "tool_result",
                    metadata={"session_id": "metadata-session"},
                )
            ]
        },
    )
    assert analyzer_module._packet_session_id(packet) == "metadata-session"

    packet = SimpleNamespace(scope=EvidenceScope(), selected_refs={})
    assert analyzer_module._packet_session_id(packet) is None
