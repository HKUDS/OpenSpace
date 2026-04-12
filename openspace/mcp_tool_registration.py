from __future__ import annotations

from typing import Protocol

from mcp.server.fastmcp import FastMCP


class MainMCPToolImplementation(Protocol):
    async def execute_task(
        self,
        task: str,
        workspace_dir: str | None = None,
        max_iterations: int | None = None,
        skill_dirs: list[str] | None = None,
        search_scope: str = "all",
    ) -> str: ...

    async def search_skills(
        self,
        query: str,
        source: str = "all",
        limit: int = 20,
        auto_import: bool = True,
    ) -> str: ...

    async def fix_skill(
        self,
        skill_dir: str,
        direction: str,
    ) -> str: ...

    async def upload_skill(
        self,
        skill_dir: str,
        visibility: str = "public",
        origin: str | None = None,
        parent_skill_ids: list[str] | None = None,
        tags: list[str] | None = None,
        created_by: str | None = None,
        change_summary: str | None = None,
    ) -> str: ...


class EvolutionMCPToolImplementation(Protocol):
    async def evolve_from_context(
        self,
        task: str,
        summary: str,
        workspace_dir: str | None = None,
        file_paths: list[str] | None = None,
        max_skills: int = 3,
        skill_dirs: list[str] | None = None,
        output_dir: str | None = None,
    ) -> str: ...


def register_main_tools(mcp: FastMCP, impl: MainMCPToolImplementation) -> None:
    @mcp.tool()
    async def execute_task(
        task: str,
        workspace_dir: str | None = None,
        max_iterations: int | None = None,
        skill_dirs: list[str] | None = None,
        search_scope: str = "all",
    ) -> str:
        """Execute a task with OpenSpace's full grounding engine.

        OpenSpace will:
        1. Auto-register bot skills from skill_dirs (if provided)
        2. Search for relevant skills (scope controls local vs cloud+local)
        3. Attempt skill-guided execution → fallback to pure tools
        4. Auto-analyze → auto-evolve (FIX/DERIVED/CAPTURED) if needed

        If skills are auto-evolved, the response includes ``evolved_skills``
        with ``upload_ready: true``.  Call ``upload_skill`` with just the
        ``skill_dir`` + ``visibility`` to upload — metadata is pre-saved.

        Note: This call blocks until the task completes (may take minutes).
        Set MCP client tool-call timeout ≥ 600 seconds.

        Args:
            task: The task instruction (natural language).
            workspace_dir: Working directory. Defaults to OPENSPACE_WORKSPACE env.
            max_iterations: Max agent iterations (default: 20).
            skill_dirs: Bot's skill directories to auto-register so OpenSpace
                        can select and track them.  Directories are re-scanned
                        on every call to discover skills created since the last
                        invocation.
            search_scope: Skill search scope before execution.
                          "all" (default) — local + cloud; falls back to local
                          if no API key is configured.
                          "local" — local SkillRegistry only (fast, no cloud).
        """
        return await impl.execute_task(
            task=task,
            workspace_dir=workspace_dir,
            max_iterations=max_iterations,
            skill_dirs=skill_dirs,
            search_scope=search_scope,
        )

    @mcp.tool()
    async def search_skills(
        query: str,
        source: str = "all",
        limit: int = 20,
        auto_import: bool = True,
    ) -> str:
        """Search skills across local registry and cloud community.

        Standalone search for browsing / discovery.  Use this when the bot
        wants to find available skills, then decide whether to handle the
        task locally or delegate to ``execute_task``.

        **Scope difference from execute_task**:
          - ``search_skills`` returns results to the bot for decision-making.
          - ``execute_task``'s internal search feeds directly into execution
            (the bot never sees the search results).

        Uses hybrid ranking: BM25 → embedding re-rank → lexical boost.
        Embedding requires OPENAI_API_KEY; falls back to lexical-only without it.

        Args:
            query: Search query text (natural language or keywords).
            source: "all" (cloud + local), "local", or "cloud".  Default: "all".
            limit: Maximum results to return (default: 20).
            auto_import: Auto-download top public cloud skills (default: True).
        """
        return await impl.search_skills(
            query=query,
            source=source,
            limit=limit,
            auto_import=auto_import,
        )

    @mcp.tool()
    async def fix_skill(
        skill_dir: str,
        direction: str,
    ) -> str:
        """Manually fix a broken skill.

        This is the **only** manual evolution entry point.  DERIVED and
        CAPTURED evolutions are triggered automatically by ``execute_task``
        (they need a task to run).  Use ``fix_skill`` when:

          - A skill's instructions are wrong or outdated
          - The bot knows exactly which skill is broken and what to fix
          - Auto-evolution inside ``execute_task`` didn't catch the issue

        The skill does NOT need to be pre-registered in OpenSpace —
        provide the skill directory path and OpenSpace will register it
        automatically before fixing.

        After fixing, the new skill is saved locally and ``.upload_meta.json``
        is pre-written.  Call ``upload_skill`` with just ``skill_dir`` +
        ``visibility`` to upload.

        Args:
            skill_dir: Path to the broken skill directory (must contain SKILL.md).
            direction: What's broken and how to fix it.  Be specific:
                       e.g. "The API endpoint changed from v1 to v2" or
                       "Add retry logic for HTTP 429 rate limit errors".
        """
        return await impl.fix_skill(skill_dir=skill_dir, direction=direction)

    @mcp.tool()
    async def upload_skill(
        skill_dir: str,
        visibility: str = "public",
        origin: str | None = None,
        parent_skill_ids: list[str] | None = None,
        tags: list[str] | None = None,
        created_by: str | None = None,
        change_summary: str | None = None,
    ) -> str:
        """Upload a local skill to the cloud.

        For evolved skills (from ``execute_task`` or ``fix_skill``), most
        metadata is **pre-saved** in ``.upload_meta.json``.  The bot only
        needs to provide:

          - ``skill_dir`` — path to the skill directory
          - ``visibility`` — "public" or "private"

        All other parameters are optional overrides.  If omitted, pre-saved
        values are used.  If no pre-saved values exist, sensible defaults
        are applied.

        **origin + parent_skill_ids constraints** (enforced by cloud):
          - imported / captured → parent_skill_ids must be empty
          - derived → at least 1 parent
          - fixed → exactly 1 parent

        Args:
            skill_dir: Path to skill directory (must contain SKILL.md).
            visibility: "public" or "private".  This is the one thing the
                        bot MUST decide.
            origin: Override origin.  Default: from .upload_meta.json or "imported".
            parent_skill_ids: Override parents.  Default: from .upload_meta.json.
            tags: Override tags.  Default: from .upload_meta.json.
            created_by: Override creator.  Default: from .upload_meta.json.
            change_summary: Override summary.  Default: from .upload_meta.json.
        """
        return await impl.upload_skill(
            skill_dir=skill_dir,
            visibility=visibility,
            origin=origin,
            parent_skill_ids=parent_skill_ids,
            tags=tags,
            created_by=created_by,
            change_summary=change_summary,
        )


def register_evolution_tools(
    mcp: FastMCP,
    impl: EvolutionMCPToolImplementation,
) -> None:
    @mcp.tool()
    async def evolve_from_context(
        task: str,
        summary: str,
        workspace_dir: str | None = None,
        file_paths: list[str] | None = None,
        max_skills: int = 3,
        skill_dirs: list[str] | None = None,
        output_dir: str | None = None,
    ) -> str:
        """Capture reusable skills from a completed host-agent task.

        Use this when the main task was already handled by another agent
        (for example Codex Desktop) and OpenSpace should only spend provider
        tokens on post-task skill capture.

        Args:
            task: Short description of the completed task.
            summary: What changed, what was learned, and what seems reusable.
            workspace_dir: Repository/workspace path. Defaults to OPENSPACE_WORKSPACE.
            file_paths: Optional files worth emphasizing when planning captures.
            max_skills: Maximum number of new skills to capture.
            skill_dirs: Optional additional skill directories to register first.
            output_dir: Override directory for new skills. Defaults to the first
                OPENSPACE_HOST_SKILL_DIRS entry.
        """
        return await impl.evolve_from_context(
            task=task,
            summary=summary,
            workspace_dir=workspace_dir,
            file_paths=file_paths,
            max_skills=max_skills,
            skill_dirs=skill_dirs,
            output_dir=output_dir,
        )
