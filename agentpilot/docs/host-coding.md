# Use AgentPilot in your coding agent

Use `/agentpilot` (or your host’s spelling below) to prepare and run an RSI experiment. The current agent fills in missing task/eval inputs, then starts the existing Arena or core runner when its adapter and evaluation are ready. If a prerequisite is missing, it explains what is needed; it does not replace AgentPilot with its own editing loop.

## Install once

From the repository root:

```bash
python install.py --host codex
```

The installer creates a dedicated runtime in `~/.local/share/agentpilot/host-runtime`, installs the core package, checks it, and adds the `agentpilot` skill. With `uv` installed it selects Python 3.12; otherwise run the installer with Python 3.11+ and working `venv`/pip support. Runtime installation may download Python or build dependencies. It does not change model settings or credentials, install project dependencies, or start a model or benchmark run. Its output gives the exact Python launcher and skill path.

Use `--prefix /path/to/runtime` to choose a different runtime, and `--skill-dir /path/to/skills` to choose a host's skill root. Re-run the same command from an updated checkout to update. An update checks skill-file conflicts before replacing the runtime, preserves unrelated files, and refuses to overwrite a skill changed outside this installer. It never commits or pushes.

If you prefer uv's tool installation:

```bash
uv tool install --python 3.12 ./agentpilot
agentpilot setup --host codex
```

### Supported hosts

Use `python install.py --host NAME`. All hosts use the same skill; the installer selects the host's skill directory and invocation.

| Host | `NAME` | Default skill directory | Invocation |
| --- | --- | --- | --- |
| Codex | `codex` | `~/.agents/skills` | `$agentpilot` or `/skills`; optional desktop plugin below. |
| Claude Code | `claude-code` | `~/.claude/skills` | `/agentpilot` |
| OpenCode | `opencode` | `~/.config/opencode/skills` | CLI: `/agentpilot` (verified with 1.19.5); V2 also supports `@agentpilot`. |
| Cursor | `cursor` | `~/.cursor/skills` | `/agentpilot` |
| GitHub Copilot CLI | `copilot` | `~/.copilot/skills` | `/agentpilot` |
| Meta Muse Code | `muse` | `~/.agents/skills` | `/agentpilot` |
| CodeBuddy Code | `codebuddy` | `~/.codebuddy/skills` | `/agentpilot` |
| Hermes Agent | `hermes` | `~/.hermes/skills` | `/agentpilot` |
| nanobot | `nanobot` | Its configured `workspace/skills` | `$agentpilot` |
| DeepSeek Harness | `dsh` | `~/.agents/skills` | `/agentpilot` with skill support enabled. |
| Grok Build | `grok` | `~/.grok/skills` | `/agentpilot` |
| WorkBuddy | `workbuddy` | ZIP export | Import the emitted ZIP through Skills, then select it. |

OpenCode, Hermes and DSH respect `XDG_CONFIG_HOME`, `HERMES_HOME` and `DSH_AGENTS_HOME`, respectively. nanobot reads its default config, falling back to workspace environment settings; use `--skill-dir` for a separate instance. For another compatible host, use `--host generic --skill-dir /its/documented/skills/path`.

The host needs skill support, local file editing, and command execution. Having a CLI alone does not establish those capabilities. WorkBuddy ZIPs contain an absolute runtime launcher: export on the machine where the skill will run. WorkBuddy and CodeBuddy are distinct products; no WorkBuddy CLI or slash spelling is assumed. Grok Build is the local coding agent, not Grok Bot. Muse here means Meta Muse Code. Restart the host or open a new chat if a new skill is not visible.

The directory integrations follow [AutoSwarm's host setup](https://github.com/HKUDS/AutoSwarm/blob/main/src/autoswarm/setup.py) and the host documentation linked in [the registry](../src/agentpilot/host/hosts.py). Installing a skill lets the host prepare inputs and operate AgentPilot. The formal worker separately needs an adapter for task input, execution options and results; merely having a CLI is insufficient.

<a id="codex-plugin"></a>

### Optional Codex plugin and @ entry

The repository also contains a local Codex plugin. With a Codex CLI that supports plugins, install the runtime, skill and plugin together:

```bash
python install.py --host codex --plugin
```

`--plugin` registers this checkout as a local marketplace and installs its AgentPilot plugin in Codex. The equivalent plugin-only commands are:

```bash
codex plugin marketplace add .
codex plugin add agentpilot@agentpilot-local
```

Alternatively open the repository in Codex and install AgentPilot from the `agentpilot-local` source in Plugins. Start a new chat and select the installed plugin with `@`. Its onboarding skill locates or installs the core runtime and loads the same coding workflow. This is a local plugin with local execution; it has not been published to the public plugin directory. Plugin availability and `@` selection depend on the client; `setup --host codex` alone installs a skill, not a plugin. See the [official plugin guide](https://developers.openai.com/plugins/build/plugins).

### Other components

```bash
python install.py --host claude-code --components agentpilot openspace rsi-arena
```

This adds `/agentpilot`, `/openspace`, and `/rsi-arena` in Claude Code (the names use `$` in Codex CLI). The last two are guided entrypoints to their existing setup flows: OpenSpace still needs its runtime and MCP connection, and Arena still needs the selected benchmark's dependencies, tasks, and model setup. They are installed only when requested and are not included in ordinary coding runs. Colon namespaces belong to a host's plugin system, not these skill names.

## Start in a project

```text
$agentpilot Help me improve this project's performance and make it run faster.
```

A bare invocation also works. The agent reads the project, infers a useful goal where possible and prepares suitable checks. It runs an initial measurement when an evaluator is available. For a performance task, this includes a representative workload and correctness checks. It asks only for essential information it cannot infer. Preparation uses the host's ordinary tools and needs no separate session or JSON plan.

The agent assesses readiness from the actual configuration and evaluation results. With a compatible adapter, working evaluation, model/worker access and budget, it continues into **proposer → worker → eval → policy feedback** through the existing runner. The runner owns candidate changes, run records and recovery. Ordinary project tests still need an environment adapter to enter this loop.

## Datasets and evaluation

**Already configured:** give the agent the runtime configuration or existing run directory. It follows the [Arena integration](../integrations/arena/README.md), [core environment](environment-adapters.md), or [Arena benchmark guide](../../arena/README.md) appropriate to that configuration. The formal UCB/Textual Policy Learning loop and its persisted state remain unchanged.

**Missing task or eval:** the current agent inspects the project, prepares the missing inputs and checks the actual evaluator. It can use the [existing preparation interfaces](../integrations/arena/docs/preparation.md) without dispatching another worker. It then launches the configured runner if ready; otherwise it reports the missing prerequisite. Automatic formal integration of arbitrary projects is not yet supported. The built-in Arena setup targets Harbor/Terminus 2 on AIME or Terminal-Bench; other projects need an implemented [environment adapter](environment-adapters.md).

**Custom eval:** give the command and success criterion, for example: `Use python evaluation/check.py; preserve output quality while reducing runtime.` The agent verifies that it measures the goal and detects relevant failures. Formal runs also require the corresponding task source, worker and environment adapter. A passing test command or static configuration check alone is insufficient.

**Model access:** the Arena research runner can reuse a configured worker CLI's model/login for proposer and Memory policy calls when their API credentials are absent. Use `--proposer-access worker --policy-access worker` to select this explicitly. Task evaluation and UCB embeddings retain their provider requirements; custom core adapters own their own provider assembly. CLI calls retain budget allowances with external billing, so login reuse does not make the entire loop free. See [worker access](../integrations/arena/docs/workers.md#reuse-the-worker-for-proposer-and-policy-calls).

**A/B/C datasets:** use the selected formal environment's protocol. A provides development evidence, B supplies validation feedback, and C is held out until final evaluation. Do not use C to guide subsequent improvement. Discovering project tests does not automatically create these splits.

## Inspect and resume

The installer returns an absolute launcher using its own Python. It accepts core commands such as `validate-config`, `run` and `resume`; use the environment where the selected adapter and provider dependencies are actually installed. Configuration validation alone does not prove that an evaluator runs.

An optional read-only helper suggests existing pytest, unittest, Node, Cargo and Go test commands:

```bash
agentpilot project inspect --cwd /path/to/project
```

It neither executes checks nor creates a session. The agent can also discover and run tests directly with the project's usual tools.

Continue preparation from the conversation and project artifacts. Resume experiments using their original runner and run identity. There is no separate host improvement loop. Re-running installation updates managed skills while protecting local edits.

The [installed skill](../src/agentpilot/host/skills/agentpilot/SKILL.md) contains the routing rules; [formal workflow details](../src/agentpilot/host/skills/agentpilot/references/formal.md) are loaded only when relevant. The agent chooses how to prepare incomplete inputs; the configured runner owns policy learning and evaluation. Commit restrictions still apply: a runner that creates candidate commits cannot be used under a blanket no-commit instruction.
