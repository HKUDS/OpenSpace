"""SkillRegistry — discover, load, match, and inject skills.

Skills follow the official SKILL.md format:
  - YAML frontmatter with only ``name`` and ``description``
  - Markdown body with instructions (loaded only after selection)

Skills are discovered from user-configured directories and matched to
tasks via LLM-based selection (with keyword fallback).

Skill identity:
  Every skill directory may contain a ``.skill_id`` sidecar file that
  stores the persistent unique identifier.  On **first discovery**
  (no ``.skill_id`` file present), an ID is generated and written to
  the file.  On subsequent runs the ID is **read** from the file —
  this makes the ID portable (survives directory moves, machine changes)
  and deterministic (never regenerated).

  Imported skills: ``{name}__imp_{uuid_hex[:8]}``
  Evolved skills:  ``{name}__v{gen}_{uuid_hex[:8]}``  (written by evolver)
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from openspace.utils.logging import Logger
from .skill_utils import parse_frontmatter, strip_frontmatter, check_skill_safety, is_skill_safe
from .skill_ranker import SkillRanker, SkillCandidate, PREFILTER_THRESHOLD

if TYPE_CHECKING:
    from openspace.llm import LLMClient

logger = Logger.get_logger(__name__)

# Sidecar filename that stores the persistent skill_id
SKILL_ID_FILENAME = ".skill_id"


def _read_or_create_skill_id(name: str, skill_dir: Path) -> str:
    """Read ``skill_id`` from ``.skill_id`` sidecar, or create one.

    The sidecar file is a single-line plain-text file containing only
    the ``skill_id`` string.  It lives alongside ``SKILL.md`` inside
    the skill directory.

    First call (no file): generates ``{name}__imp_{uuid8}`` and writes it.
    Subsequent calls: reads and returns the existing ID.
    """
    id_file = skill_dir / SKILL_ID_FILENAME
    if id_file.exists():
        try:
            existing = id_file.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError:
            pass  # fall through to generate

    # Generate a new ID and persist
    new_id = f"{name}__imp_{uuid.uuid4().hex[:8]}"
    try:
        id_file.write_text(new_id + "\n", encoding="utf-8")
        logger.debug(f"Created .skill_id for '{name}': {new_id}")
    except OSError as e:
        logger.warning(f"Cannot write {id_file}: {e} — ID will not persist across restarts")
    return new_id


def write_skill_id(skill_dir: Path, skill_id: str) -> None:
    """Write (or overwrite) the ``.skill_id`` sidecar in *skill_dir*.

    Called by ``SkillEvolver`` after FIX / DERIVED / CAPTURED to stamp
    the new ``skill_id`` into the skill directory so that the next
    ``discover()`` picks it up correctly.
    """
    id_file = skill_dir / SKILL_ID_FILENAME
    try:
        id_file.write_text(skill_id + "\n", encoding="utf-8")
    except OSError as e:
        logger.warning(f"Cannot write {id_file}: {e}")


@dataclass
class SkillMeta:
    """Metadata for a discovered skill.

    ``skill_id`` is the globally unique identifier used throughout the
    system — LLM prompts, database, evolution, and selection all
    reference this field.
    """

    skill_id: str          # Unique — persisted in .skill_id sidecar
    name: str              # Human-readable name (from frontmatter or dirname)
    description: str
    path: Path             # Absolute path to SKILL.md


class SkillRegistry:
    """Discover, load, select, and inject skills into agent context.

    Args:
        skill_dirs: Ordered list of directories to scan.  Earlier entries have higher
            priority — a skill in the first dir shadows one with the same name
            in later dirs.

    All internal maps are keyed by ``skill_id``, not ``name``.
    """

    def __init__(self, skill_dirs: Optional[List[Path]] = None) -> None:
        self._skill_dirs: List[Path] = skill_dirs or []
        self._skills: Dict[str, SkillMeta] = {}     # skill_id -> SkillMeta
        self._content_cache: Dict[str, str] = {}     # skill_id -> raw SKILL.md content
        self._discovered = False
        self._ranker: Optional[SkillRanker] = None   # lazy-init on first use

    def discover(self) -> List[SkillMeta]:
        """Scan all skill_dirs and populate the registry.

        Each skill is a sub-directory containing a ``SKILL.md`` file.
        The ``skill_id`` is read from the ``.skill_id`` sidecar (created
        automatically on first discovery). Two skills with the same
        ``name`` in different directories get different IDs and can
        coexist in the registry and database.
        """
        self._skills.clear()
        self._content_cache.clear()

        for skill_dir in self._skill_dirs:
            if not skill_dir.exists():
                logger.debug(f"Skill dir does not exist, skipping: {skill_dir}")
                continue

            for entry in sorted(skill_dir.iterdir()):
                if not entry.is_dir():
                    continue
                skill_file = entry / "SKILL.md"
                if not skill_file.exists():
                    continue

                try:
                    content = skill_file.read_text(encoding="utf-8")

                    # Safety check on skill content
                    safety_flags = check_skill_safety(content)
                    if not is_skill_safe(safety_flags):
                        logger.warning(
                            f"BLOCKED skill {entry.name}: "
                            f"safety flags {safety_flags}"
                        )
                        continue

                    meta = self._parse_skill(entry.name, entry, skill_file, content)
                    sid = meta.skill_id

                    if sid in self._skills:
                        logger.debug(f"Skill '{sid}' already discovered, skipping {skill_file}")
                        continue

                    self._skills[sid] = meta
                    self._content_cache[sid] = content
                    if safety_flags:
                        logger.debug(f"Discovered skill: {sid} (safety: {safety_flags})")
                    else:
                        logger.debug(f"Discovered skill: {sid} — {meta.description[:60]}")
                except Exception as e:
                    logger.warning(f"Failed to parse skill {skill_file}: {e}")

        self._discovered = True
        logger.info(
            f"Skill discovery complete: {len(self._skills)} skill(s) "
            f"from {len(self._skill_dirs)} dir(s)"
        )
        return list(self._skills.values())

    def list_skills(self) -> List[SkillMeta]:
        """List all discovered skills."""
        self._ensure_discovered()
        return list(self._skills.values())

    def get_skill(self, skill_id: str) -> Optional[SkillMeta]:
        """Get a skill by ``skill_id``."""
        self._ensure_discovered()
        return self._skills.get(skill_id)

    def get_skill_by_name(self, name: str) -> Optional[SkillMeta]:
        """Get a skill by ``name`` (first match).  Use ``get_skill`` when possible."""
        self._ensure_discovered()
        for meta in self._skills.values():
            if meta.name == name:
                return meta
        return None

    def update_skill(self, old_skill_id: str, new_meta: SkillMeta) -> None:
        """Replace a skill entry after FIX evolution.

        Removes *old_skill_id* from the registry and inserts *new_meta*
        under its (new) ``skill_id``.  Content cache is refreshed from
        the filesystem.
        """
        self._skills.pop(old_skill_id, None)
        self._content_cache.pop(old_skill_id, None)

        self._skills[new_meta.skill_id] = new_meta
        if new_meta.path.exists():
            try:
                self._content_cache[new_meta.skill_id] = (
                    new_meta.path.read_text(encoding="utf-8")
                )
            except Exception:
                pass
        logger.debug(
            f"Registry.update_skill: {old_skill_id} → {new_meta.skill_id}"
        )

    def add_skill(self, meta: SkillMeta) -> None:
        """Register a newly-created skill (DERIVED / CAPTURED).

        Does NOT overwrite an existing entry with the same ``skill_id``.
        """
        if meta.skill_id in self._skills:
            logger.debug(
                f"Registry.add_skill: {meta.skill_id} already exists, skipping"
            )
            return
        self._skills[meta.skill_id] = meta
        if meta.path.exists():
            try:
                self._content_cache[meta.skill_id] = (
                    meta.path.read_text(encoding="utf-8")
                )
            except Exception:
                pass
        logger.debug(f"Registry.add_skill: {meta.skill_id}")

    # Hot-reload API (add external skills at runtime)
    def discover_from_dirs(self, extra_dirs: List[Path]) -> List[SkillMeta]:
        """Discover skills from additional directories and add to the registry.

        Unlike :meth:`discover`, this does **NOT** clear existing skills — it
        only adds new ones from the given directories. Useful for hot-loading
        external skills (e.g. host-agent skills, newly downloaded cloud skills).

        Safety: applies the same ``check_skill_safety`` / ``is_skill_safe``
        filtering as :meth:`discover` to prevent malicious external skills.

        Args:
            extra_dirs: Additional directories to scan.
        """
        added: List[SkillMeta] = []
        for skill_dir in extra_dirs:
            if not skill_dir.exists() or not skill_dir.is_dir():
                logger.debug(f"discover_from_dirs: skipping {skill_dir}")
                continue
            for entry in sorted(skill_dir.iterdir()):
                if not entry.is_dir():
                    continue
                skill_file = entry / "SKILL.md"
                if not skill_file.exists():
                    continue
                try:
                    content = skill_file.read_text(encoding="utf-8")

                    # Safety check (same as discover())
                    safety_flags = check_skill_safety(content)
                    if not is_skill_safe(safety_flags):
                        logger.warning(
                            f"BLOCKED external skill {entry.name}: "
                            f"safety flags {safety_flags}"
                        )
                        continue

                    meta = self._parse_skill(entry.name, entry, skill_file, content)
                    if meta.skill_id in self._skills:
                        continue
                    self._skills[meta.skill_id] = meta
                    self._content_cache[meta.skill_id] = content
                    added.append(meta)
                    logger.debug(f"Hot-registered: {meta.skill_id} — {meta.description[:60]}")
                except Exception as e:
                    logger.warning(f"Failed to parse skill {skill_file}: {e}")

        if added:
            logger.info(
                f"discover_from_dirs: {len(added)} new skill(s) from "
                f"{len(extra_dirs)} dir(s)"
            )
        return added

    def register_skill_dir(self, skill_dir: Path) -> Optional[SkillMeta]:
        """Register a single skill directory (hot-reload).

        Safety: applies ``check_skill_safety`` / ``is_skill_safe`` filtering.

        Args:
            skill_dir: Path to a directory containing ``SKILL.md``.

        Returns:
            :class:`SkillMeta` if newly registered or already present,
            ``None`` if the directory is invalid or the skill fails safety checks.
        """
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            logger.debug(f"register_skill_dir: no SKILL.md in {skill_dir}")
            return None
        try:
            content = skill_file.read_text(encoding="utf-8")

            # Safety check (same as discover())
            safety_flags = check_skill_safety(content)
            if not is_skill_safe(safety_flags):
                logger.warning(
                    f"BLOCKED skill {skill_dir.name}: "
                    f"safety flags {safety_flags}"
                )
                return None

            meta = self._parse_skill(skill_dir.name, skill_dir, skill_file, content)
            if meta.skill_id in self._skills:
                logger.debug(f"register_skill_dir: {meta.skill_id} already exists")
                return self._skills[meta.skill_id]
            self._skills[meta.skill_id] = meta
            self._content_cache[meta.skill_id] = content
            logger.info(f"Hot-registered skill: {meta.skill_id}")
            return meta
        except Exception as e:
            logger.warning(f"Failed to register skill {skill_dir}: {e}")
            return None

    @property
    def ranker(self) -> SkillRanker:
        """Lazy-initialised :class:`SkillRanker` for hybrid pre-filtering."""
        if self._ranker is None:
            self._ranker = SkillRanker()
        return self._ranker

    async def select_skills_with_llm(
        self,
        task_description: str,
        llm_client: "LLMClient",
        max_skills: int = 2,
        model: Optional[str] = None,
        skill_quality: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> tuple[List[SkillMeta], Optional[Dict[str, Any]]]:
        """Use an LLM to select the most relevant skills.

        When the local registry has more than ``PREFILTER_THRESHOLD`` skills,
        a **BM25 → embedding** pre-filter narrows the candidate set before
        sending to the LLM.  This avoids stuffing an overly long catalog
        into the prompt.

        Progressive disclosure: the LLM only sees skill *headers*
        (skill_id + description + quality stats), not the full SKILL.md
        content.  Full content is loaded only after selection.

        Args:
            task_description: The user's task instruction.
            llm_client: An initialised LLMClient used for the selection call.
            max_skills: Maximum number of skills to inject.
            model: Override model for this selection call.
                If None, falls back to ``llm_client``'s default model.
            skill_quality: Optional mapping ``{skill_id: {total_applied, total_completions, total_fallbacks}}``
                from :class:`SkillStore`.  When provided, skills with high
                fallback rates are filtered out and quality signals are
                included in the LLM selection prompt.

        Returns:
            tuple[list[SkillMeta], dict | None]: (selected_skills, selection_record).
                selection_record contains the LLM conversation for logging.
        """
        self._ensure_discovered()
        if not task_description:
            return [], None

        available = list(self._skills.values())
        if not available:
            return [], None

        # Quality-based filtering: remove skills that consistently fail
        filtered_out: List[str] = []
        if skill_quality:
            kept: List[SkillMeta] = []
            for s in available:
                q = skill_quality.get(s.skill_id)
                if q:
                    selections = q.get("total_selections", 0)
                    applied = q.get("total_applied", 0)
                    completions = q.get("total_completions", 0)
                    fallbacks = q.get("total_fallbacks", 0)
                    # Filter 1: selected multiple times but never completed
                    if selections >= 2 and completions == 0:
                        filtered_out.append(s.skill_id)
                        continue
                    # Filter 2: high fallback rate when applied
                    if applied >= 2 and fallbacks / applied > 0.5:
                        filtered_out.append(s.skill_id)
                        continue
                kept.append(s)
            if filtered_out:
                logger.info(
                    f"Skill quality filter: removed {len(filtered_out)} "
                    f"high-fallback skill(s): {filtered_out}"
                )
            available = kept

        if not available:
            return [], None

        # Pre-filter when skill count exceeds threshold
        prefilter_used = False
        if len(available) > PREFILTER_THRESHOLD:
            available = self._prefilter_skills(task_description, available, max_skills)
            prefilter_used = True

        # Build a concise skills catalogue for the LLM (skill_id + description + quality)
        catalog_lines: List[str] = []
        for s in available:
            q = skill_quality.get(s.skill_id) if skill_quality else None
            if q:
                selections = q.get("total_selections", 0)
                applied = q.get("total_applied", 0)
                completions = q.get("total_completions", 0)
                if applied > 0:
                    rate = completions / applied
                    catalog_lines.append(
                        f"- **{s.skill_id}**: {s.description}  "
                        f"(success {completions}/{applied} = {rate:.0%})"
                    )
                elif selections > 0:
                    catalog_lines.append(
                        f"- **{s.skill_id}**: {s.description}  "
                        f"(selected {selections}x, never succeeded)"
                    )
                else:
                    catalog_lines.append(f"- **{s.skill_id}**: {s.description}  (new)")
            else:
                catalog_lines.append(f"- **{s.skill_id}**: {s.description}")
        skills_catalog = "\n".join(catalog_lines)

        prompt = self._build_skill_selection_prompt(
            task_description, skills_catalog, max_skills
        )

        selection_record: Dict[str, Any] = {
            "method": "llm",
            "task": task_description[:500],
            "available_skills": [s.skill_id for s in available],
            "filtered_out": filtered_out,
            "prefilter_used": prefilter_used,
            "prompt": prompt,
        }

        try:
            from gdpval_bench.token_tracker import set_call_source, reset_call_source
            _src_tok = set_call_source("skill_select")
        except ImportError:
            _src_tok = None

        try:
            llm_kwargs = {}
            if model:
                llm_kwargs["model"] = model
            resp = await llm_client.complete(prompt, **llm_kwargs)
            content = resp["message"]["content"].strip()
            selected_ids, brief_plan = self._parse_skill_selection_response(content)

            selection_record["llm_response"] = content
            selection_record["parsed_ids"] = selected_ids
            selection_record["brief_plan"] = brief_plan

            # Validate ids against registry & cap
            result: List[SkillMeta] = []
            for sid in selected_ids:
                if len(result) >= max_skills:
                    break
                meta = self._skills.get(sid)
                if meta:
                    result.append(meta)
                else:
                    logger.debug(f"LLM selected unknown skill_id: {sid}")

            selection_record["selected"] = [s.skill_id for s in result]

            if result:
                ids = ", ".join(s.skill_id for s in result)
                logger.info(f"LLM skill selection: [{ids}]")
            else:
                logger.info("LLM decided no skills are relevant for this task")

            return result, selection_record

        except Exception as e:
            logger.warning(f"LLM skill selection failed: {e} — proceeding without skills")
            selection_record["error"] = str(e)
            selection_record["method"] = "llm_failed"
            selection_record["selected"] = []
            return [], selection_record
        finally:
            if _src_tok is not None:
                reset_call_source(_src_tok)

    def _prefilter_skills(
        self,
        task: str,
        available: List[SkillMeta],
        max_skills: int,
    ) -> List[SkillMeta]:
        """Narrow the candidate set using BM25 + embedding hybrid ranking.

        Keeps at most ``max(15, max_skills * 5)`` candidates for the LLM
        selection prompt.
        """
        prefilter_top_k = max(15, max_skills * 5)

        # Build SkillCandidate list
        candidates: List[SkillCandidate] = []
        for s in available:
            body = ""
            raw = self._content_cache.get(s.skill_id, "")
            if raw:
                body = strip_frontmatter(raw)

            candidates.append(SkillCandidate(
                skill_id=s.skill_id,
                name=s.name,
                description=s.description,
                body=body,
            ))

        ranked = self.ranker.hybrid_rank(task, candidates, top_k=prefilter_top_k)

        # Map back to SkillMeta
        ranked_ids = {c.skill_id for c in ranked}
        result = [s for s in available if s.skill_id in ranked_ids]

        if len(result) < len(available):
            logger.info(
                f"Skill pre-filter: {len(available)} → {len(result)} candidates "
                f"(BM25+embedding, threshold={PREFILTER_THRESHOLD})"
            )
        return result

    def load_skill_content(self, skill_id: str) -> Optional[str]:
        """Return the SKILL.md content (with frontmatter stripped) for *skill_id*."""
        self._ensure_discovered()
        raw = self._content_cache.get(skill_id)
        if raw is None:
            return None
        return self._strip_frontmatter(raw)

    def build_context_injection(
        self,
        skills: List[SkillMeta],
        backends: Optional[List[str]] = None,
    ) -> str:
        """Build a prompt fragment with the full content of *skills*.

        Injected as a system message into the agent's messages before the
        user instruction so the LLM reads skill guidance first.

        Args:
            skills: Skills to inject.
            backends: Active backend names (e.g. ``["shell", "mcp"]``).  Used to
                tailor the guidance so only actually available backends are
                mentioned.  ``None`` falls back to mentioning all backends.

        Key features:
        - Includes the skill directory path so the agent can resolve
          relative references to ``scripts/``, ``references/``, ``assets/``.
        - Replaces ``{baseDir}`` placeholders with the actual skill
          directory path (a convention used in some SKILL.md files).
        """
        parts: List[str] = []
        for skill in skills:
            content = self.load_skill_content(skill.skill_id)
            if content:
                # Resolve {baseDir} placeholder to the skill directory
                skill_dir = str(skill.path.parent)
                content = content.replace("{baseDir}", skill_dir)

                part = (
                    f"### Skill: {skill.skill_id}\n"
                    f"**Skill directory**: `{skill_dir}`\n\n"
                    f"{content}"
                )
                parts.append(part)

        if not parts:
            return ""

        # Build a backend hint that only mentions registered backends
        scope = set(backends) if backends else {"gui", "shell", "mcp", "web", "system"}
        backend_names: List[str] = []
        if "mcp" in scope:
            backend_names.append("MCP")
        if "shell" in scope:
            backend_names.append("shell")
        if "gui" in scope:
            backend_names.append("GUI")
        tool_hint = ", ".join(backend_names) if backend_names else "available"

        # Resource access tips — mention shell_agent only when shell is available
        has_shell = "shell" in scope
        resource_tip = (
            "Use `read_file` / `list_dir` / `write_file` for file operations"
            + (" and `shell_agent` for running scripts" if has_shell else "")
            + ". Paths in skill instructions are relative to the skill "
            "directory listed under each skill heading.\n\n"
        )

        header = (
            "# Active Skills\n\n"
            "The following skills provide **domain knowledge and tested procedures** "
            "relevant to this task.\n\n"
            "**How to use skills:**\n"
            "- If a skill contains **step-by-step procedures or commands**, follow them — "
            "they are verified workflows.\n"
            "- If a skill provides **reference information, best practices, or tool guides**, "
            "use it as context to inform your decisions.\n"
            f"- Skills supplement your available tools — you may use **any** tool "
            f"({tool_hint}) alongside skill guidance. "
            "Choose the best tool for each sub-step.\n\n"
            "**Resource access**: Each skill may include bundled resources "
            "(scripts, references, assets) in its skill directory. "
            + resource_tip
        )
        return header + "\n\n---\n\n".join(parts)

    def _ensure_discovered(self) -> None:
        if not self._discovered:
            self.discover()

    @staticmethod
    def _parse_skill(
        dir_name: str,
        skill_dir: Path,
        skill_file: Path,
        content: str,
    ) -> SkillMeta:
        """Parse a SKILL.md file into a SkillMeta.

        Only ``name`` and ``description`` are read from frontmatter
        (per the official skill format).  ``skill_id`` is read from
        the ``.skill_id`` sidecar (created if absent).
        """
        frontmatter = parse_frontmatter(content)
        name = frontmatter.get("name", dir_name)
        description = frontmatter.get("description", name)
        skill_id = _read_or_create_skill_id(name, skill_dir)

        return SkillMeta(
            skill_id=skill_id,
            name=name,
            description=description,
            path=skill_file,
        )

    # Frontmatter parsing is delegated to skill_utils (single source of truth).
    _extract_frontmatter = staticmethod(parse_frontmatter)
    _strip_frontmatter = staticmethod(strip_frontmatter)

    @staticmethod
    def _build_skill_selection_prompt(
        task: str,
        skills_catalog: str,
        max_skills: int,
    ) -> str:
        """Build the prompt for LLM skill selection.

        Uses a plan-then-select pattern: the LLM first writes a brief
        execution plan, then selects skills that match the plan.
        """
        return f"""You are a skill selector for an autonomous agent.

# Task

{task}

# Available Skills

{skills_catalog}

# Instructions

Follow these steps:

**Step 1 — Plan**: Think about how you would accomplish this task. What are the key deliverables? What file formats are needed (PDF, DOCX, XLSX, etc.)? What tools or libraries would you use?

**Step 2 — Match**: Check which skills directly teach workflows for the deliverables or file formats identified in your plan. A skill is relevant ONLY if it provides a tested procedure for a core part of your plan. Skills that only share vague topical overlap (e.g. a "PDF checklist" skill for a task that just happens to involve PDFs) add noise and should be excluded.

**Step 3 — Quality check**: Among matching skills, prefer ones with higher success rates. Avoid skills marked as "never succeeded" or with very low success rates — they waste iterations and actively hurt performance.

**Step 4 — Decide**: Select at most {max_skills} skill(s). If no skill closely matches your plan, you MUST return an empty list. Selecting an irrelevant or low-quality skill is **worse than selecting none** — it forces the agent down an unproductive path and wastes the entire iteration budget. When in doubt, leave it out.

Return a JSON object:
{{"brief_plan": "1-2 sentence plan for this task", "skills": ["skill_id_1", "skill_id_2"]}}

If no skill applies:
{{"brief_plan": "1-2 sentence plan", "skills": []}}

IMPORTANT: Use the **exact skill_id** from the list above."""

    @staticmethod
    def _parse_skill_selection_response(content: str) -> tuple[List[str], str]:
        """Parse the LLM response and extract selected skill IDs + plan.

        Returns:
            (skill_ids, brief_plan)
        """
        # Handle markdown code blocks
        code_block = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
        if code_block:
            content = code_block.group(1).strip()
        else:
            # Try to find a raw JSON object
            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            if json_match:
                content = json_match.group()

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse LLM skill selection JSON: {content[:200]}")
            return [], ""

        brief_plan = data.get("brief_plan", "")
        if brief_plan:
            logger.info(f"Skill selection plan: {brief_plan}")

        ids = data.get("skills", [])
        if not isinstance(ids, list):
            return [], brief_plan
        return [str(n).strip() for n in ids if n], brief_plan
