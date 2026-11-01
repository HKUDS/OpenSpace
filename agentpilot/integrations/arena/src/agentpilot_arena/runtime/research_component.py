"""Measured calls to a real immutable program template, using its solver route.

This adapter does not infer how to invoke an arbitrary Python function. A
program with dynamic prompt assembly can supply its own renderer/adapter.
"""

from __future__ import annotations

import hashlib
import math
import string
from pathlib import PurePosixPath
from typing import Callable, Mapping

from agentpilot.app.provider_calls import ProviderCallContext
from agentpilot.domain import canonical_json, stable_id
from agentpilot.providers import CompletionRequest
from agentpilot.providers.base import ProviderCallRefused, ProviderCompletionUnusable


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_template(template: str, variables: Mapping[str, object]) -> str:
    """Substitute named scalar fields, without attribute/index traversal."""
    for _, field, specification, conversion in string.Formatter().parse(template):
        if field is None:
            continue
        if not field.isidentifier() or specification or conversion:
            raise ValueError("template fields must be simple variable names; use a program renderer for other syntax")
        if field not in variables:
            raise ValueError(f"template variable is missing: {field}")
    if any(not isinstance(key, str) or type(value) not in {str, int, float, bool, type(None)}
           for key, value in variables.items()):
        raise ValueError("template variables must be named scalar values")
    return template.format_map(dict(variables))


class ProgramTemplateCallAdapter:
    """Freeze and call a supplied program's template through DurableCompletion.

    ``template_reader(artifact, path)`` reads the exact artifact commit; its
    repository lookup must never substitute the mutable working tree. Allowed
    roots are checked here before the reader sees a path. The configured model
    and provider accounting are shared with the parent solver, not chosen by
    the proposer. DurableCompletion owns call dispatch and receipt replay.
    """

    def __init__(
        self, completion, artifact_lookup: Callable, template_reader: Callable,
        *, model: str, allowed_roots: tuple[str, ...], reservation_usd: float,
        default_artifact: str = "P", max_output_tokens: int = 2048,
        temperature: float = .3, disable_reasoning: bool = False,
        reasoning_effort: str | None = None, renderer: Callable = render_template,
    ):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("a frozen parent solver model is required")
        if (isinstance(reservation_usd, bool) or not math.isfinite(reservation_usd)
                or reservation_usd <= 0):
            raise ValueError("component call reservation must be positive and finite")
        if not allowed_roots:
            raise ValueError("component templates require configured visible roots")
        self.completion, self.lookup, self.read_template = completion, artifact_lookup, template_reader
        self.model, self.allowed_roots = model, tuple(allowed_roots)
        self.reservation_usd, self.default_artifact = float(reservation_usd), default_artifact
        self.max_output_tokens, self.temperature = max_output_tokens, temperature
        self.disable_reasoning, self.reasoning_effort = disable_reasoning, reasoning_effort
        self.renderer = renderer

    def _path(self, value):
        if not isinstance(value, str) or not value or "\\" in value:
            raise ValueError("template must be a repository-relative path")
        path = PurePosixPath(value)
        if path.is_absolute() or path.as_posix() != value or ".." in path.parts:
            raise ValueError("template must be a normalized repository-relative path")
        if not any(root == "." or value == root.rstrip("/") or value.startswith(root.rstrip("/") + "/")
                   for root in self.allowed_roots):
            raise ValueError("template is outside visible program roots")
        return value

    def prepare(self, arguments):
        if set(arguments) - {"template", "variables", "input", "base_artifact", "artifact_id"}:
            raise ValueError("call accepts template, variables, input and an optional base artifact")
        path = self._path(arguments.get("template"))
        if "base_artifact" in arguments and "artifact_id" in arguments and arguments["base_artifact"] != arguments["artifact_id"]:
            raise ValueError("component call names conflicting artifacts")
        artifact = self.lookup(arguments.get("base_artifact", arguments.get("artifact_id", self.default_artifact)))
        variables = arguments.get("variables", {})
        supplied_input = arguments.get("input", "")
        if not isinstance(variables, Mapping) or len(canonical_json(variables).encode()) > 16384:
            raise ValueError("component variables must fit in 16 KiB")
        if not isinstance(supplied_input, str) or len(supplied_input.encode()) > 16384:
            raise ValueError("component input must fit in 16 KiB")
        template = self.read_template(artifact, path)
        if not isinstance(template, str) or not template.strip() or len(template.encode()) > 65536:
            raise ValueError("component template must be nonempty and fit in 64 KiB")
        rendered = self.renderer(template, dict(variables))
        if not isinstance(rendered, str):
            raise ValueError("program renderer must return text")
        prompt = rendered + ("\n\n" + supplied_input if supplied_input else "")
        if not prompt.strip() or len(prompt.encode()) > 98304:
            raise ValueError("rendered component input must fit in 96 KiB")
        return {"reservation_usd": self.reservation_usd, "arguments": {
            "artifact_id": artifact.id, "commit_sha": artifact.commit_sha,
            "template": path, "template_sha256": _sha(template),
            "variables": dict(variables), "input": supplied_input,
            "prompt": prompt, "prompt_sha256": _sha(prompt),
            "model": self.model, "max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature, "disable_reasoning": self.disable_reasoning,
            "reasoning_effort": self.reasoning_effort,
        }}

    def execute(self, ticket):
        args = ticket.arguments
        artifact = self.lookup(args["artifact_id"])
        if artifact.commit_sha != args["commit_sha"]:
            raise ValueError("component artifact identity changed")
        if _sha(args["prompt"]) != args["prompt_sha256"]:
            raise ValueError("frozen component prompt changed")
        call_id = stable_id("research-component-call", ticket.id, ticket.idempotency_key,
                            args["artifact_id"], args["commit_sha"], args["prompt_sha256"])
        context = ProviderCallContext(call_id=call_id, run_id="research-" + ticket.track_id,
                                      batch_id="research-" + ticket.id, purpose="research-component-call")
        request = CompletionRequest(
            model=args["model"], prompt=args["prompt"], system="",
            max_output_tokens=args["max_output_tokens"], temperature=args["temperature"],
            disable_reasoning=args["disable_reasoning"], reasoning_effort=args["reasoning_effort"],
            metadata={"purpose": "research-component-call", "ticket_id": ticket.id,
                      "artifact_id": args["artifact_id"], "template": args["template"],
                      "template_sha256": args["template_sha256"], "prompt_sha256": args["prompt_sha256"]},
        )
        try:
            result = self.completion.complete(request, context=context)
        except ProviderCallRefused:
            return {"status": "failed", "error": "ProviderCallRefused", "cost_usd": 0.0,
                    "call_id": call_id, "origin": "program_component_call", "artifact_id": artifact.id}
        except ProviderCompletionUnusable as exc:
            return {"status": "failed", "error": type(exc).__name__,
                    "cost_usd": None if exc.usage is None else exc.usage.cost_usd,
                    "call_id": call_id, "response": exc.settled_text,
                    "origin": "program_component_call", "artifact_id": artifact.id}
        return {"cost_usd": result.usage.cost_usd, "call_id": call_id,
                "provider_request_id": result.usage.provider_request_id,
                "input_tokens": result.usage.input_tokens, "output_tokens": result.usage.output_tokens,
                "model": result.model, "response": result.text,
                "artifact_id": artifact.id, "template": args["template"],
                "template_sha256": args["template_sha256"],
                "origin": "program_component_call",
                "note": "Observed response to this component input; full-program performance is untested."}


__all__ = ["ProgramTemplateCallAdapter", "render_template"]
