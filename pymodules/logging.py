"""
Logging configuration for PyModules framework.

Provides structured logging with configurable levels and formats.
"""

import logging
import sys

# Create the framework logger
logger = logging.getLogger("pymodules")


class PyModulesFormatter(logging.Formatter):
    """Custom formatter for PyModules that includes context."""

    def __init__(self, include_timestamp: bool = True):
        if include_timestamp:
            fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
            datefmt = "%Y-%m-%d %H:%M:%S"
        else:
            fmt = "%(levelname)-8s | %(name)s | %(message)s"
            datefmt = None
        super().__init__(fmt=fmt, datefmt=datefmt)


def configure_logging(
    level: int = logging.INFO,
    handler: logging.Handler | None = None,
    format_timestamps: bool = True,
) -> logging.Logger:
    """
    Configure the PyModules framework logger.

    Args:
        level: Logging level (e.g., logging.DEBUG, logging.INFO)
        handler: Custom handler. If None, uses StreamHandler to stderr.
        format_timestamps: Include timestamps in log output.

    Returns:
        The configured logger instance.

    Example:
        from pymodules.logging import configure_logging
        import logging

        # Enable debug logging
        configure_logging(level=logging.DEBUG)

        # Or use a file handler
        file_handler = logging.FileHandler("pymodules.log")
        configure_logging(handler=file_handler)
    """
    logger.setLevel(level)

    # Remove existing handlers to avoid duplicates
    logger.handlers.clear()

    if handler is None:
        handler = logging.StreamHandler(sys.stderr)

    handler.setFormatter(PyModulesFormatter(include_timestamp=format_timestamps))
    handler.setLevel(level)
    logger.addHandler(handler)

    return logger


def get_logger(name: str) -> logging.Logger:
    """
    Get a child logger for a specific component.

    Args:
        name: Component name (e.g., "host", "module")

    Returns:
        A child logger instance.

    Example:
        from pymodules.logging import get_logger
        log = get_logger("my_module")
        log.info("Module initialized")
    """
    return logger.getChild(name)


# Module-level loggers for internal use
host_logger = get_logger("host")
module_logger = get_logger("module")
fastapi_logger = get_logger("fastapi")
