---
name: openspace
description: Set up or use the OpenSpace component of HKUDS AgentPilot for retrieving and retaining agent skills. Use when the user asks for OpenSpace skill management or its MCP connection; ordinary coding improvement uses agentpilot.
---

# OpenSpace

This entry installs a host skill, not the OpenSpace runtime or a cloud account. Reuse an existing AgentPilot checkout. Otherwise obtain https://github.com/HKUDS/AgentPilot in a separate directory, preserving the current project. Read `openspace/README.md`, starting at **Path A: For Your Agent**, and the relevant host integration instructions in `openspace/openspace/host_skills/README.md`.

Install only OpenSpace in its dedicated Python 3.12+ environment. Configure its MCP connection with that environment's absolute `openspace-mcp` executable and the host's documented configuration format, preserving unrelated settings. Use existing credentials through their supported configuration; do not copy or print tokens. Register the shipped host skills as described in the guide.

Verify the MCP server's actual tools are visible before saying the connection is ready. If the host cannot load MCP, explain the available CLI route rather than claiming a connection. The user must explicitly request any cloud upload or sharing; installation does not authorize uploading project files or traces.
