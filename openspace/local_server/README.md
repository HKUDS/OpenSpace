# OpenSpace Local Server

The local server is a **lightweight Flask service** that runs on the host machine and exposes HTTP endpoints for shell execution and GUI automation. It is only needed in **server mode** — most users should use the default **local mode** instead.

## When to Use Server Mode

| | Local Mode (default) | Server Mode |
|---|---|---|
| **Setup** | Zero — just run OpenSpace | Start `local_server` first |
| **Use case** | Same-machine development | Remote VMs, sandboxing, multi-machine |
| **Shell** | `asyncio.subprocess` in-process | HTTP → Flask → `subprocess` |
| **GUI** | Direct pyautogui | HTTP → Flask → pyautogui |
| **Network** | None required | HTTP between agent ↔ server |

Use server mode when:
- **Controlling a remote VM** — the agent runs on your host, the server runs inside the VM
- **Process isolation / sandboxing** — script execution in a separate process
- **Multi-machine deployments** — agent and execution environment on different machines

## Enable Server Mode

Set `"mode": "server"` in `openspace/config/config_grounding.json`:

```jsonc
{
  "shell": { "mode": "server", ... },  // default: "local"
  "gui":   { "mode": "server", ... }   // default: "local"
}
```

## Platform-Specific Dependencies

> [!IMPORTANT]
> Install platform-specific dependencies **on the machine running the server** (not the agent).

<details>
<summary><b>macOS</b></summary>

```bash
pip install pyobjc-core pyobjc-framework-cocoa pyobjc-framework-quartz
```

> **Note:** `atomacos` is excluded because it requires `pyautogui<0.9.42` (appears unmaintained). Accessibility-tree features are currently unavailable.

**Permissions required** (macOS will prompt automatically on first run):
- **Accessibility** (for GUI control)
- **Screen Recording** (for screenshots and video capture)

> If prompts don't appear, grant manually in System Settings → Privacy & Security.

</details>

<details>
<summary><b>Linux</b></summary>

```bash
pip install python-xlib pyatspi numpy
sudo apt install at-spi2-core python3-tk scrot
```

> **Optional:** `wmctrl` (window management), `libx11-dev` + `libxfixes-dev` (cursor in screenshots)

</details>

<details>
<summary><b>Windows</b></summary>

```bash
pip install pywinauto pywin32 PyGetWindow
```

</details>

## Launch

```bash
# Python entry point
python -m openspace.local_server.main --host 127.0.0.1 --port 5000

# Or via helper script
./openspace/local_server/run.sh
```

Press `Ctrl+C` to stop.

## Configuration

Runtime options in `openspace/local_server/config.json`:

```json
{
  "server": {
    "host": "127.0.0.1",
    "port": 5000,
    "debug": false
  }
}
```

## Architecture

- **PlatformAdapter** — abstracts OS-specific primitives (Windows, macOS, Linux)
- **Accessibility Helper** — queries the UI accessibility tree
- **Screenshot Helper** — captures full or partial screenshots (PNG)
- **Recorder** — streams screen recordings for analysis
- **Health / Feature Checker** — validates runtime capabilities and permissions

## REST Endpoints

| Path | Method | Description |
|------|--------|-------------|
| `/` | GET | Liveness probe |
| `/platform` | GET | Host OS metadata |
| `/execute` | POST | Execute a PyAutoGUI script fragment |
| `/execute_with_verification` | POST | Execute + verify via template matching |
| `/run_python` | POST | Run Python in sandbox |
| `/run_bash_script` | POST | Run shell script (optional conda activation) |
| `/screenshot` | GET | PNG screenshot (full or ROI) |
| `/cursor_position` | GET | Current mouse coordinates |
| `/screen_size` | GET/POST | Query or set virtual screen resolution |
| `/list_directory` | POST | List directory contents |

See `main.py` for ~20 additional endpoints.
