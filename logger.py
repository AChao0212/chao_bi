"""
Unified Logging Module for Chao Bi Trading System.

This module provides a centralized logging system with:
- Console logging with consistent formatting
- Telegram notifications for important events
- Different log levels (DEBUG, INFO, WARNING, ERROR)
- Module-specific loggers
"""

import logging
import sys
import inspect
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable, Tuple
from functools import wraps

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

from config import LOG_TIMEZONE as LOG_TIMEZONE_CONFIG


def _parse_timezone() -> Tuple[Optional[timezone], str]:
    """
    Parse timezone from config.

    Returns:
        Tuple of (timezone object or None for local, display string)
    """
    tz_config = LOG_TIMEZONE_CONFIG.strip()

    # System local time
    if tz_config.lower() == "system":
        return (None, "local")

    # UTC offset format: "+8", "-5", "+09", "-05:30"
    offset_match = re.match(r'^([+-])(\d{1,2})(?::?(\d{2}))?$', tz_config)
    if offset_match:
        sign = 1 if offset_match.group(1) == '+' else -1
        hours = int(offset_match.group(2))
        minutes = int(offset_match.group(3) or 0)
        offset = timedelta(hours=sign * hours, minutes=sign * minutes)
        tz = timezone(offset)
        display = f"UTC{'+' if sign > 0 else ''}{sign * hours}"
        if minutes:
            display += f":{minutes:02d}"
        return (tz, display)

    # Timezone name format: "Asia/Tokyo", "America/New_York"
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(tz_config)
            return (tz, tz_config)
        except Exception:
            pass

    # Fallback to system time if parsing failed
    print(f"[warn ] [logger] Invalid LOG_TIMEZONE '{tz_config}', using system time")
    return (None, "local")


# Parse timezone at module load
_LOG_TZ, _LOG_TZ_DISPLAY = _parse_timezone()

# =============================================================================
# Configuration
# =============================================================================

LOG_FORMAT = "[{level}] [{module}]: {message}"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_LEVEL = logging.INFO


# =============================================================================
# Custom Formatter
# =============================================================================

class ChineseFormatter(logging.Formatter):
    """Custom formatter with Chinese-friendly level names and fixed-width fields."""

    LEVEL_NAMES = {
        logging.DEBUG: "debug",
        logging.INFO: "messg",
        logging.WARNING: "warn ",
        logging.ERROR: "error",
        logging.CRITICAL: "fatal",
    }

    MODULE_WIDTH = 12  # Fixed width for module name

    def format(self, record: logging.LogRecord) -> str:
        level_name = self.LEVEL_NAMES.get(record.levelno, "unknown")
        module_name = record.name.split(".")[-1] if record.name else "root"
        # Pad or truncate module name to fixed width
        module_formatted = module_name[:self.MODULE_WIDTH].ljust(self.MODULE_WIDTH)
        return f"[{level_name}] [{module_formatted}] {record.getMessage()}"


# =============================================================================
# Logger Factory
# =============================================================================

_loggers: dict[str, logging.Logger] = {}
_telegram_notifier: Optional[Callable[[str], None]] = None


def setup_telegram_notifier(notifier_func: Callable[[str], None]) -> None:
    """
    Set up the Telegram notification function.

    Args:
        notifier_func: A function that takes a string message and sends it via Telegram.
    """
    global _telegram_notifier
    _telegram_notifier = notifier_func


def get_logger(module_name: str) -> logging.Logger:
    """
    Get or create a logger for the specified module.

    Args:
        module_name: Name of the module (e.g., 'binance', 'telegram', 'llm')

    Returns:
        Configured logger instance
    """
    if module_name in _loggers:
        return _loggers[module_name]

    logger = logging.getLogger(module_name)
    logger.setLevel(DEFAULT_LEVEL)

    # Prevent duplicate handlers
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(ChineseFormatter())
        logger.addHandler(handler)

    # Prevent propagation to root logger
    logger.propagate = False

    _loggers[module_name] = logger
    return logger


# =============================================================================
# Notification Helper
# =============================================================================

def notify(
    message: str,
    logger: Optional[logging.Logger] = None,
    level: int = logging.INFO,
    telegram: bool = False,
    event_loop = None
) -> None:
    """
    Log a message and optionally send a Telegram notification.

    Args:
        message: The message to log/send
        logger: Logger instance (if None, only sends Telegram notification)
        level: Logging level
        telegram: Whether to also send via Telegram
        event_loop: Event loop for async Telegram operations
    """
    if logger:
        logger.log(level, message)

    if telegram and _telegram_notifier:
        try:
            _telegram_notifier(message, loop=event_loop)
        except Exception:
            pass


# =============================================================================
# Convenience Functions for Common Patterns
# =============================================================================

class ModuleLogger:
    """
    A wrapper class providing convenient logging methods for a specific module.

    Usage:
        log = ModuleLogger("binance")
        log.info("Connection established")
        log.error("API call failed")
        log.notify("Order placed", telegram=True)

    Output format: [level] [module     ] [function_name     ] message
    Fixed width: module=12 chars, function=24 chars
    """

    # Fixed widths for alignment
    MODULE_WIDTH = 12
    FUNC_WIDTH = 24

    def __init__(self, module_name: str):
        self.logger = get_logger(module_name)
        self.module_name = module_name

    def _get_caller_func(self) -> str:
        """Get the name of the calling function."""
        # Go up 3 frames: _get_caller_func -> _format_message -> log method -> actual caller
        frame = inspect.currentframe()
        try:
            caller_frame = frame.f_back.f_back.f_back
            return caller_frame.f_code.co_name if caller_frame else "unknown"
        except (AttributeError, TypeError):
            return "unknown"
        finally:
            del frame

    def _format_message(self, message: str) -> str:
        """Format message with fixed-width function name and timestamp."""
        func_name = self._get_caller_func()
        # Pad or truncate to fixed width
        func_formatted = func_name[:self.FUNC_WIDTH].ljust(self.FUNC_WIDTH)
        # Add timestamp with timezone from config
        if _LOG_TZ is None:
            now = datetime.now()  # System local time
        else:
            now = datetime.now(_LOG_TZ)
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
        return f"[{func_formatted}][{timestamp} ({_LOG_TZ_DISPLAY})]: {message}"

    def debug(self, message: str) -> None:
        """Log a debug message."""
        self.logger.debug(self._format_message(message))

    def info(self, message: str) -> None:
        """Log an info message."""
        self.logger.info(self._format_message(message))

    def warning(self, message: str) -> None:
        """Log a warning message."""
        self.logger.warning(self._format_message(message))

    def error(self, message: str) -> None:
        """Log an error message."""
        self.logger.error(self._format_message(message))

    def critical(self, message: str) -> None:
        """Log a critical message."""
        self.logger.critical(self._format_message(message))

    def notify(
        self,
        message: str,
        level: int = logging.INFO,
        telegram: bool = True,
        event_loop = None
    ) -> None:
        """
        Log a message and send a Telegram notification.

        Args:
            message: The message to log and send
            level: Logging level
            telegram: Whether to send via Telegram (default True)
            event_loop: Event loop for async operations
        """
        self.logger.log(level, self._format_message(message))
        if telegram and _telegram_notifier:
            try:
                _telegram_notifier(message, loop=event_loop)
            except Exception:
                pass


# =============================================================================
# Decorators
# =============================================================================

def log_exceptions(logger: logging.Logger):
    """
    Decorator that logs exceptions raised by a function.

    Usage:
        @log_exceptions(my_logger)
        def my_function():
            ...
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.error(f"{func.__name__} failed: {e}")
                raise
        return wrapper
    return decorator


def log_async_exceptions(logger: logging.Logger):
    """
    Decorator that logs exceptions raised by an async function.

    Usage:
        @log_async_exceptions(my_logger)
        async def my_async_function():
            ...
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                logger.error(f"{func.__name__} failed: {e}")
                raise
        return wrapper
    return decorator
