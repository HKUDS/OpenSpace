"""Local skill classification for cloud import and local evolution."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field, replace
import hashlib
from pathlib import Path
from typing import Any, Iterable, Mapping

from openspace.cloud.local_mapping import CloudLocalMappingStore, utc_now_iso

_CATEGORIES = {"workflow", "tool_guide", "reference"}
_WORKFLOW_TERMS = {
    "workflow",
    "playbook",
    "end-to-end",
    "procedure",
    "process",
    "步骤",
    "流程",
    "执行",
    "任务",
}
_TOOL_TERMS = {
    "tool",
    "cli",
    "api",
    "command",
    "shell",
    "browser",
    "mcp",
    "use ",
    "使用工具",
    "命令",
}
_REFERENCE_TERMS = {
    "reference",
    "taxonomy",
    "glossary",
    "spec",
    "documentation",
    "background",
    "知识",
    "参考",
    "说明",
    "规范",
}


@dataclass(frozen=True)
class SkillClassificationResult:
    local_skill_id: str
    category: str
    local_category_path: str
    confidence: float
    rationale: str
    review_state: str = "auto"
    evidence: dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "local_skill_id": self.local_skill_id,
            "category": self.category,
            "local_category_path": self.local_category_path,
            "classification_confidence": self.confidence,
            "classification_rationale": self.rationale,
            "review_state": self.review_state,
            "evidence": dict(self.evidence),
            "updated_at": self.updated_at,
        }


def classify_skill_dir(
    skill_dir: str | Path,
    *,
    local_skill_id: str,
    cloud_package_path: str | None = None,
    local_category: str | None = None,
    local_category_path: str | None = None,
    origin: str = "imported",
) -> SkillClassificationResult:
    """Classify a concrete skill directory using frontmatter, body, and cloud path."""

    root = Path(skill_dir)
    skill_file = root / "SKILL.md"
    content = ""
    if skill_file.exists():
        try:
            content = skill_file.read_text(encoding="utf-8")
        except OSError:
            content = ""
    frontmatter = _parse_frontmatter(content)
    body = _strip_frontmatter(content)
    return classify_skill_metadata(
        local_skill_id=local_skill_id,
        name=str(frontmatter.get("name") or root.name),
        description=str(frontmatter.get("description") or ""),
        body=body,
        allowed_tools=_as_text_list(frontmatter.get("allowed-tools")),
        local_path=str(root),
        cloud_package_path=cloud_package_path,
        local_category=local_category,
        local_category_path=local_category_path,
        origin=origin,
        frontmatter=frontmatter,
    )


def classify_skill_metadata(
    *,
    local_skill_id: str,
    name: str,
    description: str = "",
    body: str = "",
    allowed_tools: Iterable[Any] | None = None,
    local_path: str = "",
    cloud_package_path: str | None = None,
    local_category: str | None = None,
    local_category_path: str | None = None,
    origin: str = "imported",
    frontmatter: Mapping[str, Any] | None = None,
) -> SkillClassificationResult:
    """Classify local skill type and local package-taxonomy path.

    ``category`` remains the coarse skill type (workflow/tool guide/reference).
    ``local_category_path`` is the local package taxonomy path.  It deliberately
    uses the same shape as cloud package paths, but is stored independently so
    the local tree can drift from the cloud tree over time.
    """

    fm = dict(frontmatter or {})
    agent_path = normalize_local_category_path(
        local_category_path,
        category=local_category,
    )
    agent_category = _normalize_category(local_category)
    explicit = _explicit_category(fm)
    evidence_text = "\n".join(
        [
            name,
            description,
            body[:8000],
            " ".join(str(item) for item in (allowed_tools or [])),
            cloud_package_path or "",
            local_path,
        ]
    ).lower()

    scores = {
        "workflow": _score_terms(evidence_text, _WORKFLOW_TERMS),
        "tool_guide": _score_terms(evidence_text, _TOOL_TERMS),
        "reference": _score_terms(evidence_text, _REFERENCE_TERMS),
    }
    if allowed_tools:
        scores["tool_guide"] += 1.0
    if origin in {"fix", "derive", "captured", "capture"}:
        scores["workflow"] += 0.25

    review_state = "auto"
    rationale_bits: list[str] = []
    if agent_path:
        category = agent_category or explicit or _best_category_from_scores(scores)[0]
        local_category_path = agent_path
        confidence = 0.96
        review_state = "reviewed"
        rationale_bits.append("agent-provided local package taxonomy path")
    elif agent_category:
        category = agent_category
        confidence = 0.92
        rationale_bits.append("agent-provided local category")
    elif explicit:
        category = explicit
        confidence = 0.95
        rationale_bits.append("explicit frontmatter category")
    else:
        category, confidence, review_state, category_rationale = _best_category_from_scores(scores)
        rationale_bits.extend(category_rationale)

    if cloud_package_path:
        rationale_bits.append("cloud package path recorded as provenance evidence")
    if not agent_path:
        local_category_path = normalize_local_category_path(cloud_package_path)
        if local_category_path:
            confidence = max(confidence, 0.86)
            rationale_bits.append("seeded local taxonomy path from cloud package path")
        else:
            local_category_path = _local_category_path(
                category,
                name=name,
                local_path=local_path,
            )
            review_state = "needs_review"
            confidence = min(confidence, 0.68)
            rationale_bits.append("fallback local taxonomy path requires review")
            if origin in {"derive", "derived", "capture", "captured"}:
                rationale_bits.append("generated skill missing agent-selected local path")
    return SkillClassificationResult(
        local_skill_id=local_skill_id,
        category=category,
        local_category_path=local_category_path,
        confidence=round(confidence, 3),
        rationale="; ".join(rationale_bits),
        review_state=review_state,
        evidence={
            "origin": origin,
            "cloud_package_path": cloud_package_path or "",
            "local_path": local_path,
            "scores": scores,
            "frontmatter_category": explicit or "",
            "agent_local_category": agent_category or "",
            "agent_local_category_path": agent_path,
        },
        updated_at=utc_now_iso(),
    )


def persist_skill_classification(
    store: CloudLocalMappingStore,
    classification: SkillClassificationResult,
) -> SkillClassificationResult:
    saved = store.upsert_skill_local_classification(
        local_skill_id=classification.local_skill_id,
        category=classification.category,
        local_category_path=classification.local_category_path,
        classification_confidence=classification.confidence,
        classification_rationale=classification.rationale,
        review_state=classification.review_state,
        evidence=classification.evidence,
        updated_at=classification.updated_at or utc_now_iso(),
    )
    return SkillClassificationResult(
        local_skill_id=saved.local_skill_id,
        category=saved.category,
        local_category_path=saved.local_category_path,
        confidence=float(saved.classification_confidence or 0.0),
        rationale=saved.classification_rationale,
        review_state=saved.review_state,
        evidence=dict(saved.evidence or {}),
        updated_at=saved.updated_at,
    )


def build_local_category_path(
    category: str,
    *,
    local_category_path: str | None = None,
    cloud_package_path: str | None = None,
    local_path: str = "",
    name: str = "",
) -> str:
    normalized = str(category or "").strip().lower().replace("-", "_")
    if normalized not in _CATEGORIES:
        normalized = "workflow"
    explicit = normalize_local_category_path(local_category_path)
    if explicit:
        return explicit
    seeded = normalize_local_category_path(cloud_package_path)
    if seeded:
        return seeded
    return _local_category_path(
        normalized,
        name=name,
        local_path=local_path,
    )


def initialize_local_skill_taxonomy(
    *,
    mapping_store: CloudLocalMappingStore,
    skills: Iterable[Any] | None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Ensure discovered local skills have logical local taxonomy rows.

    This is the bootstrap step for an independent local taxonomy.  Cloud-bound
    skills are seeded from their cloud package path; already nested local skills
    are seeded from their filesystem taxonomy; the remaining skills are placed
    into a local review namespace so they participate in retrieval and browsing
    instead of staying outside the tree.
    """

    created: list[dict[str, Any]] = []
    skipped = 0
    for skill in skills or []:
        local_skill_id = str(getattr(skill, "skill_id", "") or "")
        if not local_skill_id:
            skipped += 1
            continue
        if not overwrite and mapping_store.get_skill_local_classification(local_skill_id):
            skipped += 1
            continue

        skill_path = Path(str(getattr(skill, "path", "") or ""))
        if not skill_path.name:
            skipped += 1
            continue
        skill_dir = skill_path.parent
        binding = mapping_store.get_skill_cloud_binding_by_local(local_skill_id)
        cloud_path = ""
        if binding is not None:
            cloud_path = (
                binding.current_package_path
                or binding.package_path_at_pull
                or ""
            )
        filesystem_path = _infer_local_category_path_from_skill_path(skill_path)
        local_category_path = None if cloud_path else (filesystem_path or None)
        category = _skill_category_value(skill) or None
        classification = classify_skill_dir(
            skill_dir,
            local_skill_id=local_skill_id,
            cloud_package_path=cloud_path or None,
            local_category=category,
            local_category_path=local_category_path,
            origin="bootstrap",
        )
        evidence = dict(classification.evidence or {})
        evidence.update({
            "bootstrap": True,
            "bootstrap_source": (
                "cloud_package_path"
                if cloud_path
                else "filesystem_taxonomy"
                if filesystem_path
                else "local_review_fallback"
            ),
        })
        if not local_category_path and not cloud_path:
            classification = replace(
                classification,
                review_state="needs_review",
                confidence=min(classification.confidence, 0.62),
                rationale=(
                    f"{classification.rationale}; initialized into local review namespace"
                    if classification.rationale
                    else "initialized into local review namespace"
                ),
                evidence=evidence,
            )
        else:
            classification = replace(classification, evidence=evidence)
        saved = persist_skill_classification(mapping_store, classification)
        created.append(saved.to_payload())
    return {
        "created_count": len(created),
        "skipped_count": skipped,
        "created": created,
    }


def build_local_taxonomy_snapshot(
    *,
    mapping_store: CloudLocalMappingStore | None = None,
    skills: Iterable[Any] | None = None,
    category: str | None = None,
    path_prefix: str | None = None,
    query: str | None = None,
    max_paths: int = 80,
    max_examples_per_path: int = 4,
    include_sample_paths: bool = False,
) -> dict[str, Any]:
    """Return a bounded local package-taxonomy tree view for agent/LLM placement."""

    skills_by_id: dict[str, Any] = {}
    for skill in skills or []:
        skill_id = str(getattr(skill, "skill_id", "") or "")
        if skill_id:
            skills_by_id[skill_id] = skill

    paths: dict[str, dict[str, Any]] = {}
    seen_skill_ids: set[str] = set()
    classifications = []
    if mapping_store is not None:
        try:
            classifications = mapping_store.list_skill_local_classifications()
        except Exception:
            classifications = []

    for classification in classifications:
        path = normalize_local_category_path(classification.local_category_path)
        if not path:
            path = _local_category_path(
                classification.category,
                name=classification.local_skill_id,
                local_path="",
            )
        entry = _taxonomy_entry(
            paths,
            path,
            source="classification",
            category=classification.category,
        )
        entry["skill_count"] += 1
        entry["skill_categories"][classification.category] = (
            entry["skill_categories"].get(classification.category, 0) + 1
        )
        entry["review_states"][classification.review_state] = (
            entry["review_states"].get(classification.review_state, 0) + 1
        )
        skill = skills_by_id.get(classification.local_skill_id)
        if skill is not None and len(entry["examples"]) < max_examples_per_path:
            entry["examples"].append(_taxonomy_skill_example(skill))
        elif len(entry["examples"]) < max_examples_per_path:
            entry["examples"].append({
                "local_skill_id": classification.local_skill_id,
                "name": "",
                "description": "",
                "path": "",
            })
        seen_skill_ids.add(classification.local_skill_id)

    unclassified: list[dict[str, Any]] = []
    for skill_id, skill in skills_by_id.items():
        if skill_id in seen_skill_ids:
            continue
        inferred_path = _infer_local_category_path_from_skill_path(
            getattr(skill, "path", None)
        )
        if inferred_path:
            skill_category = _skill_category_value(skill) or "workflow"
            entry = _taxonomy_entry(
                paths,
                inferred_path,
                source="filesystem",
                category=skill_category,
            )
            entry["skill_count"] += 1
            entry["skill_categories"][skill_category] = (
                entry["skill_categories"].get(skill_category, 0) + 1
            )
            entry["review_states"]["unclassified"] = (
                entry["review_states"].get("unclassified", 0) + 1
            )
            if len(entry["examples"]) < max_examples_per_path:
                entry["examples"].append(_taxonomy_skill_example(skill))
            continue
        unclassified.append(_taxonomy_skill_example(skill))

    if mapping_store is not None:
        try:
            package_entries = mapping_store.list_package_path_index()
        except Exception:
            package_entries = []
        for package in package_entries:
            package_path = normalize_local_category_path(package.package_path)
            if not package_path:
                continue
            entry = _taxonomy_entry(paths, package_path, source="cloud_seed")
            entry["cloud_seed"] = {
                "package_id": package.package_id,
                "package_kind": package.package_kind,
                "snapshot_version": package.snapshot_version,
                "can_select_as_upload_target": package.can_select_as_upload_target,
                "can_create_child_regular_package": package.can_create_child_regular_package,
            }

    all_path_rows = sorted(
        paths.values(),
        key=lambda item: str(item["local_category_path"]),
    )

    category_counts = {category: 0 for category in sorted(_CATEGORIES)}
    for row in all_path_rows:
        for skill_category, count in (row.get("skill_categories") or {}).items():
            if skill_category in category_counts:
                category_counts[skill_category] += int(count)

    normalized_category = _normalize_category(category)
    normalized_prefix = normalize_local_category_path(path_prefix)
    q = str(query or "").strip().lower()
    filtered_path_rows = [
        row for row in all_path_rows
        if not normalized_category
        or int(row.get("skill_count") or 0) == 0
        or normalized_category in (row.get("skill_categories") or {})
        or row.get("category") == normalized_category
    ]

    payload: dict[str, Any] = {
        "status": "success",
        "tree_kind": "local_package_taxonomy",
        "path_format": "domain/sub-domain/package[/local-finer-package]",
        "categories": [
            {"category": category, "skill_count": category_counts.get(category, 0)}
            for category in sorted(_CATEGORIES)
        ],
        "total_path_count": len(all_path_rows),
        "unclassified_skills": unclassified[: max(max_examples_per_path, 1)],
        "unclassified_count": len(unclassified),
        "agent_instruction": (
            "Browse by path prefix or query, then choose an existing local package "
            "taxonomy path when appropriate, or create one nearby/finer child path. "
            "The local tree uses the same taxonomy style as cloud package paths but "
            "is stored independently and may diverge from the current cloud tree."
        ),
    }

    if q:
        matches = [
            row for row in filtered_path_rows
            if _taxonomy_row_matches_query(row, q)
        ]
        payload.update({
            "view": "search_results",
            "query": query,
            "paths": matches[:max_paths],
            "match_count": len(matches),
            "paths_truncated": len(matches) > max_paths,
        })
        return payload

    if normalized_prefix:
        current, children = _local_taxonomy_children(
            filtered_path_rows,
            normalized_prefix,
            max_examples_per_path=max_examples_per_path,
        )
        child_rows = sorted(
            children.values(),
            key=lambda item: str(item["local_category_path"]),
        )
        payload.update({
            "view": "children",
            "current_path": normalized_prefix,
            "current_path_entry": current,
            "children": child_rows[:max_paths],
            "child_count": len(child_rows),
            "children_truncated": len(child_rows) > max_paths,
        })
        return payload

    _, root_children = _local_taxonomy_children(
        filtered_path_rows,
        "",
        max_examples_per_path=max_examples_per_path,
    )
    root_rows = sorted(
        root_children.values(),
        key=lambda item: str(item["local_category_path"]),
    )
    payload.update({
        "view": "roots",
        "roots": root_rows[:max_paths],
        "root_count": len(root_rows),
        "roots_truncated": len(root_rows) > max_paths,
    })
    if include_sample_paths:
        sample_rows = filtered_path_rows[:max_paths]
        payload["sample_paths"] = sample_rows
        payload["sample_paths_truncated"] = len(filtered_path_rows) > max_paths
    return payload


def normalize_local_category_path(
    value: str | None,
    *,
    category: str | None = None,
) -> str:
    """Normalize an agent/user-provided local package taxonomy path.

    The value uses cloud-package style path segments, but it is a local path:
    changing it does not change the cloud placement.  The optional ``category``
    argument is accepted for older callers and is no longer used as a forced
    path prefix.
    """

    raw_value = str(value or "").strip()
    if not raw_value:
        return ""
    raw_parts = [
        _slug(part)
        for part in raw_value.replace("\\", "/").split("/")
        if _slug(part)
    ]
    _ = category
    return "/".join(raw_parts)


def materialize_skill_category_tree(
    skill_dir: str | Path,
    classification: SkillClassificationResult | Mapping[str, Any],
    *,
    skills_root: str | Path,
) -> Path:
    """Move a skill directory under ``<skills_root>/<local_category_path>/``.

    The returned directory always contains the original skill directory name as
    the final segment.  If a target directory already exists for the same local
    skill id, the source is removed when possible and the existing target is
    reused.  Otherwise a deterministic local-id suffix avoids collisions.
    """

    source = Path(skill_dir).expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"skill directory not found: {source}")
    raw_path = (
        classification.local_category_path
        if isinstance(classification, SkillClassificationResult)
        else str(classification.get("local_category_path") or "")
    )
    local_skill_id = (
        classification.local_skill_id
        if isinstance(classification, SkillClassificationResult)
        else str(classification.get("local_skill_id") or "")
    )
    category_parts = _category_path_parts(raw_path)
    if not category_parts:
        return source

    root = _infer_category_tree_root(
        source,
        category_parts=category_parts,
        fallback_root=Path(skills_root).expanduser().resolve(),
    )
    target_parent = root.joinpath(*category_parts)
    target = target_parent / source.name
    if source == target:
        return source

    if target.exists():
        if _skill_id_at(target) == local_skill_id:
            if source != target and _skill_id_at(source) == local_skill_id:
                shutil.rmtree(source)
            return target.resolve()
        suffix = hashlib.sha256((local_skill_id or source.name).encode("utf-8")).hexdigest()[:8]
        target = target_parent / f"{source.name}__local_{suffix}"
        if target.exists() and _skill_id_at(target) == local_skill_id:
            if source != target and _skill_id_at(source) == local_skill_id:
                shutil.rmtree(source)
            return target.resolve()
    target_parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    _prune_empty_category_dirs(source.parent, stop_at=root)
    return target.resolve()


def _explicit_category(frontmatter: Mapping[str, Any]) -> str | None:
    for key in ("category", "skill_category", "skill-type", "skill_type"):
        value = str(frontmatter.get(key) or "").strip().lower().replace("-", "_")
        if value in _CATEGORIES:
            return value
    return None


def _score_terms(text: str, terms: set[str]) -> float:
    score = 0.0
    for term in terms:
        if term in text:
            score += 1.0
    return score


def _best_category_from_scores(
    scores: Mapping[str, float],
) -> tuple[str, float, str, list[str]]:
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    category, top_score = ordered[0]
    second_score = ordered[1][1] if len(ordered) > 1 else 0.0
    review_state = "auto"
    rationale_bits: list[str] = []
    if top_score <= 0:
        category = "workflow"
        confidence = 0.55
        rationale_bits.append("defaulted to workflow from weak evidence")
    else:
        confidence = min(0.9, 0.55 + top_score * 0.12)
        rationale_bits.append(f"matched {category} evidence")
    if top_score > 0 and top_score - second_score < 0.75:
        review_state = "needs_review"
        confidence = min(confidence, 0.68)
        rationale_bits.append("close category scores")
    return category, confidence, review_state, rationale_bits


def _local_category_path(
    category: str,
    *,
    name: str,
    local_path: str,
) -> str:
    skill_segment = _slug(name) or _slug(Path(local_path).name)
    return "/".join(["local", category, skill_segment]) if skill_segment else f"local/{category}"


def _normalize_category(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower().replace("-", "_")
    return normalized if normalized in _CATEGORIES else None


def _category_from_path(local_category_path: str) -> str | None:
    first = str(local_category_path or "").split("/", 1)[0]
    return _normalize_category(first)


def _category_path_parts(local_category_path: str) -> list[str]:
    parts: list[str] = []
    for raw in str(local_category_path or "").replace("\\", "/").split("/"):
        part = _slug(raw)
        if part and part not in {".", ".."}:
            parts.append(part)
    return parts


def _infer_category_tree_root(
    skill_dir: Path,
    *,
    category_parts: list[str],
    fallback_root: Path,
) -> Path:
    parent_parts = list(skill_dir.parent.parts)
    count = len(category_parts)
    if count and len(parent_parts) >= count:
        lowered_parent = [part.lower() for part in parent_parts]
        lowered_category = [part.lower() for part in category_parts]
        if lowered_parent[-count:] == lowered_category:
            root_parts = parent_parts[:-count]
            if root_parts:
                return Path(*root_parts).resolve()
    return fallback_root


def _taxonomy_entry(
    paths: dict[str, dict[str, Any]],
    local_category_path: str,
    *,
    source: str,
    category: str | None = None,
) -> dict[str, Any]:
    canonical = normalize_local_category_path(local_category_path)
    skill_category = _normalize_category(category)
    entry = paths.get(canonical)
    if entry is None:
        entry = {
            "local_category_path": canonical,
            "category": skill_category or "",
            "skill_categories": {},
            "skill_count": 0,
            "examples": [],
            "review_states": {},
            "sources": [],
        }
        paths[canonical] = entry
    elif skill_category and not entry.get("category"):
        entry["category"] = skill_category
    elif skill_category and entry.get("category") not in {"", skill_category}:
        entry["category"] = "mixed"
    if source not in entry["sources"]:
        entry["sources"].append(source)
    return entry


def _taxonomy_skill_example(skill: Any) -> dict[str, Any]:
    return {
        "local_skill_id": str(getattr(skill, "skill_id", "") or ""),
        "name": str(getattr(skill, "name", "") or ""),
        "description": str(getattr(skill, "description", "") or "")[:240],
        "path": str(getattr(skill, "path", "") or ""),
    }


def _taxonomy_row_matches_query(row: dict[str, Any], query: str) -> bool:
    haystack = [
        str(row.get("local_category_path") or ""),
        str(row.get("category") or ""),
    ]
    for example in row.get("examples") or []:
        if not isinstance(example, dict):
            continue
        haystack.extend([
            str(example.get("name") or ""),
            str(example.get("description") or ""),
            str(example.get("local_skill_id") or ""),
        ])
    text = "\n".join(haystack).lower()
    return all(token in text for token in query.split() if token)


def _local_taxonomy_children(
    rows: list[dict[str, Any]],
    prefix: str,
    *,
    max_examples_per_path: int,
) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]]]:
    prefix_parts = _category_path_parts(prefix)
    current: dict[str, Any] | None = None
    children: dict[str, dict[str, Any]] = {}
    for row in rows:
        row_path = str(row.get("local_category_path") or "")
        row_parts = _category_path_parts(row_path)
        if row_parts == prefix_parts:
            current = row
            continue
        if len(row_parts) <= len(prefix_parts):
            continue
        if row_parts[: len(prefix_parts)] != prefix_parts:
            continue
        child_path = "/".join(row_parts[: len(prefix_parts) + 1])
        child = _taxonomy_entry(children, child_path, source="child")
        child["skill_count"] += int(row.get("skill_count") or 0)
        for skill_category, count in (row.get("skill_categories") or {}).items():
            child["skill_categories"][skill_category] = (
                child["skill_categories"].get(skill_category, 0) + int(count)
            )
            if not child.get("category"):
                child["category"] = skill_category
            elif child.get("category") != skill_category:
                child["category"] = "mixed"
        for state, count in (row.get("review_states") or {}).items():
            child["review_states"][state] = child["review_states"].get(state, 0) + int(count)
        for example in row.get("examples") or []:
            if len(child["examples"]) >= max_examples_per_path:
                break
            if isinstance(example, dict):
                child["examples"].append(dict(example))
    return current, children


def _infer_local_category_path_from_skill_path(path: Any) -> str:
    try:
        skill_file = Path(path)
    except TypeError:
        return ""
    container_parts = list(skill_file.parent.parts[:-1])
    normalized_parts = [_slug(part) for part in container_parts]
    for index in range(len(normalized_parts) - 1, -1, -1):
        if normalized_parts[index] not in {"skills", "host-skills"}:
            continue
        tail = [_slug(raw) for raw in container_parts[index + 1 :] if _slug(raw)]
        if tail:
            return "/".join(tail)
    for index, part in enumerate(normalized_parts):
        if part.replace("-", "_") not in _CATEGORIES:
            continue
        tail = [_slug(raw) for raw in container_parts[index + 1 :] if _slug(raw)]
        return "/".join([part, *tail]) if tail else part
    return ""


def _skill_category_value(skill: Any) -> str:
    raw = getattr(skill, "category", "")
    value = getattr(raw, "value", raw)
    return _normalize_category(str(value or "")) or ""


def _skill_id_at(skill_dir: Path) -> str:
    try:
        return (skill_dir / ".skill_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _prune_empty_category_dirs(start: Path, *, stop_at: Path) -> None:
    try:
        current = start.resolve()
        stop = stop_at.resolve()
    except OSError:
        return
    while current != stop:
        try:
            current.relative_to(stop)
        except ValueError:
            return
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _slug(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", str(value).strip().lower())
    return re.sub(r"-+", "-", text).strip("-")


def _parse_frontmatter(content: str) -> dict[str, Any]:
    if not content:
        return {}
    try:
        from openspace.skill_engine.skill_utils import parse_frontmatter

        return parse_frontmatter(content)
    except Exception:
        return {}


def _strip_frontmatter(content: str) -> str:
    if not content.startswith("---"):
        return content
    end = content.find("\n---", 3)
    if end < 0:
        return content
    body_start = content.find("\n", end + 4)
    return content[body_start + 1 :] if body_start >= 0 else ""


def _as_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]
