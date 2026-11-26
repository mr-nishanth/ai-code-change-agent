"""Typed application errors."""


class AppError(Exception):
    """Base class for expected, typed application failures."""


class ConfigurationError(AppError):
    """Invalid or missing deployment configuration."""
