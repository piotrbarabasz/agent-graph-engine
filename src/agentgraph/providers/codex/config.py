"""Immutable configuration for the local Codex CLI provider."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CodexProviderConfig:
    executable: str = "codex"
    executable_arguments: tuple[str, ...] = ()
    timeout_seconds: float = 900.0
    model: str | None = None
    max_result_bytes: int = 4 * 1024 * 1024

    def __post_init__(self) -> None:
        if not self.executable or "\x00" in self.executable:
            raise ValueError("Codex executable must be non-empty and NUL-free")
        if not all(
            isinstance(value, str) and "\x00" not in value for value in self.executable_arguments
        ):
            raise ValueError("Codex executable arguments must be NUL-free strings")
        if isinstance(self.timeout_seconds, bool) or self.timeout_seconds <= 0:
            raise ValueError("Codex timeout must be positive")
        if self.model is not None and (not self.model or "\x00" in self.model):
            raise ValueError("Codex model override must be non-empty and NUL-free")
        if isinstance(self.max_result_bytes, bool) or self.max_result_bytes <= 0:
            raise ValueError("Codex result limit must be positive")
