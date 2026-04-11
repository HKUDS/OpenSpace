# Release Note: Local Split-Routing Customization

This note explains what the local customization changed, why it was needed, and why embeddings looked "broken first, then fixed later".

## Summary

The local customization solved a routing mismatch between:

- the main OpenSpace LLM path
- the skill-router embedding path
- the Codex Desktop sidecar path

The important result is:

- OpenSpace main inference can continue using the local OpenAI-compatible gateway and `gpt-5.4`
- skill-router embeddings no longer depend on that gateway exposing `/v1/embeddings`
- Codex Desktop sidecar evolution stays isolated from the user's main Desktop session

## What Was Wrong Before

The main provider path was already usable for normal OpenSpace LLM calls:

- `/v1/chat/completions`
- `/v1/responses`-compatible workflows

But the skill router has a separate semantic re-rank stage that tries to call:

- `POST /v1/embeddings`

The current local gateway worked for the main LLM path, but did not provide a working embeddings endpoint for this use case.

So the real state before the fix was:

- main OpenSpace task execution: usable
- skill-router semantic embeddings: degraded or unavailable
- Desktop sidecar: usable for LLM tasks, but still inherited the embedding weakness

## Why It Looked Like "Embeddings Started Working"

Embeddings did **not** start working on the same remote provider endpoint.

What changed was the routing.

Originally:

- main LLM and skill embeddings were effectively expected to succeed through the same OpenAI-compatible path

After the fix:

- main LLM stayed on the provider path
- skill embeddings were routed to a different backend

For the current local setup, that different backend is:

- `fastembed`
- model: `BAAI/bge-small-en-v1.5`

So the correct explanation is:

- the remote embeddings path was not fixed
- the system was changed so it no longer needed that remote embeddings path

## What Was Implemented

### 1. Split routing for skill embeddings

Skill embedding generation now supports:

- `OPENSPACE_SKILL_EMBEDDING_BACKEND=local`
- `OPENSPACE_SKILL_EMBEDDING_BACKEND=remote`
- `OPENSPACE_SKILL_EMBEDDING_BACKEND=auto`

This was implemented in:

- `openspace/cloud/embedding.py`
- `openspace/skill_engine/skill_ranker.py`

Effect:

- the main LLM provider and the skill embedding backend are now decoupled

### 2. Local embedding support

Added `fastembed` as a project dependency and enabled local skill embeddings.

Effect:

- the skill router can still do semantic vector re-rank even when the LLM provider does not expose `/v1/embeddings`

### 3. Launcher propagation

Updated launchers so the new embedding settings are passed through consistently:

- `scripts/openspace.sh`
- `scripts/codex-openspace`
- `scripts/codex-desktop-evolution`

Effect:

- CLI, isolated Codex profile, and Desktop sidecar all use the same split-routing model

### 4. Sidecar isolation kept intact

The Desktop sidecar still runs in an isolated overlay profile and does not overwrite the main Codex Desktop environment.

Effect:

- the user can keep their normal Desktop workflow
- OpenSpace evolution still uses the provider-backed sidecar path
- embedding config does not leak back into the main Desktop session

## Current Effective Routing

### Main LLM path

- model: `gpt-5.4`
- API base: `http://127.0.0.1:8080/v1`
- source: `OPENSPACE_LLM_*`

### Skill-router embedding path

- backend: `local`
- model: `BAAI/bge-small-en-v1.5`
- source: `OPENSPACE_SKILL_EMBEDDING_*`

### Desktop sidecar

- still isolated through `scripts/codex-desktop-evolution`
- uses the same split routing internally

## Practical Result

This local customization solved three concrete problems:

1. OpenSpace no longer treats "main LLM works" and "embeddings work" as the same thing.
2. Skill routing quality is preserved even when the LLM provider has no usable `/v1/embeddings`.
3. Codex Desktop sidecar evolution keeps using provider tokens without polluting the main Desktop environment.

## Short Version

Before:

- one path was effectively assumed to do both LLM and embeddings
- the provider could handle the LLM part
- the embedding part was weak or unavailable

Now:

- LLM still goes through the provider
- embeddings go through local fastembed
- sidecar remains isolated

That is why it first looked broken and later looked fixed.

It was not a provider repair.
It was a routing repair.
