"""Short read addresses and explicit final-draft citation binding.

Source/trace presentation stays literal. Final evidence citations resolve
through the session catalogue before strict delivery validation.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping

PROPOSER_PRESENTATION_CONTRACT = "harness-context-v2"
LOCAL_REFERENCE_HANDLE = re.compile(
    r"(?<![A-Za-z0-9_])@[TEFPR][0-9]+(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


class ProposerReferences:
    # These values are source, observed data or model-authored intent. Do not
    # rewrite text that happens to look like a tool address inside them.
    _literal_fields = frozenset(
        {
            "content",
            "code",
            "path",
            "file",
            "relative_path",
            "signature",
            "summary",
            "observed_behavior",
            "failure_mechanism",
            "implementation_direction",
            "input",
            "parent_prediction",
            "child_prediction",
            "fixed_family",
            "role",
            "symbol",
            "expression",
            "stdout",
            "stderr",
            "statement",
            "title",
            "decision_excerpt",
            "output_excerpt",
            "environment_constraints",
            "policy_guidance",
            "authoritative_environment_constraints",
            "untrusted_policy_guidance",
        }
    )

    def __init__(self, namespace: str):
        self.namespace = namespace
        self._internal = re.compile(
            rf"(?<![A-Za-z0-9_])([TEFPR]){re.escape(namespace)}_(\d{{5}})(?![A-Za-z0-9_])"
        )

    def present(self, value):
        if isinstance(value, str):
            match = self._internal.fullmatch(value)
            return f"@{match[1]}{int(match[2])}" if match is not None else value
        if isinstance(value, Mapping):
            return {
                self.present(key): item
                if key in self._literal_fields
                else self.present(item)
                for key, item in value.items()
            }
        if isinstance(value, (tuple, list)):
            return [self.present(item) for item in value]
        return value

    def resolve(self, ref):
        if not isinstance(ref, str):
            return ref
        match = re.fullmatch(r"@([TEFPR])([1-9][0-9]{0,4})", ref)
        if match is None:
            return ref
        return f"{match[1]}{self.namespace}_{int(match[2]):05d}"

    def diagnostic_text(self, text: str) -> str:
        """Shorten owned references in generated errors, never in source/data."""
        return self._internal.sub(lambda match: f"@{match[1]}{int(match[2])}", text)

    def worker_text(self, text: str, addresses: Mapping[str, str]) -> str:
        """Resolve explicitly marked research references only at worker export.

        The caller supplies only issued/seen addresses. Raw research, code and
        provider receipts remain untouched; unresolved handles stay labelled.
        """
        return LOCAL_REFERENCE_HANDLE.sub(
            lambda match: addresses.get(
                self.resolve(match[0]), f"[unresolved local reference {match[0]}]"
            ),
            text,
        )

    def arguments(self, args):
        """Resolve only address fields; preserve proposal and calculation text."""
        if not isinstance(args, Mapping):
            return args
        result = dict(args)
        for field in ("ref", "root_ref"):
            if field in result:
                result[field] = self.resolve(result[field])
        for field in ("scope_refs", "evidence_refs"):
            if isinstance(result.get(field), list):
                result[field] = [self.resolve(ref) for ref in result[field]]
        if isinstance(result.get("new_paths"), list):
            result["new_paths"] = [self.arguments(item) for item in result["new_paths"]]
        if "direction" in result:
            result["direction"] = self.arguments(result["direction"])
        return result

    def inline_evidence(self, args, evidence_catalog):
        """Resolve final-draft citations only; keep observed source and raw drafts intact."""
        from agentpilot.app.model_harnesses import _LOCAL_ALIAS_TOKEN

        result = copy.deepcopy(args)
        aliases = {}
        for item in evidence_catalog:
            aliases[item.ref.upper()] = item
            aliases[self.present(item.ref).upper()] = item
        used, normalizations, issues = [], [], []

        def resolve(value, field):
            if not isinstance(value, str):
                return value
            bindings = {}

            def replace_reference(match):
                token = match[0]
                if not token.lstrip("@").upper().startswith("E"):
                    return token  # Scope addresses are not evidence citations.
                item = aliases.get(token.upper())
                if item is None:
                    issues.append(
                        {
                            "field": field,
                            "code": "unknown_evidence_ref",
                            "message": f"Unknown or unread evidence citation: {token}",
                        }
                    )
                    return token
                bindings[token] = item.canonical_ref
                if item.ref not in used:
                    used.append(item.ref)
                return item.canonical_ref

            text = _LOCAL_ALIAS_TOKEN.sub(replace_reference, value)
            if bindings:
                normalizations.append(
                    {
                        "field": field,
                        "operation": "resolve_inline_evidence",
                        "bindings": bindings,
                    }
                )
            return text

        for field in (
            "observed_behavior",
            "failure_mechanism",
            "implementation_direction",
        ):
            if field in result:
                result[field] = resolve(result[field], field)
        if isinstance(result.get("new_paths"), list):
            for index, item in enumerate(result["new_paths"]):
                if isinstance(item, dict) and "role" in item:
                    item["role"] = resolve(item["role"], f"new_paths[{index}].role")
        if isinstance(result.get("mechanism_test"), dict):
            for field, value in result["mechanism_test"].items():
                result["mechanism_test"][field] = resolve(
                    value, f"mechanism_test.{field}"
                )
        evidence = result.setdefault("evidence_refs", [])
        if isinstance(evidence, list):
            evidence.extend(ref for ref in used if ref not in evidence)
        return result, normalizations, issues
