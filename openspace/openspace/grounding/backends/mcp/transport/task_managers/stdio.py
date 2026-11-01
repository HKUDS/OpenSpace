"""
StdIO connection management for MCP implementations.

This module provides a connection manager for stdio-based MCP connections
that ensures proper task isolation and resource cleanup.
"""

import asyncio
import io
import logging
import sys
from typing import Any, TextIO, Tuple

from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client

from openspace.utils.logging import Logger
from openspace.grounding.core.transport.task_managers import (
    AsyncContextConnectionManager,
)

logger = Logger.get_logger(__name__)


class FilteredStderrWrapper(io.TextIOBase):
    """Wrapper for stderr that filters out harmless MCP server shutdown messages.
    
    This wrapper suppresses error messages from MCP servers during shutdown
    that are harmless but create noise in the logs.
    """
    
    def __init__(self, wrapped_stream: TextIO):
        """Initialize the wrapper.
        
        Args:
            wrapped_stream: The underlying stderr stream
        """
        self._stream = wrapped_stream
        self._buffer = ""
        self._in_traceback = False
        self._traceback_lines = []
        self._in_rich_traceback = False  # Track rich-formatted tracebacks
        self._rich_traceback_needs_error_line = False  # After ╰, need one more line
    
    def write(self, s: str) -> int:
        """Write to stderr, filtering out harmless error messages.
        
        Args:
            s: The string to write
            
        Returns:
            Number of characters written
        """
        # Buffer the input for line-by-line processing
        self._buffer += s
        
        # Process complete lines
        while '\n' in self._buffer:
            line, self._buffer = self._buffer.split('\n', 1)
            self._process_line(line + '\n')
        
        return len(s)
    
    def _process_line(self, line: str):
        """Process a single line and decide whether to output it."""
        # Detect start of traceback or exception group
        if line.lstrip().startswith(("╭", "┏")):
            self._in_traceback = True
            self._in_rich_traceback = True
            self._rich_traceback_needs_error_line = False 
            self._traceback_lines = [line]
            return

        if (line.strip().startswith('Traceback (most recent call last)') or
            line.strip().startswith('Exception Group Traceback (most recent call last)') or
            line.strip().startswith('BaseExceptionGroup:') or
            line.strip().startswith('ExceptionGroup:')):
            self._in_traceback = True
            self._traceback_lines = [line]
            self._in_rich_traceback = False
            self._rich_traceback_needs_error_line = False
            return
        
        # Collect traceback lines
        if self._in_traceback:
            self._traceback_lines.append(line)

            # If not in rich traceback mode, but current line contains rich border characters, switch to rich mode
            if not self._in_rich_traceback and any(ch in line for ch in ("╭", "┏")):
                self._in_rich_traceback = True
            
            # Check for end of rich-formatted traceback (line with ╰)
            if self._in_rich_traceback and '╰' in line:
                # Rich traceback box ended, but we need to collect the error line that follows
                self._rich_traceback_needs_error_line = True
                return
            
            # If we just ended a rich traceback, this should be the error line
            if self._rich_traceback_needs_error_line:
                # Now we have the complete rich traceback including the error line
                if self._is_harmless_error():
                    logger.debug(f"Suppressed harmless rich-formatted MCP server error")
                else:
                    # Output the full traceback
                    for tb_line in self._traceback_lines:
                        self._stream.write(tb_line)
                    self._stream.flush()
                
                # Reset traceback collection
                self._in_traceback = False
                self._in_rich_traceback = False
                self._rich_traceback_needs_error_line = False
                self._traceback_lines = []
                return
            
            # For exception groups, we need to collect more lines
            # Check if we've collected enough to determine if it's harmless
            if len(self._traceback_lines) > 5 and not self._in_rich_traceback:
                # Check periodically if this is a harmless error
                if self._is_harmless_error():
                    # Suppress this traceback
                    logger.debug(f"Suppressed harmless MCP server shutdown error")
                    self._in_traceback = False
                    self._in_rich_traceback = False
                    self._rich_traceback_needs_error_line = False
                    self._traceback_lines = []
                    return
            
            # Check if this is the error line (last line of regular traceback)
            # But not for rich tracebacks which use box characters
            # A final traceback line is typically unindented and contains "ErrorType: message"
            if not self._in_rich_traceback and line and not line[0].isspace() and ':' in line:
                # Check if this is a harmless cleanup error
                if self._is_harmless_error():
                    # Suppress this traceback
                    logger.debug(f"Suppressed harmless MCP server shutdown error")
                else:
                    # Output the full traceback
                    for tb_line in self._traceback_lines:
                        self._stream.write(tb_line)
                    self._stream.flush()
                
                # Reset traceback collection
                self._in_traceback = False
                self._in_rich_traceback = False
                self._rich_traceback_needs_error_line = False
                self._traceback_lines = []
                return
            
            # If we've collected too many lines without finding the end, output and reset
            if len(self._traceback_lines) > 100:
                # Output what we have
                for tb_line in self._traceback_lines:
                    self._stream.write(tb_line)
                self._stream.flush()
                self._in_traceback = False
                self._in_rich_traceback = False
                self._rich_traceback_needs_error_line = False
                self._traceback_lines = []
                return
        else:
            # Normal line - check if it's a harmless error log
            line_lower = line.lower()
            harmless_log_patterns = [
                'an error occurred during closing of asynchronous generator',
                'asyncgen:',
                'service stopped.',
            ]
            
            # Check if this is a harmless log line
            is_harmless_log = any(pattern in line_lower for pattern in harmless_log_patterns)
            
            if not is_harmless_log:
                # Output normal lines
                self._stream.write(line)
                self._stream.flush()
            else:
                # Suppress harmless log messages
                logger.debug(f"Suppressed harmless log line: {line.strip()}")
    
    def _is_harmless_error(self) -> bool:
        """Check if the collected traceback is a harmless error."""
        traceback_text = ''.join(self._traceback_lines).lower()
        
        # List of harmless error patterns (case-insensitive)
        harmless_patterns = [
            'valueerror: i/o operation on closed file',
            'oserror: [errno 9] bad file descriptor',
            'brokenpipeerror',
            'runtimeerror: attempted to exit cancel scope in a different task',
            'baseexceptiongroup: unhandled errors in a taskgroup',
            'generatorexit',
            'an error occurred during closing of asynchronous generator',
        ]
        
        # Check if any pattern matches and it's related to shutdown
        for pattern in harmless_patterns:
            if pattern in traceback_text:
                # Also check if it's related to shutdown/cleanup
                shutdown_keywords = ['finally:', 'stopped', 'cleanup', '__exit__', '__aexit__', 'stdio_client', 'service stopped']
                if any(keyword in traceback_text for keyword in shutdown_keywords):
                    return True
        
        return False
    
    def flush(self):
        """Flush any remaining buffered content and the underlying stream."""
        if self._buffer:
            self._process_line(self._buffer)
            self._buffer = ""
        
        if self._traceback_lines:
            # Flush incomplete traceback
            for line in self._traceback_lines:
                self._stream.write(line)
            self._traceback_lines = []
        
        self._stream.flush()
    
    def fileno(self) -> int:
        """Return the file descriptor of the underlying stream."""
        if hasattr(self._stream, 'fileno'):
            return self._stream.fileno()
        return -1
    
    @property
    def closed(self) -> bool:
        """Check if the stream is closed."""
        return self._stream.closed


class StdioConnectionManager(AsyncContextConnectionManager[Tuple[Any, Any], ...]):
    """Connection manager for stdio-based MCP connections.

    This class handles the proper task isolation for stdio_client context managers
    to prevent the "cancel scope in different task" error. It runs the stdio_client
    in a dedicated task and manages its lifecycle.
    
    Note: Error handling during cleanup (e.g., I/O operations on closed files) is 
    handled by the parent AsyncContextConnectionManager class in _close_connection().
    """

    def __init__(
        self,
        server_params: StdioServerParameters,
        errlog: TextIO | None = None,
    ):
        """Initialize a new stdio connection manager.

        Args:
            server_params: The parameters for the stdio server
            errlog: The error log stream (defaults to filtered sys.stderr)
        """
        # Wrap stderr to filter out harmless shutdown errors
        if errlog is None:
            errlog = FilteredStderrWrapper(sys.stderr)
        elif not isinstance(errlog, FilteredStderrWrapper):
            errlog = FilteredStderrWrapper(errlog)
        
        super().__init__(stdio_client, server_params, errlog)
        self.server_params = server_params
        self.errlog = errlog
        self._mcp_logger_filter = None
        self._stop_event: asyncio.Event | None = None  # Signal for background task
        self._runner_task: asyncio.Task | None = None  # Background runner task
        self._conn_future: asyncio.Future | None = None  # Future for the established connection
        logger.debug("StdioConnectionManager init with params=%s", server_params)
    
    async def _establish_connection(self) -> Tuple[Any, Any]:
        """Establish connection in a dedicated task to avoid cancel-scope issues."""
        # Suppress MCP SDK's noisy JSON parse errors **before** starting the runner
        self._suppress_mcp_json_errors()

        # Lazily create primitives the first time we connect
        if self._stop_event is None:
            self._stop_event = asyncio.Event()
        if self._conn_future is None or self._conn_future.done():
            self._conn_future = asyncio.get_event_loop().create_future()

        async def _runner():  # Runs in its *own* task (same task for enter/exit)
            try:
                async with stdio_client(self.server_params, self.errlog) as conn:
                    # Pass connection back to the caller
                    if not self._conn_future.done():
                        self._conn_future.set_result(conn)
                    # Wait until close is requested
                    await self._stop_event.wait()
            finally:
                # Make sure the future is set even on error so awaiters don’t hang
                if not self._conn_future.done():
                    self._conn_future.set_exception(RuntimeError("Connection failed"))

        # Start background runner if not already active
        if self._runner_task is None or self._runner_task.done():
            self._runner_task = asyncio.create_task(_runner(), name="stdio_client_runner")

        # Wait for the connection tuple from the future
        conn: Tuple[Any, Any] = await self._conn_future  # type: ignore
        return conn

    async def _close_connection(self) -> None:
        """Request the background task to exit its context and wait for it."""
        try:
            # Restore original logging configuration *before* shutdown
            self._restore_mcp_logging()

            # Signal the runner to exit its context manager
            if self._stop_event and not self._stop_event.is_set():
                self._stop_event.set()

            # Await the runner task so that __aexit__ executes in *its* task
            if self._runner_task:
                try:
                    await asyncio.wait_for(self._runner_task, timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning("Timeout while waiting for stdio_client to shut down")
        finally:
            # Clean up helpers so next connect() creates new ones
            self._runner_task = None
            self._stop_event = None
            self._conn_future = None
    
    def _suppress_mcp_json_errors(self):
        """Suppress MCP SDK's JSON parsing error logs.
        
        The MCP SDK logs errors when it receives non-JSON messages from servers.
        These are harmless (the SDK continues working), so we filter them out.
        """
        mcp_logger = logging.getLogger("mcp.client.stdio")
        
        class JSONErrorFilter(logging.Filter):
            """Filter out JSON parsing errors from MCP SDK."""
            def filter(self, record):
                # Suppress "Failed to parse JSONRPC message" errors
                if "Failed to parse JSONRPC message" in str(record.msg):
                    return False
                return True
        
        self._mcp_logger_filter = JSONErrorFilter()
        mcp_logger.addFilter(self._mcp_logger_filter)
    
    def _restore_mcp_logging(self):
        """Restore MCP SDK logging to normal."""
        if self._mcp_logger_filter:
            mcp_logger = logging.getLogger("mcp.client.stdio")
            mcp_logger.removeFilter(self._mcp_logger_filter)
            self._mcp_logger_filter = None

if not isinstance(sys.stderr, FilteredStderrWrapper):
    sys.stderr = FilteredStderrWrapper(sys.stderr)
    logger.debug("Applied global FilteredStderrWrapper to sys.stderr")