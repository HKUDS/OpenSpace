import asyncio
import argparse
import sys
import logging
from typing import Optional

from openspace.tool_layer import OpenSpace, OpenSpaceConfig
from openspace.utils.logging import Logger
from openspace.utils.ui import create_ui, OpenSpaceUI
from openspace.utils.ui_integration import UIIntegration
from openspace.utils.cli_display import CLIDisplay
from openspace.utils.display import colorize

logger = Logger.get_logger(__name__)


class UIManager:
    def __init__(self, ui: Optional[OpenSpaceUI], ui_integration: Optional[UIIntegration]):
        self.ui = ui
        self.ui_integration = ui_integration
        self._original_log_levels = {}
    
    async def start_live_display(self):
        if not self.ui or not self.ui_integration:
            return
        
        print()
        print(colorize("  ▣ Starting real-time visualization...", 'c'))
        print()
        await asyncio.sleep(1)
        
        self._suppress_logs()
        
        await self.ui.start_live_display()
        await self.ui_integration.start_monitoring(poll_interval=2.0)
    
    async def stop_live_display(self):
        if not self.ui or not self.ui_integration:
            return
        
        await self.ui_integration.stop_monitoring()
        await self.ui.stop_live_display()
        
        self._restore_logs()
    
    def print_summary(self, result: dict):
        if self.ui:
            self.ui.print_summary(result)
        else:
            CLIDisplay.print_result_summary(result)
    
    def _suppress_logs(self):
        log_names = ["openspace", "openspace.grounding", "openspace.agents"]
        for name in log_names:
            log = logging.getLogger(name)
            self._original_log_levels[name] = log.level
            log.setLevel(logging.CRITICAL)
    
    def _restore_logs(self):
        for name, level in self._original_log_levels.items():
            logging.getLogger(name).setLevel(level)
        self._original_log_levels.clear()


async def _execute_task(openspace: OpenSpace, query: str, ui_manager: UIManager):
    await ui_manager.start_live_display()
    result = await openspace.execute(query)
    await ui_manager.stop_live_display()
    ui_manager.print_summary(result)
    return result


async def interactive_mode(openspace: OpenSpace, ui_manager: UIManager):
    CLIDisplay.print_interactive_header()
    
    while True:
        try:
            prompt = colorize(">>> ", 'c', bold=True)
            query = input(f"\n{prompt}").strip()
            
            if not query:
                continue
            
            if query.lower() in ['exit', 'quit', 'q']:
                print("\nExiting...")
                break

            if query.lower() == 'status':
                _print_status(openspace)
                continue
            
            if query.lower() == 'help':
                CLIDisplay.print_help()
                continue

            CLIDisplay.print_task_header(query)
            await _execute_task(openspace, query, ui_manager)
            
        except KeyboardInterrupt:
            print("\n\nInterrupt signal detected, exiting...")
            break
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            print(f"\nError: {e}")


async def single_query_mode(openspace: OpenSpace, query: str, ui_manager: UIManager):
    CLIDisplay.print_task_header(query, title="▶ Single Query Execution")
    await _execute_task(openspace, query, ui_manager)


def _print_status(openspace: OpenSpace):
    """Print system status"""
    from openspace.utils.display import Box, BoxStyle
    
    box = Box(width=70, style=BoxStyle.ROUNDED, color='bl')
    print()
    print(box.text_line(colorize("System Status", 'bl', bold=True), 
                      align='center', indent=4, text_color=''))
    print(box.separator_line(indent=4))
    
    status_lines = [
        f"Initialized: {colorize('Yes' if openspace.is_initialized() else 'No', 'g' if openspace.is_initialized() else 'rd')}",
        f"Running: {colorize('Yes' if openspace.is_running() else 'No', 'y' if openspace.is_running() else 'g')}",
        f"Model: {colorize(openspace.config.llm_model, 'c')}",
    ]
    
    if openspace.is_initialized():
        backends = openspace.list_backends()
        status_lines.append(f"Backends: {colorize(', '.join(backends), 'c')}")
        
        sessions = openspace.list_sessions()
        status_lines.append(f"Active Sessions: {colorize(str(len(sessions)), 'y')}")
    
    for line in status_lines:
        print(box.text_line(f"  {line}", indent=4, text_color=''))
    
    print(box.bottom_line(indent=4))
    print()


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

    communication_parser = subparsers.add_parser(
        'communication',
        help='Run the communication gateway'
    )
    communication_parser.add_argument(
        '--config',
        type=str,
        dest='communication_config',
        help='Communication configuration file path'
    )
    communication_subparsers = communication_parser.add_subparsers(
        dest='communication_command',
        help='Communication gateway commands'
    )
    communication_run_parser = communication_subparsers.add_parser(
        'run',
        help='Start the communication gateway'
    )
    communication_run_parser.add_argument(
        '--config',
        type=str,
        dest='communication_config',
        help='Communication configuration file path'
    )
    communication_health_parser = communication_subparsers.add_parser(
        'health',
        help='Check the communication gateway health endpoint'
    )
    communication_health_parser.add_argument(
        '--config',
        type=str,
        dest='communication_config',
        help='Communication configuration file path'
    )
    communication_health_parser.add_argument('--host', type=str, default=None)
    communication_health_parser.add_argument('--port', type=int, default=None)
    
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
    parser.add_argument('--interactive', '-i', action='store_true', help='Force interactive mode')
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


def _load_config(args) -> OpenSpaceConfig:
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

    # Resolve LLM model & credentials
    #   CLI --model  >  OPENSPACE_MODEL env  >  host-agent auto-detect  >  default
    env_model = args.model or os.environ.get("OPENSPACE_MODEL", "")
    model, llm_kwargs = build_llm_kwargs(env_model)
    cli_overrides['llm_model'] = model
    cli_overrides['llm_kwargs'] = llm_kwargs

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
            
            print(f"✓ Loaded from config file: {args.config}")
        else:
            config = OpenSpaceConfig(**cli_overrides)
            print("✓ Using default configuration")
        
        if args.model:
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


async def _initialize_openspace(config: OpenSpaceConfig, args) -> OpenSpace:
    openspace = OpenSpace(config)
    
    init_steps = [("Initializing OpenSpace...", "loading")]
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
    
    CLIDisplay.print_initialization_progress(init_steps, show_header=True)
    
    return openspace


async def main():
    parser = _create_argument_parser()
    args = parser.parse_args()
    
    # Handle subcommands
    if args.command == 'refresh-cache':
        await refresh_mcp_cache(args.config)
        return 0
    if args.command == 'communication':
        from openspace.communication.gateway import main as communication_main

        communication_argv = []
        if args.communication_config:
            communication_argv.extend(['--config', args.communication_config])
        if args.communication_command:
            communication_argv.append(args.communication_command)
        if args.communication_command == 'health':
            if args.host:
                communication_argv.extend(['--host', args.host])
            if args.port is not None:
                communication_argv.extend(['--port', str(args.port)])
        return await communication_main(communication_argv)
    
    # Load configuration
    config = _load_config(args)
    
    # Setup UI
    ui, ui_integration = _setup_ui(args)
    
    # Print configuration
    CLIDisplay.print_configuration(config)
    
    openspace = None
    
    try:
        # Initialize OpenSpace
        openspace = await _initialize_openspace(config, args)
        
        # Connect UI (if enabled)
        if ui_integration:
            ui_integration.attach_llm_client(openspace._llm_client)
            ui_integration.attach_grounding_client(openspace._grounding_client)
            CLIDisplay.print_system_ready()
        
        ui_manager = UIManager(ui, ui_integration)
        
        # Run appropriate mode
        if args.query:
            await single_query_mode(openspace, args.query, ui_manager)
        else:
            await interactive_mode(openspace, ui_manager)
        
    except KeyboardInterrupt:
        print("\n\nInterrupt signal detected")
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        print(f"\nError: {e}")
        return 1
    finally:
        if openspace:
            print("\nCleaning up resources...")
            await openspace.cleanup()
    
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
