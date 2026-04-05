# Security Policy

## Reporting Vulnerabilities

If you discover a security vulnerability, please report it responsibly by opening a private security advisory on this repository. Do **not** open a public issue.

## Security Considerations for Users

OpenSpace is a powerful agent framework that can execute shell commands, run arbitrary code, and connect to external services. Users should be aware of the following:

### Telemetry

Telemetry is **disabled by default** as of this PR. If you opt in by setting `MCP_USE_ANONYMIZED_TELEMETRY=true`, be aware that execution metadata (model names, tool usage counts, timing) is sent to PostHog and Scarf. Query text and response text are **never** transmitted regardless of this setting.

### Cloud Skills

Cloud skill search and auto-import are **disabled by default** (`search_scope="local"`). If you enable cloud search (`search_scope="all"`), downloaded skills are not sandboxed or signature-verified. Only enable this in trusted environments.

### Host Config Auto-Detection

OpenSpace reads host agent configs (`~/.openclaw/openclaw.json`, `~/.nanobot/config.json`) to auto-detect LLM credentials. It only reads from the explicitly scoped `openspace` env blocks — not top-level or unrelated configuration sections.

### Shell Execution

The grounding engine can execute shell commands. The `config_security.json` defines blocked command lists, but this is a denylist approach. For production deployments, enable sandboxing (`sandbox_enabled: true`) and review the security policy configuration.
