"""Cloud platform integration.

Provides:
  - ``OpenSpaceClient`` — HTTP client for the cloud API
  - ``OpenSpaceAccountClient`` — user auth and agent-key lifecycle client
  - ``cloud_auth_flow`` — high-level auth / agent-key provisioning flow
  - ``PackagePlacementResolver`` — controller-side package placement resolver
  - ``load_cloud_config`` — strict OPENSPACE_CLOUD_* configuration
  - ``SkillSearchEngine`` — hybrid BM25 + embedding search
  - ``generate_embedding`` — OpenAI embedding generation
  - ``CloudTelemetryOutbox`` — redacted telemetry retry queue
  - ``TaskTraceExporter`` — openspace_task_trace_v2 archive builder
  - telemetry payload helpers — schema-aligned telemetry builders
"""

from openspace.cloud.config import (
    CloudConfig,
    load_cloud_config,
    load_cloud_skill_quality_reporting_enabled,
)


def __getattr__(name: str):
    if name == "OpenSpaceClient":
        from openspace.cloud.client import OpenSpaceClient
        return OpenSpaceClient
    if name == "OpenSpaceAccountClient":
        from openspace.cloud.account import OpenSpaceAccountClient
        return OpenSpaceAccountClient
    if name == "cloud_auth_flow":
        from openspace.cloud.auth_flow import cloud_auth_flow
        return cloud_auth_flow
    if name == "PackagePlacementResolver":
        from openspace.cloud.package_placement import PackagePlacementResolver
        return PackagePlacementResolver
    if name == "SkillSearchEngine":
        from openspace.cloud.search import SkillSearchEngine
        return SkillSearchEngine
    if name == "generate_embedding":
        from openspace.cloud.embedding import generate_embedding
        return generate_embedding
    if name == "CloudTelemetryOutbox":
        from openspace.cloud.telemetry_outbox import CloudTelemetryOutbox
        return CloudTelemetryOutbox
    if name == "TaskTraceExporter":
        from openspace.cloud.task_trace_exporter import TaskTraceExporter
        return TaskTraceExporter
    if name == "CloudTaskTraceReporter":
        from openspace.cloud.task_trace_reporter import CloudTaskTraceReporter
        return CloudTaskTraceReporter
    if name == "CloudSkillQualityReporter":
        from openspace.cloud.skill_quality_reporter import CloudSkillQualityReporter
        return CloudSkillQualityReporter
    if name in {
        "build_task_report_payload",
        "build_skill_use_report_payload",
        "build_evolve_report_payload",
        "build_usage_report_payload",
        "short_cloud_request_id",
    }:
        from openspace.cloud import telemetry_payloads

        return getattr(telemetry_payloads, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "OpenSpaceClient",
    "OpenSpaceAccountClient",
    "cloud_auth_flow",
    "PackagePlacementResolver",
    "CloudConfig",
    "load_cloud_config",
    "load_cloud_skill_quality_reporting_enabled",
    "SkillSearchEngine",
    "generate_embedding",
    "CloudTelemetryOutbox",
    "TaskTraceExporter",
    "CloudTaskTraceReporter",
    "CloudSkillQualityReporter",
    "build_task_report_payload",
    "build_skill_use_report_payload",
    "build_evolve_report_payload",
    "build_usage_report_payload",
    "short_cloud_request_id",
]
