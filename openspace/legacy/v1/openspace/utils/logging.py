import logging
import os
import sys
import threading
import json
from pathlib import Path
from datetime import datetime
from typing import Optional
from colorama import init

init(autoreset=True)


def _load_log_level_from_config() -> int:
    """
    Load log_level from config_grounding.json and convert to OPENSPACE_DEBUG value.
    Returns: 0 (WARNING), 1 (INFO), or 2 (DEBUG)
    """
    try:
        config_path = Path(__file__).parent.parent / "config" / "config_grounding.json"
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
                log_level = config.get("log_level", "INFO").upper()
                
                # Convert log level string to OPENSPACE_DEBUG value
                level_map = {
                    "DEBUG": 2,
                    "INFO": 1,
                    "WARNING": 0,
                    "ERROR": 0,
                    "CRITICAL": 0
                }
                return level_map.get(log_level, 1)  # Default to INFO
    except Exception:
        # If any error occurs, silently return default INFO level
        pass
    return 1  # Default to INFO


# 0=WARNING, 1=INFO, 2=DEBUG; can be overridden by set_debug / environment variable
# Load from config_grounding.json to ensure consistency
OPENSPACE_DEBUG = _load_log_level_from_config()

# Default log directory and file pattern
# Use absolute path to openspace/logs directory
DEFAULT_LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "logs")
DEFAULT_LOG_FILE_PATTERN = "openspace_{timestamp}.log"


class FlushFileHandler(logging.FileHandler):
    """File handler that flushes after each emit for real-time logging"""
    
    def emit(self, record):
        super().emit(record)
        self.flush()  # Immediately flush to disk


class ColoredFormatter(logging.Formatter):
    COLORS = {
        'DEBUG': '\033[1;36m',    # Bold cyan
        'INFO': '\033[1;32m',     # Bold green
        'WARNING': '\033[1;33m',  # Bold yellow
        'ERROR': '\033[1;31m',    # Bold red
        'CRITICAL': '\033[1;35m', # Bold magenta
        'RESET': '\033[0m',
    }

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        
        level_color = self.COLORS.get(record.levelname, self.COLORS["RESET"])
        colored_line = f"{level_color}{formatted}{self.COLORS['RESET']}"
        
        return colored_line


class Logger:
    """
    Thread-safe logger facade that:
    1. Configures handlers only once (lazy initialization).
    2. Ensures all subsequent loggers obtained via ``Logger.get_logger()``
       inherit the configured handlers.
    3. Dynamically adapts log levels according to ``OPENSPACE_DEBUG``.
    """

    _ROOT_NAME = "openspace"        # Package root name
    # Standard format: time with milliseconds | level | file:line number | message
    _LOG_FORMAT = (
        "%(asctime)s.%(msecs)03d [%(levelname)-8s] %(filename)s:%(lineno)d - %(message)s"
    )

    _lock = threading.Lock()
    _configured = False
    _registered: dict[str, logging.Logger] = {}
    
    @staticmethod
    def _get_default_log_file() -> str:
        """Generate default log file path with timestamp (to seconds)
        
        Log files are organized by the running script name:
        - logs/<script_name>/openspace_2025-10-24_15-30-00.log
        """
        # Get the name of the main script
        script_name = "openspace"  # Default name
        try:
            import __main__
            if hasattr(__main__, "__file__") and __main__.__file__:
                # Extract script name without extension
                script_path = os.path.basename(__main__.__file__)
                script_name = os.path.splitext(script_path)[0]
        except Exception:
            # If can't get script name, use default
            pass
        
        # Create log directory: logs/<script_name>/
        log_dir = os.path.join(DEFAULT_LOG_DIR, script_name)
        
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = DEFAULT_LOG_FILE_PATTERN.format(timestamp=timestamp)
        return os.path.abspath(os.path.join(log_dir, filename))

    @classmethod
    def get_logger(cls, name: Optional[str] = None) -> logging.Logger:
        """Return a logger with *name* (defaults to ``openspace``).
        The first call triggers :meth:`configure` automatically."""
        if name is None:
            name = cls._ROOT_NAME

        # Check if configuration is needed to avoid recursive calls.
        need_config = False
        with cls._lock:
            logger = cls._registered.get(name)
            if logger is None:
                logger = logging.getLogger(name)
                logger.propagate = True
                cls._registered[name] = logger
            if not cls._configured:
                need_config = True

        if need_config:
            cls.configure()
        return logger

    @classmethod
    def configure(
        cls,
        *,
        level: Optional[int] = None,
        fmt: Optional[str] = None,
        log_to_console: bool = True,
        log_to_file: Optional[str] = "auto",
        use_colors: bool = True,
        force_color: bool = False,
        force: bool = False,
        attach_to_root: bool = False,
    ) -> None:
        """
        Configure the logging system. Usually called automatically
        on first use; pass ``force=True`` to reconfigure explicitly.

        Args:
            level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            fmt: Log format string
            log_to_console: Whether to output to console
            log_to_file: Log file path ("auto" auto-generate by date, None disable, or specify path)
            use_colors: Whether to use colors on console
            force_color: Force use of colors (even if not supported)
            force: Whether to force reconfiguration
            attach_to_root: Whether to attach to root logger

        If *attach_to_root* is ``True``, handlers are attached to the *root*
        logger (``""``). This makes every logger—regardless of its name—
        inherit the handlers (handy for standalone scripts) but will also
        surface logs from third-party libraries. Choose with care.
        """
        with cls._lock:
            if cls._configured and not force:
                # Already configured and no need to force reconfiguration, only update level.
                if level is not None:
                    cls._update_level(level)
                return

            resolved_level = cls._resolve_level(level)
            fmt_str = fmt or cls._LOG_FORMAT

            # Handle log_to_file parameter
            actual_log_file = None
            if log_to_file == "auto":
                actual_log_file = cls._get_default_log_file()
            elif log_to_file is not None:
                actual_log_file = log_to_file

            # Select the logger to attach handlers to (root logger or openspace).
            target_logger = (
                logging.getLogger() if attach_to_root else logging.getLogger(cls._ROOT_NAME)
            )
            target_logger.setLevel(resolved_level)

            # Clean up old handlers.
            for h in target_logger.handlers[:]:
                target_logger.removeHandler(h)

            # Construct Formatter
            date_fmt = "%Y-%m-%d %H:%M:%S"
            color_supported = force_color or (use_colors and cls._stdout_supports_color())
            console_formatter = (
                ColoredFormatter(fmt_str, datefmt=date_fmt) if color_supported 
                else logging.Formatter(fmt_str, datefmt=date_fmt)
            )
            file_formatter = logging.Formatter(fmt_str, datefmt=date_fmt)

            # Console Handler
            if log_to_console:
                ch = logging.StreamHandler(sys.stdout)
                ch.setLevel(resolved_level)
                ch.setFormatter(console_formatter)
                target_logger.addHandler(ch)

            # File Handler (with real-time flush)
            if actual_log_file:
                dir_path = os.path.dirname(actual_log_file)
                if dir_path:
                    os.makedirs(dir_path, exist_ok=True)
                fh = FlushFileHandler(actual_log_file, encoding="utf-8")
                fh.setLevel(resolved_level)
                fh.setFormatter(file_formatter)
                target_logger.addHandler(fh)
                
                # Record log file location
                if not cls._configured:
                    print(f"Log file enabled: {actual_log_file}")

            cls._configured = True

    @classmethod
    def set_level(cls, level: str) -> None:
        """Set log level by name (e.g. ``"DEBUG"``, ``"INFO"``, ``"WARNING"``)."""
        resolved = getattr(logging, level.upper(), None)
        if resolved is None or not isinstance(resolved, int):
            raise ValueError(f"Unknown log level: {level!r}")
        if not cls._configured:
            cls.configure(level=resolved, attach_to_root=True)
            return

        root_logger = logging.getLogger()
        root_logger.setLevel(resolved)
        for handler in root_logger.handlers:
            handler.setLevel(resolved)

        cls._update_level(resolved)

    @classmethod
    def set_debug(cls, debug_level: int = 2) -> None:
        """Dynamically switch debug level: 0 = WARNING, 1 = INFO, 2 = DEBUG."""
        global OPENSPACE_DEBUG
        OPENSPACE_DEBUG = max(0, min(debug_level, 2))
        cls._update_level(cls._resolve_level(None))

    @classmethod
    def add_file_handler(
        cls, 
        filepath: str, 
        logger_name: Optional[str] = None
    ) -> None:
        """
        Append a file handler to the given (default ``openspace``) logger.
        
        Args:
            filepath: Log file path
            logger_name: Log logger name
        """
        logger = cls.get_logger(logger_name or cls._ROOT_NAME)

        dir_path = os.path.dirname(filepath)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)

        fh = FlushFileHandler(filepath, encoding="utf-8")
        fh.setLevel(logger.level)
        fh.setFormatter(logging.Formatter(cls._LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
        logger.addHandler(fh)

    @classmethod
    def reset_configuration(cls) -> None:
        """Remove all handlers and clear registered loggers."""
        with cls._lock:
            for lg in cls._registered.values():
                for h in lg.handlers[:]:
                    lg.removeHandler(h)
            cls._registered.clear()
            cls._configured = False

    @staticmethod
    def _stdout_supports_color() -> bool:
        return sys.stdout.isatty() and not os.getenv("NO_COLOR")

    @classmethod
    def _resolve_level(cls, level: Optional[int]) -> int:
        if level is not None:
            # Allow passing logging.INFO / "INFO" / 20 etc.
            return getattr(logging, str(level).upper(), level)
        return {2: logging.DEBUG, 1: logging.INFO}.get(OPENSPACE_DEBUG, logging.WARNING)

    @classmethod
    def _update_level(cls, level: int) -> None:
        for lg in cls._registered.values():
            lg.setLevel(level)
            for h in lg.handlers:
                h.setLevel(level)


# Adjust debug level automatically according to the
# ``OPENSPACE_DEBUG`` (preferred) or legacy ``DEBUG`` environment variable.
_env_debug = os.getenv("OPENSPACE_DEBUG") or os.getenv("DEBUG")
if _env_debug is not None:
    try:
        Logger.set_debug(int(_env_debug))
    except ValueError:
        # When not a number, use common format: DEBUG=1/true
        Logger.set_debug(2 if _env_debug.strip().lower() in {"1", "true", "yes"} else 0)

# Initialize logger system, attach to root so all loggers inherit the configuration
# This ensures any logger obtained via Logger.get_logger() will work correctly
Logger.configure(attach_to_root=True)

# Get openspace logger for internal logging
logger = Logger.get_logger()
logger.debug("OpenSpace logging initialized")
