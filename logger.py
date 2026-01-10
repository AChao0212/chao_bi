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
from datetime import datetime
from typing import Optional, Callable
from functools import wraps

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
    """Custom formatter with Chinese-friendly level names."""

    LEVEL_NAMES = {
        logging.DEBUG: "debug",
        logging.INFO: "messg",
        logging.WARNING: "warn ",
        logging.ERROR: "error",
        logging.CRITICAL: "fatal",
    }

    def format(self, record: logging.LogRecord) -> str:
        level_name = self.LEVEL_NAMES.get(record.levelno, "unknown")
        module_name = record.name.split(".")[-1] if record.name else "root"
        return f"[{level_name}] [{module_name}]: {record.getMessage()}"


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
    """

    def __init__(self, module_name: str):
        self.logger = get_logger(module_name)
        self.module_name = module_name

    def debug(self, message: str) -> None:
        """Log a debug message."""
        self.logger.debug(message)

    def info(self, message: str) -> None:
        """Log an info message."""
        self.logger.info(message)

    def warning(self, message: str) -> None:
        """Log a warning message."""
        self.logger.warning(message)

    def error(self, message: str) -> None:
        """Log an error message."""
        self.logger.error(message)

    def critical(self, message: str) -> None:
        """Log a critical message."""
        self.logger.critical(message)

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
        self.logger.log(level, message)
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
