"""Helpers for optional dependency imports with clear error messages."""


def require_optional_dependency(
    module_name: str,
    package_name: str,
    install_extra: str,
) -> None:
    """
    Check if an optional dependency is available.

    Raises ImportError with clear installation instructions if not.

    Args:
        module_name: The module to import (e.g., "fastapi")
        package_name: Display name (e.g., "pymodules.api")
        install_extra: The pip extra to install (e.g., "api")
    """
    try:
        __import__(module_name)
    except ImportError as e:
        raise ImportError(
            f"'{package_name}' requires '{module_name}' which is not installed.\n"
            f"Install it with: pip install pymodules[{install_extra}]"
        ) from e
