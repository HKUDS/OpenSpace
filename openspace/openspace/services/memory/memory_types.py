"""Typed auto-memory taxonomy.

Implementation notes: ``memdir/memoryTypes.ts`` (272 lines).  The taxonomy is kept
closed: memories are one of user, feedback, project, or reference.
"""

from __future__ import annotations

from typing import Literal, Optional

MemoryType = Literal["user", "feedback", "project", "reference"]
MEMORY_TYPES: tuple[MemoryType, ...] = (
    "user",
    "feedback",
    "project",
    "reference",
)


def parse_memory_type(raw: object) -> Optional[MemoryType]:
    """Parse a raw frontmatter value into a known memory type."""

    if not isinstance(raw, str):
        return None
    normalized = raw.strip()
    return normalized if normalized in MEMORY_TYPES else None  # type: ignore[return-value]


TYPES_SECTION_COMBINED: tuple[str, ...] = (
    "## Types of memory",
    "",
    "There are several discrete types of memory that you can store in your memory system. Each type below declares a <scope> of `private`, `team`, or guidance for choosing between the two.",
    "",
    "<types>",
    "<type>",
    "    <name>user</name>",
    "    <scope>always private</scope>",
    "    <description>Contain information about the user's role, goals, responsibilities, and knowledge. Great user memories help you tailor your future behavior to the user's preferences and perspective. Your goal in reading and writing these memories is to build up an understanding of who the user is and how you can be most helpful to them specifically. Avoid writing memories about the user that could be viewed as a negative judgement or that are not relevant to the work you're trying to accomplish together.</description>",
    "    <when_to_save>When you learn any details about the user's role, preferences, responsibilities, or knowledge</when_to_save>",
    "    <how_to_use>When your work should be informed by the user's profile or perspective.</how_to_use>",
    "</type>",
    "<type>",
    "    <name>feedback</name>",
    "    <scope>default to private. Save as team only when the guidance is clearly a project-wide convention that every contributor should follow, not a personal style preference.</scope>",
    "    <description>Guidance the user has given you about how to approach work - both what to avoid and what to keep doing. Record from failure and success; confirmations are quieter but can be just as important as corrections.</description>",
    "    <when_to_save>Any time the user corrects your approach or confirms a non-obvious approach worked. Include why so you can judge edge cases later.</when_to_save>",
    "    <how_to_use>Let these memories guide your behavior so the user and team do not need to offer the same guidance twice.</how_to_use>",
    "    <body_structure>Lead with the rule itself, then a **Why:** line and a **How to apply:** line.</body_structure>",
    "</type>",
    "<type>",
    "    <name>project</name>",
    "    <scope>private or team, but strongly bias toward team</scope>",
    "    <description>Information that you learn about ongoing work, goals, initiatives, bugs, or incidents within the project that is not otherwise derivable from the code or git history.</description>",
    "    <when_to_save>When you learn who is doing what, why, or by when. Convert relative dates to absolute dates when saving.</when_to_save>",
    "    <how_to_use>Use these memories to understand the context behind the user's request and make better informed suggestions.</how_to_use>",
    "    <body_structure>Lead with the fact or decision, then a **Why:** line and a **How to apply:** line.</body_structure>",
    "</type>",
    "<type>",
    "    <name>reference</name>",
    "    <scope>usually team</scope>",
    "    <description>Stores pointers to where information can be found in external systems.</description>",
    "    <when_to_save>When you learn about resources in external systems and their purpose.</when_to_save>",
    "    <how_to_use>When the user references an external system or information that may be in an external system.</how_to_use>",
    "</type>",
    "</types>",
    "",
)

TYPES_SECTION_INDIVIDUAL: tuple[str, ...] = (
    "## Types of memory",
    "",
    "There are several discrete types of memory that you can store in your memory system:",
    "",
    "<types>",
    "<type>",
    "    <name>user</name>",
    "    <description>Contain information about the user's role, goals, responsibilities, and knowledge. Great user memories help you tailor your future behavior to the user's preferences and perspective. Your goal in reading and writing these memories is to build up an understanding of who the user is and how you can be most helpful to them specifically. Avoid writing memories about the user that could be viewed as a negative judgement or that are not relevant to the work you're trying to accomplish together.</description>",
    "    <when_to_save>When you learn any details about the user's role, preferences, responsibilities, or knowledge</when_to_save>",
    "    <how_to_use>When your work should be informed by the user's profile or perspective.</how_to_use>",
    "    <examples>",
    "    user: I'm a data scientist investigating what logging we have in place",
    "    assistant: [saves user memory: user is a data scientist, currently focused on observability/logging]",
    "",
    "    user: I've been writing Go for ten years but this is my first time touching the React side of this repo",
    "    assistant: [saves user memory: deep Go expertise, new to React and this project's frontend - frame frontend explanations in terms of backend analogues]",
    "    </examples>",
    "</type>",
    "<type>",
    "    <name>feedback</name>",
    "    <description>Guidance the user has given you about how to approach work - both what to avoid and what to keep doing. These are a very important type of memory to read and write as they allow you to remain coherent and responsive to the way you should approach work. Record from failure and success.</description>",
    "    <when_to_save>Any time the user corrects your approach or confirms a non-obvious approach worked. Corrections are easy to notice; confirmations are quieter - watch for them. Include why so you can judge edge cases later.</when_to_save>",
    "    <how_to_use>Let these memories guide your behavior so that the user does not need to offer the same guidance twice.</how_to_use>",
    "    <body_structure>Lead with the rule itself, then a **Why:** line and a **How to apply:** line.</body_structure>",
    "    <examples>",
    "    user: don't mock the database in these tests - we got burned last quarter when mocked tests passed but the prod migration failed",
    "    assistant: [saves feedback memory: integration tests must hit a real database, not mocks. Reason: prior incident where mock/prod divergence masked a broken migration]",
    "",
    "    user: stop summarizing what you just did at the end of every response, I can read the diff",
    "    assistant: [saves feedback memory: this user wants terse responses with no trailing summaries]",
    "    </examples>",
    "</type>",
    "<type>",
    "    <name>project</name>",
    "    <description>Information that you learn about ongoing work, goals, initiatives, bugs, or incidents within the project that is not otherwise derivable from the code or git history. Project memories help you understand the broader context and motivation behind the user's work in this working directory.</description>",
    "    <when_to_save>When you learn who is doing what, why, or by when. These states change relatively quickly so keep your understanding up to date. Convert relative dates to absolute dates when saving.</when_to_save>",
    "    <how_to_use>Use these memories to understand the details and nuance behind the user's request and make better informed suggestions.</how_to_use>",
    "    <body_structure>Lead with the fact or decision, then a **Why:** line and a **How to apply:** line.</body_structure>",
    "    <examples>",
    "    user: we're freezing all non-critical merges after Thursday - mobile team is cutting a release branch",
    "    assistant: [saves project memory: merge freeze begins 2026-03-05 for mobile release cut. Flag any non-critical PR work scheduled after that date]",
    "    </examples>",
    "</type>",
    "<type>",
    "    <name>reference</name>",
    "    <description>Stores pointers to where information can be found in external systems. These memories allow you to remember where to look to find up-to-date information outside of the project directory.</description>",
    "    <when_to_save>When you learn about resources in external systems and their purpose.</when_to_save>",
    "    <how_to_use>When the user references an external system or information that may be in an external system.</how_to_use>",
    "    <examples>",
    '    user: check the Linear project "INGEST" if you want context on these tickets, that is where we track all pipeline bugs',
    '    assistant: [saves reference memory: pipeline bugs are tracked in Linear project "INGEST"]',
    "    </examples>",
    "</type>",
    "</types>",
    "",
)

WHAT_NOT_TO_SAVE_SECTION: tuple[str, ...] = (
    "## What NOT to save in memory",
    "",
    "- Code patterns, conventions, architecture, file paths, or project structure - these can be derived by reading the current project state.",
    "- Git history, recent changes, or who-changed-what - `git log` / `git blame` are authoritative.",
    "- Debugging solutions or fix recipes - the fix is in the code; the commit message has the context.",
    "- Anything already documented in OPENSPACE.md files.",
    "- Ephemeral task details: in-progress work, temporary state, current conversation context.",
    "",
    "These exclusions apply even when the user explicitly asks you to save. If they ask you to save a PR list or activity summary, ask what was surprising or non-obvious about it - that is the part worth keeping.",
)

MEMORY_DRIFT_CAVEAT = (
    "- Memory records can become stale over time. Use memory as context for what "
    "was true at a given point in time. Before answering the user or building "
    "assumptions based solely on information in memory records, verify that the "
    "memory is still correct and up-to-date by reading the current state of the "
    "files or resources. If a recalled memory conflicts with current information, "
    "trust what you observe now - and update or remove the stale memory rather "
    "than acting on it."
)

WHEN_TO_ACCESS_SECTION: tuple[str, ...] = (
    "## When to access memories",
    "- When memories seem relevant, or the user references prior-conversation work.",
    "- You MUST access memory when the user explicitly asks you to check, recall, or remember.",
    "- If the user says to *ignore* or *not use* memory: proceed as if MEMORY.md were empty. Do not apply remembered facts, cite, compare against, or mention memory content.",
    MEMORY_DRIFT_CAVEAT,
)

TRUSTING_RECALL_SECTION: tuple[str, ...] = (
    "## Before recommending from memory",
    "",
    "A memory that names a specific function, file, or flag is a claim that it existed *when the memory was written*. It may have been renamed, removed, or never merged. Before recommending it:",
    "",
    "- If the memory names a file path: check the file exists.",
    "- If the memory names a function or flag: grep for it.",
    "- If the user is about to act on your recommendation (not just asking about history), verify first.",
    "",
    '"The memory says X exists" is not the same as "X exists now."',
    "",
    "A memory that summarizes repo state (activity logs, architecture snapshots) is frozen in time. If the user asks about *recent* or *current* state, prefer `git log` or reading the code over recalling the snapshot.",
)

MEMORY_FRONTMATTER_EXAMPLE: tuple[str, ...] = (
    "```markdown",
    "---",
    "name: {{memory name}}",
    "description: {{one-line description - used to decide relevance in future conversations, so be specific}}",
    "type: {{user, feedback, project, reference}}",
    "---",
    "",
    "{{memory content - for feedback/project types, structure as: rule/fact, then **Why:** and **How to apply:** lines}}",
    "```",
)
