"""Prompts for the skill engine subsystem."""

class SkillEnginePrompts:
    """Central registry of prompts used by the skill engine."""

    @staticmethod
    def evolution_fix(
        *,
        current_content: str,
        direction: str,
        failure_context: str,
    ) -> str:
        """Build the prompt for a FIX evolution (in-place repair).

        Args:
            current_content: Current SKILL.md content.
            direction: What to fix and why (from suggestion or diagnosis).
            failure_context: Formatted recent analysis context showing failures.
        """
        return _EVOLUTION_FIX_TEMPLATE.format(
            current_content=current_content,
            direction=direction,
            failure_context=failure_context,
            runtime_overlay_guide=_RUNTIME_OVERLAY_GUIDE,
            finalization_guide=_EVOLUTION_FINALIZATION_GUIDE,
        )

    @staticmethod
    def evolution_derived(
        *,
        parent_content: str,
        direction: str,
        execution_insights: str,
    ) -> str:
        """Build the prompt for a DERIVED evolution (enhanced version).

        Args:
            parent_content: Parent SKILL.md content.
            direction: What to enhance and why.
            execution_insights: Formatted analysis context with improvement signals.
        """
        return _EVOLUTION_DERIVED_TEMPLATE.format(
            parent_content=parent_content,
            direction=direction,
            execution_insights=execution_insights,
            runtime_overlay_guide=_RUNTIME_OVERLAY_GUIDE,
            finalization_guide=_EVOLUTION_FINALIZATION_GUIDE,
        )

    @staticmethod
    def evolution_captured(
        *,
        direction: str,
        category: str,
        execution_highlights: str,
    ) -> str:
        """Build the prompt for a CAPTURED evolution (brand-new skill).

        Args:
            direction: What pattern to capture.
            category: Desired skill category (tool_guide / workflow / reference).
            execution_highlights: Task context where the pattern was observed.
        """
        return _EVOLUTION_CAPTURED_TEMPLATE.format(
            direction=direction,
            category=category,
            execution_highlights=execution_highlights,
            runtime_overlay_guide=_RUNTIME_OVERLAY_GUIDE,
            finalization_guide=_EVOLUTION_FINALIZATION_GUIDE,
        )

    @staticmethod
    def evolution_confirm(
        *,
        skill_id: str,
        skill_content: str,
        proposed_type: str,
        proposed_direction: str,
        trigger_context: str,
        recent_analyses: str,
    ) -> str:
        """Build the prompt for LLM confirmation of rule-based evolution candidates.

        Args:
            skill_id: Unique skill_id of the candidate skill.
            skill_content: Truncated SKILL.md content.
            proposed_type: "fix" or "derived".
            proposed_direction: What the rule-based system suggests.
            trigger_context: Summary of the trigger (metrics or tool issue).
            recent_analyses: Formatted recent execution analyses.
        """
        return _EVOLUTION_CONFIRM_TEMPLATE.format(
            skill_id=skill_id,
            skill_content=skill_content,
            proposed_type=proposed_type,
            proposed_direction=proposed_direction,
            trigger_context=trigger_context,
            recent_analyses=recent_analyses,
        )

    @staticmethod
    def execution_analysis(
        *,
        task_description: str,
        execution_status: str,
        iterations: int,
        tool_list: str,
        skill_section: str,
        conversation_log: str,
        traj_summary: str,
        selected_skill_ids_json: str,
        resource_info: str = "",
    ) -> str:
        """Build the prompt for post-execution skill quality analysis.

        Args:
            task_description: Human-readable description of the task.
            execution_status: Agent's self-reported status ("success" / "incomplete" / "error").
                NOT ground truth — the analysis LLM assesses actual completion independently.
            iterations: Number of agent iterations used.
            tool_list: List of available tool names with backend info.
            skill_section: Pre-formatted markdown section describing selected skills.
                Empty string when no skills were selected.
            conversation_log: Formatted execution log (priority-truncated to fit context).
            traj_summary: Structured tool execution timeline from traj.jsonl.
            selected_skill_ids_json: JSON-encoded list of selected skill IDs.
            resource_info: Recording / skill directory paths and tool-use guidance.
        """
        return _EXECUTION_ANALYSIS_TEMPLATE.format(
            task_description=task_description,
            execution_status=execution_status,
            iterations=iterations,
            tool_list=tool_list,
            skill_section=skill_section,
            conversation_log=conversation_log,
            traj_summary=traj_summary,
            selected_skill_ids_json=selected_skill_ids_json,
            resource_info=resource_info,
        )

_RUNTIME_OVERLAY_GUIDE = """\
## Optional Runtime Overlay Suggestions

Do not put high-risk runtime fields directly in the SKILL.md frontmatter.
If this skill would benefit from runtime behavior that needs human review,
put this optional object inside the finalization block as ``runtime_overlay``:

{
  "fields": {
    "when_to_use": "Use when ...",
    "allowed-tools": ["bash"],
    "context": "fork",
    "agent": "general-purpose",
    "shell": "bash",
    "hooks": {
      "PreToolUse": [
        {
          "matcher": "bash",
          "hooks": [
            {
              "type": "command",
              "command": "python \"$OPENSPACE_SKILL_ROOT/scripts/check.py\" $ARGUMENTS"
            }
          ]
        }
      ]
    }
  },
  "rationale": {
    "allowed-tools": "The workflow only needs bash access.",
    "hooks": "The pre-tool hook validates risky shell inputs before execution."
  }
}

Rules for runtime overlay suggestions:
- Use ``"runtime_overlay": null`` when no runtime fields are needed.
- Only include fields that materially improve future executions.
- Use the canonical field names below. Legacy SKILL.md may use
  ``when-to-use``; normalize it to ``when_to_use`` in runtime overlay JSON.
- Field schema and runtime meaning:
  - ``when_to_use``: string, one concise sentence beginning with "Use when ...".
    Improves skill discovery, search, and model-facing skill descriptions.
  - ``description``: string, a short user-facing capability summary. Replaces
    the runtime description shown in skill listings.
  - ``argument-hint``: string, compact CLI-style argument hint such as
    "<file> [--mode fast]". Helps users understand expected invocation input.
  - ``arguments``: non-empty array of non-numeric argument names, or a
    whitespace-separated string of those names. Enables named argument
    substitution in skill body placeholders.
  - ``version``: string. Records the runtime overlay/version label; it does
    not by itself change execution behavior.
  - ``paths``: non-empty array of relative path prefixes/globs. Makes the
    skill conditional: it becomes visible after matching files are touched.
  - ``user-invocable``: boolean. Controls whether users may invoke the skill
    directly.
  - ``disable-model-invocation``: boolean. Prevents the model from invoking
    the skill automatically while still allowing other permitted uses.
  - ``allowed-tools``: non-empty array of tool/backend names already available
    to the agent, e.g. ["bash"]. Restricts or grants the tool surface available
    inside this skill's execution scope.
  - ``model``: string model override. Requests a different model while this
    skill is active.
  - ``effort``: string effort/reasoning override. Requests a different
    reasoning effort while this skill is active.
  - ``context``: string; only "fork" is supported. Runs the skill in a forked
    agent context instead of inline in the current agent.
  - ``agent``: string forked-agent type, e.g. "general-purpose". Selects the
    agent type used when ``context`` is "fork".
  - ``shell``: string; only "bash" or "powershell" is supported. Selects the
    shell used for skill hook command execution and shell-oriented rendering.
  - ``hooks``: non-empty object keyed by hook event name. Registers scoped
    runtime hooks while the skill is active. Each event value is an array of
    matchers, where each matcher contains optional ``matcher`` and a non-empty
    ``hooks`` array. Hook entries may be ``command``, ``prompt``, ``http``, or
    ``agent`` hooks.
- High-risk fields (allowed-tools, hooks, shell, model, effort, context,
  agent, user-invocable, disable-model-invocation) are saved only as pending
  suggestions and require explicit user approval before they affect runtime.
- Never include secrets, credentials, private tokens, or machine-specific
  absolute paths in runtime overlay values.
"""

_EVOLUTION_FINALIZATION_GUIDE = """\
## Structured Finalization

When the evolution is ready to stop, the final assistant response must end with
exactly one finalization block. This block is the only signal that the evolution
loop may stop.

If you need tools, call the tools and do not include a finalization block in
that tool-calling response. Emit the finalization block only in a normal text
response after the edit is ready or after you determine the evolution cannot be
completed.

For a successful edit, output the edit content first, then append:

*** Begin Evolution Finalization
{
  "status": "complete",
  "change_summary": "One sentence describing the edit.",
  "intent_spec": {
    "capability": "The reusable capability this skill gives the agent.",
    "trigger_contexts": [
      "Concrete user/task wording or runtime context where this skill should trigger."
    ],
    "non_trigger_contexts": [
      "Near-miss wording or contexts where this skill should not trigger."
    ],
    "expected_artifacts": [
      "Files, outputs, or decisions expected when the skill is used."
    ],
    "success_criteria": [
      "Observable criteria that show the skill helped."
    ],
    "tool_dependencies": [
      "Only tools materially required by the workflow."
    ],
    "resource_plan": {
      "scripts": "none, or the reusable scripts included and why",
      "references": "none, or the reference files included and why",
      "assets": "none, or the assets included and why"
    },
    "parent_difference": "For DERIVED only: how this differs from the parent skill.",
    "observed_pattern": "For CAPTURED only: the observed reusable pattern.",
    "generalization_boundary": "For CAPTURED only: what was abstracted away and where this should not apply."
  },
  "eval_plan": {
    "positive_trigger_queries": [
      "A realistic user/task query that should select this skill."
    ],
    "negative_trigger_queries": [
      "A realistic near-miss query that should not select this skill."
    ],
    "replay_tasks": [
      {
        "prompt": "A realistic task prompt for paired baseline-vs-candidate replay.",
        "task_id": "optional-source-or-generated-id",
        "judge_policy": "deterministic|llm|gdpval|hybrid|manual",
        "expected_outcome": "What should improve or remain non-regressed."
      }
    ],
    "deterministic_assertions": [
      {
        "type": "file_exists|error_absent|artifact_valid|manual",
        "target": "What to check",
        "expected": true,
        "description": "Why this assertion proves the skill helped."
      }
    ],
    "judge_policy": "hybrid",
    "success_criteria": [
      "Quality must not regress and the observed failure/inefficiency should disappear."
    ],
    "baseline": "active"
  },
  "runtime_overlay": null
}
*** End Evolution Finalization

If runtime overlay suggestions are needed, replace ``null`` with the
``runtime_overlay`` object described above.

For failure, output only:

*** Begin Evolution Finalization
{
  "status": "failed",
  "reason": "Brief explanation of why this evolution cannot be completed."
}
*** End Evolution Finalization

Rules:
- The finalization block must be the last content in the response.
- Do not put text after the finalization block.
- On success, the edit content before the finalization block must be directly
  applicable by the skill edit applier.
- ``intent_spec`` and ``eval_plan`` are required for every FIX, DERIVED, and
  CAPTURED action. They are not optional notes: they drive behavior-evaluation
  before the skill can be committed.
- Include at least one positive trigger query and one near-miss negative trigger
  query. For FIX and DERIVED, include at least one replay task. For CAPTURED,
  include replay tasks whenever the source execution can be replayed; otherwise
  include deterministic assertions and explain the limitation in ``notes``.
- Keep eval prompts realistic. Do not include hidden expected answers, intended
  fixes, or conclusions that would leak the evaluation target.
- On failure, do not output any edit content.
"""

_EXECUTION_ANALYSIS_TEMPLATE = """\
You are an expert analyst evaluating an autonomous agent's task execution.
Your job is to assess how the agent used its skills and tools, trace the
reasoning and outcome of each iteration, and surface actionable insights.

## Task Context

**Task**: {task_description}
**Agent self-reported status**: {execution_status}
**Iterations used**: {iterations}
**Available tools**: {tool_list}

> This is the agent's **self-reported** status, not ground truth.
> ``success`` = agent output ``<COMPLETE>`` (may be wrong/premature);
> ``incomplete`` = iteration budget exhausted; ``error`` = code exception.
> You must independently judge actual task completion below.

{skill_section}

## Tool Execution Timeline (from traj.jsonl)

This is a structured summary of every tool invocation and its outcome:

{traj_summary}

## Agent Conversation Log

This shows the agent's reasoning (ASSISTANT), tool calls (TOOL_CALL),
tool results (TOOL_RESULT / TOOL_ERROR), and the user's original instruction.

**Reading guide**:
- ``[USER INSTRUCTION]`` — the original task from the user.
- ``[Iter N] ASSISTANT:`` — the agent's reasoning and decisions at iteration N.
- ``[Iter N] TOOL_CALL:`` — what tool the agent invoked and with what arguments.
- ``[Iter N] TOOL_ERROR:`` — tool returned an error (high priority for analysis).
- ``[Iter N] TOOL_RESULT:`` — tool returned successfully.
  Some tool results include an embedded summary from tool-specific execution.
- ``[Iter N] TOOL_RESULT_EVIDENCE:`` — a compact pointer for persisted large
  results, including tool name, tool call ID, and saved full-output path. Relative
  paths are under the recording directory listed below.

{conversation_log}

## Available Resources

{resource_info}

## Analysis Instructions

### 1. Per-iteration trace

For each agent iteration, identify:
- **What** the agent decided to do and **why** (from ASSISTANT content).
- **Which tool** was called and what happened (success / error / timeout).
- **Cause of next iteration**: did the agent retry due to error? Switch strategy?
  Follow a skill step? Or complete the task?

### 2. Task completion assessment

Did the agent **actually** accomplish the user's request?
Judge from conversation evidence (tool results, final output), **not** the
self-reported status.

- ``task_completed = true`` ONLY when the user's goal is genuinely fulfilled.
- Watch for mismatches: agent may claim ``<COMPLETE>`` after giving up or
  getting wrong results; conversely, it may finish the work but exhaust
  iterations without outputting ``<COMPLETE>``.
- Explain your reasoning in ``execution_note``.

### 3. Skill assessment

For each selected or dynamically retrieved skill (IDs: {selected_skill_ids_json}), produce one
``skill_judgments`` entry:
- ``skill_id``: Use the **exact skill_id** from the list above (e.g.
  ``weather__imp_a1b2c3d4``).  Do NOT use the human-readable name alone.
- ``skill_applied``: Was the skill's information **actually used** (not just injected)?
  - WORKFLOW skill: did the agent follow the prescribed steps?
  - TOOL_GUIDE skill: did the agent use the tool as the guide describes?
  - REFERENCE skill: did the agent rely on the knowledge for decisions?
- ``note``: Describe HOW the skill was used. If it wasn't applied, explain why.

If the execution metadata says the skill-guided phase failed and a later
tool-only fallback ran, judge whether the skill was applied, but do **not**
credit that skill for the final fallback success.
Also list those exact IDs in top-level ``skill_phase_failed_skill_ids`` so
quality counters can record fallback without crediting completion.

If no skills were selected, ``skill_judgments`` must be an empty list.

### 4. Tool issues (separate from skill assessment)

List **only tools that had actual problems** during this execution.
Do NOT list tools that worked correctly or were simply unused.

**Tool key format** — use the key that matches the tool list above:
- MCP tools: ``mcp:server_name:tool_name``
- Other tools: ``backend:tool_name``

For each problematic tool, include:
- The **symptom** (error, timeout, wrong output, semantic failure, etc.).
- The **likely cause** if you can infer it (network issue, tool bug, bad parameters,
  misleading description, etc.).
- Whether the issue is the **tool's fault** or the **agent's misuse** of the tool.

These issues are fed to a tool quality tracking system. If the tool returned HTTP 200
but the data is incorrect or unusable, still flag it — your qualitative judgment
complements the raw success/failure tracking.

### 5. Evolution suggestions

The skill library improves through execution feedback. **If something went wrong,
fix it. If something useful was learned, capture or derive it.** Actively look for evolution
opportunities — they are how the system gets smarter over time.

You may output **0 to N** suggestions. Each suggestion is one of three types:

| Type | When to use | ``target_skills`` |
|------|------------|-------------------|
| ``fix`` | A selected skill had **incorrect, outdated, or incomplete** instructions that caused failure, deviation, or unnecessary friction. The skill needs repair. | ``["skill_id"]`` — exactly 1 skill, use the exact skill_id |
| ``derived`` | A selected skill worked, but the execution revealed a **better approach** — improved steps, added error handling, broader scope, or useful edge-case handling. Worth creating an enhanced version. Can also **merge** multiple skills. | ``["parent_skill_id"]`` or ``["skill_id_a", "skill_id_b"]`` for merge |
| ``captured`` | The agent solved the task **without skill guidance** (or skills were not relevant) and the approach is **reusable** — a debugging technique, a tool usage pattern, a multi-step workflow, a non-obvious workaround. | ``[]`` (empty list) |

**One type per suggestion**: Each suggestion MUST have exactly one ``type`` — pick
``fix``, ``derived``, OR ``captured``. A single suggestion cannot be two types at once.
Different suggestions in the same analysis MAY have different types (e.g. one ``fix``
for a broken skill and one ``captured`` for a novel pattern are both fine).

**Guiding principles:**
- ``fix``: If the skill's instructions led the agent astray, caused errors, or missed
  important steps/caveats, it should be fixed. Suboptimal instructions that cost extra
  iterations also warrant a fix.
- ``derived``: If the agent found a meaningfully better way to accomplish what a skill
  describes — even if the original skill "worked" — suggest deriving a new version.
  The improvement should be generalizable beyond this specific task.
- ``captured``: Capture one useful pattern only when the trace contains both the
  executed procedure and a separate check that directly validates its claimed
  postcondition. Overall task success is neither required nor sufficient: a failed task
  may contain a validated subworkflow, while a completed task may contain an unvalidated
  technique.
- **Do NOT** capture trivial one-step operations or highly task-specific data unlikely to recur.
- **Do NOT** capture something that an existing selected skill already covers adequately.
- **Do NOT** capture a proposed correction that was never executed, or treat a command's
  zero exit status as proof that its computed answer is correct.

For each suggestion, specify:
- ``type``: ``"fix"`` | ``"derived"`` | ``"captured"``
- ``target_skills``: list of **exact skill_id(s)** from the selected skills above —
  ``["weather__imp_a1b2c3d4"]`` for fix (exactly 1),
  ``["skill_id_1"]`` or ``["skill_id_1", "skill_id_2"]`` for derived (1+ for merge),
  ``[]`` for captured
- ``category``: ``"tool_guide"`` | ``"workflow"`` | ``"reference"``
- ``local_category_path``: local package-taxonomy path for the resulting skill,
  e.g. ``"technology/computing/browser-automation"`` or
  ``"data/apis/weather/geocoding"``. This path uses the same taxonomy style as
  cloud package paths, but it belongs to the local tree and may diverge from the
  cloud tree. For ``derived`` and ``captured``, inspect the Local taxonomy tree
  in the resource info and choose an existing path when it fits, otherwise
  create a nearby/finer child path.
- ``direction``: 1-2 sentences describing **what** to fix / derive / capture
- ``capture_contract``: required for ``captured`` and omitted for other types. It
  defines exactly one reusable capability with:
  - ``capability``: one precise, reusable outcome
  - ``preconditions``: conditions under which the procedure applies
  - ``procedure_refs``: exact selected ref IDs showing the procedure was executed
  - ``validation_refs``: exact selected ref IDs from a different tool call or
    observation showing an independent readback, test, comparison, or postcondition
  - ``validation_summary``: what those validation refs establish
  - ``limitations``: known boundaries and claims that remain unverified

### When you need more information

**In most cases the trace data above is sufficient.** If not:
1. Use ``read`` / ``ls`` to inspect recording artifacts or output files.
2. If still unclear, use ``bash`` or other available tools to reproduce the error.

### Output format

Return **exactly one** JSON object (no markdown fences, no explanation outside JSON):

{{
  "task_completed": true,
  "execution_note": "2-3 sentence overview of execution quality and outcome.",
  "tool_issues": [
    "mcp:server_name:tool_name — symptom; likely cause (tool fault / agent misuse)",
    "backend:tool_name — symptom; likely cause"
  ],
  "skill_judgments": [
    {{
      "skill_id": "weather__imp_a1b2c3d4",
      "skill_applied": true,
      "note": "How the skill was used, deviations, and effectiveness."
    }}
  ],
  "skill_phase_failed_skill_ids": ["weather__imp_a1b2c3d4"],
  "evolution_suggestions": [
    {{
      "type": "fix",
      "target_skills": ["weather__imp_a1b2c3d4"],
      "category": "workflow",
      "local_category_path": "data/apis/weather",
      "direction": "What to fix and why."
    }},
    {{
      "type": "captured",
      "target_skills": [],
      "category": "workflow",
      "local_category_path": "data/apis/weather/geocoding",
      "direction": "Capture the source-validated geocoding fallback only.",
      "capture_contract": {{
        "capability": "Resolve a place name with the demonstrated fallback API and verify the returned coordinates.",
        "preconditions": ["The fallback API is reachable."],
        "procedure_refs": ["tool_event:exact-procedure-ref"],
        "validation_refs": ["tool_event:exact-readback-ref"],
        "validation_summary": "A separate lookup checked that the coordinates resolve to the requested place.",
        "limitations": ["No evidence supports other providers or offline operation."]
      }}
    }}
  ]
}}

**Rules**:
- ``skill_judgments`` must include exactly one entry per selected skill ID.
  If no skills were selected, ``skill_judgments`` must be ``[]``.
- ``skill_phase_failed_skill_ids``: exact selected skill IDs whose
  skill-guided phase failed before a later tool-only fallback completed the
  task. Use ``[]`` when no such fallback phase occurred.
- ``tool_issues``: ``"key — description"`` format (MCP: ``mcp:server:tool``, other: ``backend:tool``). ``[]`` if no problems.
- ``evolution_suggestions``: ``[]`` only if the execution revealed no issues to fix and no reusable patterns to capture.
  For ``fix``, ``target_skills`` must be a list with exactly 1 skill name from the selected skills.
  For ``derived``, ``target_skills`` must be a list with 1 or more skill names (multi = merge).
  For ``captured``, ``target_skills`` must be ``[]``.
  For ``captured``, ``capture_contract`` is mandatory. Its procedure and validation
  refs must be non-empty, copied exactly from Selected refs, and come from distinct
  source observations. A tool event and tool result from the same tool call are not
  independent validation. If no independent validation exists, do not emit the suggestion.
  The capability may contain only outcomes established by the validation refs. If a
  check validates one subset, mode, period, field, or output property, narrow the
  capability to exactly that subset instead of capturing the broader procedure.
  Do not emit a captured suggestion when its own limitations admit that correctness
  or accuracy of the capability's primary output was not verified. Narrow the
  capability to the independently validated postcondition or emit no suggestion.
  Do not capture an incomplete security, integrity, privacy, or correctness safeguard
  with known bypasses merely because those bypasses are disclosed as limitations.
  Do not capture techniques framed as bypassing, circumventing, or evading a
  permission, approval, authorization, access-control, or security-control boundary.
  ``category``: one of ``"tool_guide"``, ``"workflow"``, ``"reference"``.
  ``local_category_path``: local package-taxonomy path; omit only when the Local
  taxonomy tree is unavailable and no reasonable placement can be inferred.
- ``execution_note``: substantive but concise (2-3 sentences).
"""


_EVOLUTION_FIX_TEMPLATE = """\
You are a skill editor. Your job is to **fix** an existing skill that has
been identified as broken, outdated, or incomplete.

A skill is a directory containing ``SKILL.md`` (the main instruction file)
and optionally auxiliary files (scripts, configs, examples, etc.).

## Current Skill Content

{current_content}

## What needs fixing

{direction}

## Execution failure context

These are recent task executions where this skill was involved:

{failure_context}

## Instructions

1. Analyze the failure context and identify the root cause in the skill's
   instructions (wrong parameters, outdated API, missing error handling, etc.).
2. Fix the affected files to address the identified issues.
3. Preserve the overall structure and YAML frontmatter format (``---`` fences)
   in SKILL.md.
4. Keep ``name`` and ``description`` in frontmatter; update ``description``
   only if the skill's purpose has changed.
5. Be surgical — fix what's broken without unnecessary rewrites.

## Output format

Your output MUST have exactly two parts:

**Part 1** — The actual changes in one of the formats below.

**Part 2** — The structured finalization block described after the runtime
overlay section.

### Format A: Patch (PREFERRED for fixes — use this unless you need a full rewrite)

The patch format lets you make surgical, targeted edits across one or more
files.  Structure:

*** Begin Patch
*** Update File: <relative path>
@@ <anchor line>
 <unchanged context line>
-<line to remove>
+<line to add>
 <unchanged context line>
*** End Patch

**How ``@@`` anchor lines work** (NOT the same as unified-diff ``@@ -n,m +n,m @@``):
- Write ``@@`` followed by a single line that already exists verbatim in the
  file.  The system searches forward for this line and applies the changes
  immediately after locating it.
- After the ``@@`` line, prefix every line with exactly one character:
  ``-`` = delete this old line, ``+`` = insert this new line,
  `` `` (one space) = keep this line unchanged (context).
- You may have multiple ``@@`` sections inside one ``*** Update File`` block.

Other operations:
- ``*** Add File: path`` — every content line prefixed with ``+``.
- ``*** Delete File: path`` — no content lines needed.

Example — fixing an incorrect curl parameter and adding a missing step:

*** Begin Patch
*** Update File: SKILL.md
@@ 3. Send the API request:
 3. Send the API request:
-   curl -X POST -H "Content-Type: text/plain" ...
+   curl -X POST -H "Content-Type: application/json" ...
@@ ## Error handling
 ## Error handling
+
+4. **Retry on transient failures**: If you receive a 429 or 5xx status,
+   wait 2 seconds and retry up to 3 times.
*** End Patch

### Format B: Full rewrite (only when most of the content changes)

If the fix is so extensive that a patch would be larger than the full file,
output the complete file contents instead:

*** Begin Files
*** File: SKILL.md
(complete file content)
*** File: examples/helper.sh
(complete file content)
*** End Files

For single-file skills you may omit the ``*** Begin/End Files`` envelope
and output the complete SKILL.md content directly.

### Rules

- Do NOT wrap your output in markdown code fences (no ``` blocks).
- Prefer Format A (patch) for fixes — it is more precise and less error-prone.
- Only use Format B when the patch would touch more than ~60% of the file.

{runtime_overlay_guide}

{finalization_guide}
"""


_EVOLUTION_DERIVED_TEMPLATE = """\
You are a skill editor. Your job is to **derive** an enhanced version of an
existing skill.  The new skill will live in a new directory; the original
stays unchanged.

A skill is a directory containing ``SKILL.md`` (the main instruction file)
and optionally auxiliary files (scripts, configs, examples, etc.).

## Parent Skill Content

{parent_content}

## Enhancement direction

{direction}

## Execution insights

These are recent task executions that informed this enhancement:

{execution_insights}

## Instructions

1. Create an enhanced version that addresses the improvement direction.
2. Give the new skill a **different, concise name** (in frontmatter ``name:`` field)
   that reflects its specialization or enhancement.
   - Name MUST be ≤50 characters, lowercase, hyphens only (e.g. ``resilient-panel-unified``).
   - Do NOT just append "-enhanced" or "-merged" to the parent name.
     Instead, pick a descriptive name that captures the NEW capability
     (e.g. ``panel-circuit-breaker`` instead of ``panel-component-enhanced-enhanced``).
3. Update ``description`` to reflect the new capability.
4. You may restructure, add steps, improve error handling, add alternatives,
   or broaden/narrow scope as appropriate.
5. Maintain the YAML frontmatter format (``---`` fences with ``name`` and
   ``description`` at minimum).
6. The derived skill should be self-contained — a user should be able to
   follow it without referencing the parent.
7. You may add, modify, or remove auxiliary files as needed.

## Output format

Your output MUST have exactly two parts:

**Part 1** — The actual changes in one of the formats below.

**Part 2** — The structured finalization block described after the runtime
overlay section.

### Choosing a format

- **Small enhancement** (new steps, improved wording, added error handling
  while keeping most content intact): use Format A (patch).
- **Major restructure** or **substantially different skill**: use Format B
  (full rewrite).  This is also the best choice when creating a merged skill
  from multiple parents.

### Format A: Patch

*** Begin Patch
*** Update File: <relative path>
@@ <anchor line>
 <unchanged context line>
-<line to remove>
+<line to add>
 <unchanged context line>
*** Add File: <new file path>
+<new line 1>
+<new line 2>
*** End Patch

**How ``@@`` anchor lines work** (NOT unified-diff ``@@ -n,m +n,m @@``):
- ``@@`` followed by a line that exists verbatim in the file.  The system
  locates this line and applies changes starting there.
- After ``@@``, prefix lines with: ``-`` remove, ``+`` add, `` `` (space) keep.
- Multiple ``@@`` sections per file are allowed.

Example — renaming and enhancing a skill:

*** Begin Patch
*** Update File: SKILL.md
@@ name: api-request-guide
-name: api-request-guide
-description: How to make single API requests
+name: api-request-guide-enhanced
+description: Robust API requests with retry logic and batch support
@@ ## Steps
 ## Steps
+
+0. **Pre-check**: Verify the API endpoint is reachable with a HEAD request.
@@ 3. Send the request
 3. Send the request
+4. **Handle failures**: On 429/5xx, back off exponentially (1s, 2s, 4s)
+   up to 3 retries before reporting an error.
*** Add File: examples/batch_request.sh
+#!/bin/bash
+# Batch API request example
+for endpoint in "$@"; do
+  curl -s "$endpoint" || echo "FAILED: $endpoint"
+done
*** End Patch

### Format B: Full rewrite

*** Begin Files
*** File: SKILL.md
(complete file content)
*** File: examples/helper.sh
(complete file content)
*** End Files

For single-file skills you may omit the envelope and output the complete
SKILL.md content directly.

### Rules

- Do NOT wrap your output in markdown code fences (no ``` blocks).
- The new skill MUST have a different ``name`` from the parent.

{runtime_overlay_guide}

{finalization_guide}
"""


_EVOLUTION_CAPTURED_TEMPLATE = """\
You are a skill author. Your job is to **capture** a reusable pattern that
was observed during task executions into a brand-new skill.

A skill is a directory containing ``SKILL.md`` (the main instruction file)
and optionally auxiliary files (scripts, configs, examples, etc.).

## Pattern to capture

{direction}

## Desired category

``{category}``

Categories:
- ``tool_guide``: How to use a specific tool effectively
- ``workflow``: End-to-end multi-step procedure
- ``reference``: Reference knowledge / best practices

## Execution context

These are task executions where the pattern was observed:

{execution_highlights}

## Instructions

1. Distill the observed pattern into a clear, reusable skill document.
2. Choose a concise, descriptive ``name`` (lowercase, hyphens for spaces).
   - Name MUST be ≤50 characters (e.g. ``safe-file-write``, ``ts-compile-check``).
   - Capture the core technique, not every detail.
3. Write a brief ``description`` that captures the skill's purpose.
4. Structure the body as clear, actionable instructions that an autonomous
   agent can follow.  Include code examples where helpful.
5. Make the skill reusable only within the admitted preconditions and limitations.
   Abstract incidental task-specific values, but never broaden the capability,
   add an unvalidated operational step, or present an unverified step with a warning.
   Every executable example must be internally complete: do not leave undefined
   variables, omitted algorithm steps, TODOs, or pseudocode presented as runnable code.
6. Use YAML frontmatter format (``---`` fences with ``name`` and
   ``description``).
7. If the pattern benefits from auxiliary files (shell scripts, config
   templates, etc.), include them.

## Output format

Your output MUST have exactly two parts:

**Part 1** — The complete skill content.

**Part 2** — The structured finalization block described after the runtime
overlay section.

Since this is a brand-new skill, always output the **full content**.

**If the skill has multiple files**, use the multi-file full format:

*** Begin Files
*** File: SKILL.md
---
name: my-skill-name
description: What this skill does
---

# My Skill

Instructions here...
*** File: examples/setup.sh
#!/bin/bash
echo "setup script"
*** End Files

**If the skill is just SKILL.md** (most common), output the complete
SKILL.md content directly (no ``*** Begin/End Files`` envelope needed):

---
name: my-skill-name
description: What this skill does
---

# My Skill

Step-by-step instructions...

### Rules

- Do NOT wrap your output in markdown code fences (no ``` blocks).
- Start immediately with the skill content. Do not include analysis, planning,
  evidence-reading narration, or a second frontmatter block.
- The SKILL.md MUST start with YAML frontmatter (``---`` fences) containing
  at least ``name`` and ``description``.

{runtime_overlay_guide}

{finalization_guide}
"""


_EVOLUTION_CONFIRM_TEMPLATE = """\
You are an expert evaluating whether a skill needs evolution.

A rule-based monitoring system has flagged a skill as a candidate for
evolution based on evidence-backed execution analysis. Your job is to
**confirm or reject** this recommendation by examining the skill content
and recent execution history.

## Skill Under Review

**ID**: {skill_id}

**Content** (may be truncated):

{skill_content}

## Proposed Evolution

**Type**: ``{proposed_type}``
**Direction**: {proposed_direction}

## Trigger Context

{trigger_context}

## Recent Execution History

{recent_analyses}

## Decision Criteria

Consider these factors:

1. **Is the signal real?** Could the poor metrics be caused by external
   factors (task distribution shift, temporary tool outage) rather than
   a genuine skill deficiency?

2. **Is the skill actually problematic?** Read the skill content — are
   the instructions actually wrong/outdated, or are the metrics
   misleading?

3. **Is evolution worth the cost?** Would fixing/deriving this skill
   meaningfully improve future executions, or is the skill rarely used
   and not worth the LLM cost?

4. **Is the proposed direction correct?** Does the suggested fix/derive
   direction address the actual root cause?

## Output Format

Return **exactly one** JSON object (no markdown fences):

{{
  "proceed": true,
  "reasoning": "1-2 sentence explanation of your decision.",
  "adjusted_direction": "Optional: refined direction if you agree but want to adjust the approach. Omit or set to empty string if the original direction is fine."
}}

Set ``"proceed": false`` to skip this evolution.
Set ``"proceed": true`` to confirm it should proceed.
"""
