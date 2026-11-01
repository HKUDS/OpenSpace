# Prepare Terminal-Bench 2.1 Images

Complete [Arena installation](../../README.md#quick-start-arena), then run these commands from `arena/` with Docker available.

## ① Check out the tasks

Use the revision pinned by the supplied Terminal-Bench configurations. For an existing checkout, verify its revision and clean state instead of cloning again:

```bash
git clone https://github.com/harbor-framework/terminal-bench-2-1.git
git -C terminal-bench-2-1 checkout 5c8eadf1f393183288fa08b8f73ca9a469cc5e00
```

## ② Prepare the images

The first call downloads and locks dependencies, then builds and verifies the images without network access. Subsequent calls reuse the cache or rebuild missing images:

```bash
uv run --no-sync python -m rsi_arena.benchmarks.tb21.closure_builder prepare \
  --task-repository terminal-bench-2-1 \
  --cache-dir tb21-closure-cache \
  --temp-dir tb21-closure-cache/tmp \
  --verifier-timeout-multiplier 2.0 \
  --docker-host unix:///var/run/docker.sock
```

This produces **`tb21-closure-cache/build/offline-verifier-authority.json`**. Keep the full cache, which includes base-image archives, packages and task assets. All 89 tasks can require substantial disk space. Use the same Docker socket for preparation and evaluation; a changed task checkout requires a new cache.

## ③ Use the result

When editing the Terminal-Bench experiment TOML, set these existing fields:

```toml
[benchmarks.config.backend]
task_repository = "terminal-bench-2-1"
offline_verifier_authority_path = "tb21-closure-cache/build/offline-verifier-authority.json"
docker_host = "unix:///var/run/docker.sock"
```

Keep `verifier_settings.timeout_multiplier = 2.0` consistent with preparation; it scales each task's own verifier deadline.

Continue with [Harbor and the initial candidate](../README.md#prepare-harbor-and-the-initial-candidate), then configure and launch the run. To move the prepared images to another Docker daemon, use the optional [image bundle guide](tb21-exact-image-bundle.md).

<details>
<summary>Optional: inspect the individual build phases</summary>

For a small one-task preparation, run the phases separately:

```bash
uv run --no-sync python -m rsi_arena.benchmarks.tb21.closure_builder lock \
  --task-repository terminal-bench-2-1 \
  --task count-dataset-tokens \
  --output-dir closure-inputs

uv run --no-sync python -m rsi_arena.benchmarks.tb21.closure_builder build \
  --lock closure-inputs/closure-lock.json \
  --docker-host unix:///var/run/docker.sock \
  --output-dir closure-build

uv run --no-sync python -m rsi_arena.benchmarks.tb21.closure_builder verify \
  --authority closure-build/offline-verifier-authority.json \
  --verifier-timeout-multiplier 2.0 \
  --docker-host unix:///var/run/docker.sock
```

This alternative writes `closure-build/offline-verifier-authority.json`; it is a different output path from `prepare`. A one-task authority cannot run the supplied full benchmark configs. Omit `--task` to include all tasks.

Only `lock` downloads dependencies. Keep its complete directory with the lock file. `build` and `verify` run offline against those retained inputs. Repeated `--task` or `--family` selectors support smaller preparations; changing a prepared cache's source or task selection is rejected.

</details>
