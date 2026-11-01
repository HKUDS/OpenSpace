"""Bundled reading and experiment guides, kept separate from core instructions."""

from functools import lru_cache
from importlib.resources import files


def _skill_body(name: str) -> str:
    resource = files("agentpilot").joinpath(
        f"policy/proposer/skills/{name}/SKILL.md"
    )
    text = resource.read_text(encoding="utf-8")
    return text.split("---", 2)[2].strip()


@lru_cache(maxsize=1)
def proposer_guide() -> str:
    return _skill_body("proposer-guide")


_EXPERIMENT_CAPABILITIES = {
    "evaluate": frozenset({"build", "admit"}),
    "experiment": frozenset({"experiment"}),
    "build": frozenset({"build"}),
    # These session operations remain useful for inspecting retained work.
    "read_experiment": frozenset(),
    "note": frozenset(),
    "drop": frozenset(),
    "close": frozenset(),
}


def experiment_guide(supported_operations) -> str:
    """Include only experiment choices supported by the configured backend."""
    available = frozenset(supported_operations)
    lines = []
    for line in _skill_body("experiment-guide").splitlines():
        if line.startswith("- `"):
            labels = line.split("`")
            operation = labels[1]
            required = _EXPERIMENT_CAPABILITIES[operation]
            if required and not required.intersection(available):
                continue
        lines.append(line)
    return "\n".join(lines).strip()


# Existing callers use this name; both load the same single guide.
harness_reading_skill = proposer_guide
