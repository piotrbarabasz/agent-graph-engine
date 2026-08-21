"""Stable failures for untrusted repository configuration."""

from __future__ import annotations

from pathlib import Path


class ConfigError(Exception):
    """A concise, safely reportable project configuration failure."""

    def __init__(self, code: str, message: str, *, path: Path | None = None) -> None:
        self.code = code
        self.message = message
        self.path = path
        super().__init__(message)
