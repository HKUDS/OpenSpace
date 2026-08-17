import asyncio
import argparse
import sys
import logging
from typing import Optional

from openspace.core.tui_bridge import TUIBridge
from openspace import OpenSpace, OpenSpaceConfig
from openspace.utils.logging import Logger
from openspace.utils.ui import create_ui, OpenSpaceUI
from openspace.utils.ui_integration import UIIntegration
from openspace.utils.cli_display import CLIDisplay

logger = Logger.get_logger(__name__)

from openspace.entrypoints.cli.text_loop import UIManager, interactive_mode, single_query_mode
from openspace.entrypoints.tui.controller import (
    _restore_console_logs,
    _suppress_console_logs_for_tui,
    tui_mode,
)



def _create_argument_parser() -> argparse.ArgumentParser:
    """Create command-line argument parser"""
    parser = argparse.ArgumentParser(
        description='OpenSpace - Self-Evolving Skill Worker & Community',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    
    # Subcommands
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # refresh-cache subcommand
    cache_parser = subparsers.add_parser(
        'refresh-cache',
        help='Refresh MCP tool cache (starts all servers once)'
    )
    cache_parser.add_argument(
        '--config', '-c', type=str,
        help='MCP configuration file path'
    )

    # Basic arguments (for run mode)
    parser.add_argument('--config', '-c', type=str, help='Configuration file path (JSON format)')
    parser.add_argument('--query', '-q', type=str, help='Single query mode: execute query directly')
    
    # LLM arguments
    parser.add_argument('--model', '-m', type=str, help='LLM model name')
    
    # Logging arguments
    parser.add_argument('--log-level', type=str, choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'], help='Log level')
    
    # Execution arguments
    parser.add_argument('--max-iterations', type=int, help='Maximum iteration count')
    parser.add_argument('--timeout', type=float, help='LLM API call timeout (seconds)')
    
    # UI arguments
    parser.add_argument('--interactive', '-i', action='store_true', help='Force text interactive mode')
    parser.add_argument('--tui', action='store_true', help='Use the TS TUI instead of the text input loop')
    parser.add_argument('--no-tui', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--resume', action='store_true', help='Start the TUI on the resume screen')
    parser.add_argument('--doctor', action='store_true', help='Start the TUI on the doctor screen')
    parser.add_argument('--no-ui', action='store_true', help='Disable visualization UI')
    parser.add_argument('--ui-compact', action='store_true', help='Use compact UI layout')
    
    return parser


async def refresh_mcp_cache(config_path: Optional[str] = None):
    """Refresh MCP tool cache by starting servers one by one and saving tool metadata."""
    from openspace.grounding.backends.mcp import MCPProvider, get_tool_cache
    from openspace.grounding.core.types import SessionConfig, BackendType
    from openspace.config import load_config, get_config
    
    print("Refreshing MCP tool cache...")
    print("Servers will be started one by one (start -> get tools -> close).")
    print()
    
    # Load config
    if config_path:
        config = load_config(config_path)
    else:
        config = get_config()
    
    # Get MCP config
    mcp_config = getattr(config, 'mcp', None) or {}
    if hasattr(mcp_config, 'model_dump'):
        mcp_config = mcp_config.model_dump()
    
    # Skip dependency checks for refresh-cache (servers are pre-validated)
    mcp_config["check_dependencies"] = False
    
    # Create provider
    provider = MCPProvider(config=mcp_config)
    await provider.initialize()
    
    servers = provider.list_servers()
    total = len(servers)
    print(f"Found {total} MCP servers configured")
    print()
    
    cache = get_tool_cache()
    cache.set_server_order(servers)  # Preserve config order when saving
    total_tools = 0
    success_count = 0
    skipped_count = 0
    failed_servers = []
    
    # Load existing cache to skip already processed servers
    existing_cache = cache.get_all_tools()
    
    # Timeout for each server (in seconds)
    SERVER_TIMEOUT = 60
    
    # Process servers one by one
    for i, server_name in enumerate(servers, 1):
        # Skip if already cached (resume support)
        if server_name in existing_cache:
            cached_tools = existing_cache[server_name]
            total_tools += len(cached_tools)
            skipped_count += 1
            print(f"[{i}/{total}] {server_name}... ⏭ cached ({len(cached_tools)} tools)")
            continue
        
        print(f"[{i}/{total}] {server_name}...", end=" ", flush=True)
        session_id = f"mcp-{server_name}"
        
        try:
            # Create session and get tools with timeout protection
            async with asyncio.timeout(SERVER_TIMEOUT):
                # Create session for this server
                cfg = SessionConfig(
                    session_name=session_id,
                    backend_type=BackendType.MCP,
                    connection_params={"server": server_name},
                )
                session = await provider.create_session(cfg)
                
                # Get tools from this server
                tools = await session.list_tools()
            
            # Convert to metadata format
            tool_metadata = []
            for tool in tools:
                tool_metadata.append({
                    "name": tool.schema.name,
                    "description": tool.schema.description or "",
                    "parameters": tool.schema.parameters or {},
                })
            
            # Save to cache (incremental)
            cache.save_server(server_name, tool_metadata)
            
            # Close session immediately to free resources
            await provider.close_session(session_id)
            
            total_tools += len(tools)
            success_count += 1
            print(f"✓ {len(tools)} tools")
        
        except asyncio.TimeoutError:
            error_msg = f"Timeout after {SERVER_TIMEOUT}s"
            failed_servers.append((server_name, error_msg))
            print(f"✗ {error_msg}")
            
            # Save failed server info to cache
            cache.save_failed_server(server_name, error_msg)
            
            # Try to close session if it was created
            try:
                await provider.close_session(session_id)
            except Exception:
                pass
            
        except Exception as e:
            error_msg = str(e)
            failed_servers.append((server_name, error_msg))
            print(f"✗ {error_msg[:50]}")
            
            # Save failed server info to cache
            cache.save_failed_server(server_name, error_msg)
            
            # Try to close session if it was created
            try:
                await provider.close_session(session_id)
            except Exception:
                pass
    
    print()
    print(f"{'='*50}")
    print(f"✓ Collected {total_tools} tools from {success_count + skipped_count}/{total} servers")
    if skipped_count > 0:
        print(f"  (skipped {skipped_count} cached, processed {success_count} new)")
    print(f"✓ Cache saved to: {cache.cache_path}")
    
    if failed_servers:
        print(f"✗ Failed servers ({len(failed_servers)}):")
        for name, err in failed_servers[:10]:
            print(f"  - {name}: {err[:60]}")
        if len(failed_servers) > 10:
            print(f"  ... and {len(failed_servers) - 10} more (see cache file for details)")
    
    print()
    print("Done! Future list_tools() calls will use cache (no server startup).")


def _load_config(args, *, quiet: bool = False) -> OpenSpaceConfig:
    """Load configuration"""
    import os
    from openspace.host_detection import (
        build_grounding_config_path,
        build_llm_kwargs,
        load_runtime_env,
    )

    load_runtime_env()

    cli_overrides = {}
    if args.max_iterations is not None:
        cli_overrides['grounding_max_iterations'] = args.max_iterations
    if args.timeout is not None:
        cli_overrides['llm_timeout'] = args.timeout
    if args.log_level:
        cli_overrides['log_level'] = args.log_level
    if quiet:
        cli_overrides['log_to_console'] = False
        cli_overrides['log_to_file'] = True

    try:
        from openspace.services.runtime_support.settings import get_setting

        settings_model = get_setting("model", None, cwd=os.getcwd())
        settings_thinking = get_setting("alwaysThinkingEnabled", None, cwd=os.getcwd())
    except Exception:
        settings_model = None
        settings_thinking = None

    # Resolve LLM model & credentials
    #   CLI --model  >  OPENSPACE_MODEL env  > settings model > host-agent auto-detect > default
    env_model = args.model or os.environ.get("OPENSPACE_MODEL", "") or (settings_model or "")
    model, llm_kwargs = build_llm_kwargs(env_model)
    cli_overrides['llm_model'] = model
    cli_overrides['llm_kwargs'] = llm_kwargs
    if isinstance(settings_thinking, bool):
        cli_overrides["llm_enable_thinking"] = settings_thinking

    max_iter = int(os.environ.get("OPENSPACE_MAX_ITERATIONS", "20"))
    enable_rec = os.environ.get("OPENSPACE_ENABLE_RECORDING", "true").lower() in ("true", "1", "yes")
    backend_scope_raw = os.environ.get("OPENSPACE_BACKEND_SCOPE")
    backend_scope = (
        [b.strip() for b in backend_scope_raw.split(",") if b.strip()]
        if backend_scope_raw else None
    )
    config_path = build_grounding_config_path()

    if 'grounding_max_iterations' not in cli_overrides:
        cli_overrides['grounding_max_iterations'] = max_iter
    cli_overrides['enable_recording'] = enable_rec
    if backend_scope is not None:
        cli_overrides['backend_scope'] = backend_scope
    if config_path:
        cli_overrides['grounding_config_path'] = config_path

    try:
        # Load from config file if provided
        if args.config:
            import json
            with open(args.config, 'r', encoding='utf-8') as f:
                config_dict = json.load(f)
            
            # Apply CLI / env overrides
            config_dict.update(cli_overrides)
            config = OpenSpaceConfig(**config_dict)
            
            if not quiet:
                print(f"✓ Loaded from config file: {args.config}")
        else:
            config = OpenSpaceConfig(**cli_overrides)
            if not quiet:
                print("✓ Using default configuration")
        
        if args.model and not quiet:
            print(f"✓ CLI overrides: llm_model")
        
        if args.log_level:
            Logger.set_level(args.log_level)
        
        return config
        
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")
        sys.exit(1)


def _setup_ui(args) -> tuple[Optional[OpenSpaceUI], Optional[UIIntegration]]:
    if args.no_ui:
        CLIDisplay.print_banner()
        return None, None
    
    ui = create_ui(enable_live=True, compact=args.ui_compact)
    ui.print_banner()
    ui_integration = UIIntegration(ui)
    return ui, ui_integration


async def _initialize_openspace(
    config: OpenSpaceConfig,
    args,
    *,
    quiet: bool = False,
) -> OpenSpace:
    openspace = OpenSpace(config)
    
    init_steps = [("Initializing OpenSpace...", "loading")]
    if not quiet:
        CLIDisplay.print_initialization_progress(init_steps, show_header=False)
    
    if not args.config:
        original_log_level = Logger.get_logger("openspace").level
        for log_name in ["openspace", "openspace.grounding", "openspace.agents"]:
            Logger.get_logger(log_name).setLevel(logging.WARNING)
    
    await openspace.initialize()
    
    # Restore log level
    if not args.config:
        for log_name in ["openspace", "openspace.grounding", "openspace.agents"]:
            Logger.get_logger(log_name).setLevel(original_log_level)
    
    # Print initialization results
    backends = openspace.list_backends()
    init_steps = [
        ("LLM Client", "ok"),
        (f"Grounding Backends ({len(backends)} available)", "ok"),
        ("Grounding Agent", "ok"),
    ]
    
    if config.enable_recording:
        init_steps.append(("Recording Manager", "ok"))
    
    if not quiet:
        CLIDisplay.print_initialization_progress(init_steps, show_header=True)
    
    return openspace


def _should_use_tui(args) -> bool:
    return bool(args.tui or args.resume or args.doctor) and not args.query


def _resolve_use_tui(args) -> bool:
    if not _should_use_tui(args):
        return False

    if TUIBridge.default_tui_available():
        if TUIBridge.interactive_terminal_available():
            return True

        message = (
            "The TS TUI needs an interactive terminal because stdin/stdout are "
            "reserved for Core/TUI IPC, but no usable terminal device was found."
        )
        raise RuntimeError(
            f"{message} Run without --tui for the text input loop, or use "
            "--query for non-interactive execution."
        )

    searched = "\n".join(
        f"  - {path}" for path in TUIBridge.default_tui_entry_candidates()
    )
    raise FileNotFoundError(
        "The TS TUI was requested, but no TUI entry was found.\n"
        "Searched default locations:\n"
        f"{searched}\n{TUIBridge.default_tui_missing_hint()}"
    )


async def main():
    parser = _create_argument_parser()
    args = parser.parse_args()
    tui_options = bool(args.tui or args.resume or args.doctor)
    if getattr(args, "no_tui", False) and args.tui:
        parser.error("--no-tui cannot be combined with --tui")
    if args.interactive and tui_options:
        parser.error("--interactive cannot be combined with --tui, --resume, or --doctor")
    if args.query and tui_options:
        parser.error("--query cannot be combined with --tui, --resume, or --doctor")
    
    # Handle subcommands
    if args.command == 'refresh-cache':
        await refresh_mcp_cache(args.config)
        return 0
    
    prefer_tui = _should_use_tui(args)
    startup_console_log_handlers = (
        _suppress_console_logs_for_tui() if prefer_tui else []
    )

    # Load configuration
    try:
        config = _load_config(args, quiet=prefer_tui)

        try:
            use_tui = _resolve_use_tui(args)
        except (FileNotFoundError, RuntimeError) as e:
            _restore_console_logs(startup_console_log_handlers)
            startup_console_log_handlers = []
            print(f"\nError: {e}")
            return 1
    except Exception:
        _restore_console_logs(startup_console_log_handlers)
        startup_console_log_handlers = []
        raise

    if not use_tui and startup_console_log_handlers:
        _restore_console_logs(startup_console_log_handlers)
        startup_console_log_handlers = []

    ui = None
    ui_integration = None
    if not use_tui:
        ui, ui_integration = _setup_ui(args)
        CLIDisplay.print_configuration(config)
    
    openspace = None
    
    try:
        # Initialize OpenSpace
        openspace = await _initialize_openspace(config, args, quiet=use_tui)
        
        # Connect UI (if enabled)
        if ui_integration:
            ui_integration.attach_llm_client(openspace.get_llm_client())
            ui_integration.attach_grounding_client(openspace.get_grounding_client())
            CLIDisplay.print_system_ready()
        
        ui_manager = UIManager(ui, ui_integration)
        
        # Run appropriate mode
        if args.query:
            await single_query_mode(openspace, args.query, ui_manager)
        elif use_tui:
            await tui_mode(
                openspace,
                args,
                console_log_handlers=startup_console_log_handlers,
            )
            startup_console_log_handlers = []
        else:
            if not await interactive_mode(openspace, ui_manager):
                return 1
        
    except KeyboardInterrupt:
        print("\n\nInterrupt signal detected")
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        print(f"\nError: {e}")
        return 1
    finally:
        if startup_console_log_handlers:
            _restore_console_logs(startup_console_log_handlers)
            startup_console_log_handlers = []
        if openspace:
            cleanup_log_handlers = (
                _suppress_console_logs_for_tui() if use_tui else []
            )
            try:
                if not use_tui:
                    print("\nCleaning up resources...")
                await openspace.cleanup()
            finally:
                _restore_console_logs(cleanup_log_handlers)
    
    if not use_tui:
        print("\nGoodbye!")
    return 0


def run_main():
    """Run main function"""
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n\nProgram interrupted")
        sys.exit(0)


if __name__ == "__main__":
    run_main()
