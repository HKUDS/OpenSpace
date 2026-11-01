# Connect a Worker

The worker runs your coding harness to edit code and conduct experiments. Save one configuration below as `runs/my-agent/worker.toml`:

| Mode | Model and login |
| --- | --- |
| `native` | Use the harness's existing configuration. |
| `provider` | Supply the model and provider for this run. |

After saving the file, continue to [② Program and tasks](../README.md#program). Setup reads this file when generating the runtime configuration.

The same configuration can run an optional [preparation task](preparation.md) to find a missing initial program or evaluation before setup. Its separate prompt includes the Arena measurement interfaces and the required handoff format.

<a id="native"></a>

## Use your harness's model and login

Install and sign in to the CLI, then set its executable path:

```toml
[worker]
backend = "codex"
mode = "native"
command = ["/absolute/path/to/codex"]
runtime_root = "/absolute/path/to/worker-runtime"
sandbox_backend = "host"
reservation_usd = 2.0
```

`runtime_root` stores temporary files outside your repository. Hermes additionally creates and removes a temporary profile beside its login profile. `host` uses the harness's own execution permissions. `reservation_usd` allocates a budget allowance per task.

Presets: `codex`, `claude_code`, `hermes`, `nanobot`, `codebuddy`, and `dsh`. Configure the CLI's permissions for unattended execution. For a custom config directory, add its variable, such as `CODEX_HOME`, to `env_allowlist`; add credential variables to `secret_env_names`.

<a id="provider"></a>

## Use a provider such as OpenRouter

Change the same worker table to `mode = "provider"`, then add:

```toml
[worker.provider]
name = "openrouter"
model = "vendor/model-id"
api_key_env = "OPENROUTER_API_KEY"
```

Supply `OPENROUTER_API_KEY` through your environment or the repository-root `.env`. Setup adds the key name to the generated configuration; the OpenRouter endpoint is filled in automatically.

For another compatible service, set `name` and `base_url`. Codex uses Responses, Claude Code uses Messages, and nanobot/CodeBuddy use compatible model endpoints. The Hermes preset supports OpenRouter.

<a id="custom-command"></a>

## Other harnesses (custom commands)

Use this when your harness has no built-in preset, such as a company-internal coding agent. Keep the `native` or `provider` mode selected above, and replace `backend` and `command` with:

```toml
backend = "command"
command = ["/absolute/path/to/my-worker", "--task-file", "{prompt_file}", "--cwd", "{workspace}", "--session-id", "{session_id}", "--state-dir", "{session_root}"]
```

Use your harness's actual noninteractive command and flags. This example passes a task file, working directory, fresh session ID and private state directory. The wrapper must start with empty history and put all conversation state and automatic memory in that directory. It must keep using its existing login/model configuration separately. Both session placeholders are required; an opaque command without an isolation binding is rejected.

If your wrapper takes session settings through environment variables, bind both instead:

```toml
[worker.session_env]
MY_SESSION_ID = "{session_id}"
MY_SESSION_DIRECTORY = "{session_root}"
```

The wrapper must actually consume these variables. AgentPilot can enforce fresh values and cleanup, but cannot enforce an arbitrary external program's internal behavior.

The task is sent on stdin by default. Use `{prompt}` or `{prompt_file}` when your CLI expects an argument. The harness must leave its code changes in `{workspace}` before exiting.

For provider mode, map all three values to variables your wrapper reads:

```toml
[worker.provider_env]
MY_MODEL = "{model}"
MY_BASE_URL = "{base_url}"
MY_API_KEY = "{api_key}"
```

DSH provider overrides also use a wrapper/profile that consumes these variables. Other presets configure the provider automatically.

## Session isolation

Proposer/policy calls and execution workers reuse authentication and model settings, with independent CLI conversations. Every launch, including retries and concurrent tasks, receives fresh session state. Known resume/continue/fork options and fixed session IDs are rejected, and inherited parent-session environment variables are removed. Built-in commands cannot contain an option terminator (`--`) before the runtime adds its isolation flags. Task prompts, repository files and evidence explicitly supplied by AgentPilot remain available.

| Backend | Isolation |
| --- | --- |
| Codex | `exec --ephemeral`, no prompt history, automatic memories disabled. |
| Claude Code / CodeBuddy | Fresh UUID, `--no-session-persistence`, automatic memories disabled. |
| Hermes | Disposable profile with its own history/memory store, memory providers disabled, native credential-pool fallback to the existing login. OAuth grants are not copied. A private `.env` projection removes session selectors and preserves explicit provider overrides without modifying the original. Select a source profile with allowlisted `HERMES_HOME`, rather than `--profile`. |
| nanobot | Fresh `--session`, private config-relative session store, explicit task workspace instead of the login's default workspace/memory. Proposer workspaces are empty; execution workspaces contain the task checkout. |
| DSH | One-shot `headless` profile and a final overlay isolating session, search and attachment storage. Login/model settings remain in the original DSH home. |
| Custom command | Explicit `{session_id}` and `{session_root}` bindings consumed by the wrapper. |

Use CLI versions supporting the listed flags and storage behavior (Hermes needs shared profile credential fallback; nanobot needs config-relative session storage). An unsupported CLI fails the call; the runtime does not retry it with isolation disabled. Temporary state is cleaned up after normal completion, launch failure and timeout. `host` is still the configured execution policy; session isolation does not restrict filesystem access.

## Reuse the worker for proposer and policy calls

The Arena **research** command defaults to `--proposer-access auto` and `--policy-access auto`. Each role uses its configured API credential when present; when that credential is absent, it invokes the configured worker CLI with the worker's model and login. This supports all seven backends above, in both `native` and `provider` mode. Authentication failures from an API do not trigger a switch to worker access.

To use the worker even when an API key is available, add these flags to the research command:

```bash
--proposer-access worker --policy-access worker
```

`--policy-access` controls Textual Policy Learning's ranker and summarizer. Use `api` instead of `auto` to require an API and fail immediately when its key is missing. The API key name comes from `environment.settings.provider_runtime.providers`; older OpenRouter configurations without a binding use the solver's configured key name.

Worker access preserves the worker's model/provider and generation settings. Set those in the worker CLI or `worker.toml`; do not pass a different `--model` or an API endpoint allowlist for these calls. Each call uses a fresh temporary directory and isolated CLI session, receives the conversation and tool definitions, and returns JSON. Proposer's multi-turn history is supplied explicitly by AgentPilot; execution-worker conversation history is never attached. AgentPilot validates and executes the requested proposer tools. Saved replies are replayed after interruption; a dispatched call without a receipt is not repeated. Keep the same access mode and worker profile when resuming a run.

Custom commands can write their JSON reply to the path in `AGENTPILOT_COMPLETION_OUTPUT` or return it on stdout. The supplied prompt describes the output contract. Built-in CLIs can also save `agentpilot-response.json` in their temporary workspace. No login tokens are copied into the runtime TOML or prompts.

CLI billing remains external: each completed call retains its budget reservation, with actual dollars unknown rather than zero. CLI token usage is recorded when reported. API temperature/output-token settings do not override the worker CLI's generation settings; timeout and response-byte limits still apply.

The benchmark task solver and UCB embedding model retain their own provider requirements. In particular, a full Arena run still needs the task-solver API; `--proposer-only` can inspect existing evidence using just worker login. Legacy single-action campaigns and custom core adapters keep their existing provider assembly.
