"""
    RecordingManager
      ├── internal management of platform.RecordingClient
      ├── internal management of platform.ScreenshotClient
      ├── internal management of TrajectoryRecorder
      └── internal management of ActionRecorder
"""

from importlib import import_module

__all__ = [
    'RecordingManager',
    'TrajectoryRecorder',
    'ActionRecorder',
    'load_trajectory_from_jsonl',
    'load_metadata',
    'format_trajectory_for_export',
    'analyze_trajectory',
    'load_recording_session',
    'filter_trajectory',
    'extract_errors',
    'generate_summary_report',
    'load_agent_actions',
    'analyze_agent_actions',
    'format_agent_actions',
]

_EXPORTS = {
    'RecordingManager': ('.manager', 'RecordingManager'),
    'TrajectoryRecorder': ('.recorder', 'TrajectoryRecorder'),
    'ActionRecorder': ('.action_recorder', 'ActionRecorder'),
    'load_trajectory_from_jsonl': ('.utils', 'load_trajectory_from_jsonl'),
    'load_metadata': ('.utils', 'load_metadata'),
    'format_trajectory_for_export': ('.utils', 'format_trajectory_for_export'),
    'analyze_trajectory': ('.utils', 'analyze_trajectory'),
    'load_recording_session': ('.utils', 'load_recording_session'),
    'filter_trajectory': ('.utils', 'filter_trajectory'),
    'extract_errors': ('.utils', 'extract_errors'),
    'generate_summary_report': ('.utils', 'generate_summary_report'),
    'load_agent_actions': ('.action_recorder', 'load_agent_actions'),
    'analyze_agent_actions': ('.action_recorder', 'analyze_agent_actions'),
    'format_agent_actions': ('.action_recorder', 'format_agent_actions'),
}


def __getattr__(name: str):
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}') from exc
    module = import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
