"""Bounded, explicit redaction for process diagnostics and receipts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

REDACTED = "***REDACTED***"
_SENSITIVE_KEY_TOKENS = (
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "SECRET",
    "API_KEY",
    "PRIVATE_KEY",
    "CREDENTIAL",
    "AUTHORIZATION",
)


def is_sensitive_environment_key(key: str) -> bool:
    """Return whether an environment key has a known credential-bearing token."""

    normalized = key.upper().replace("-", "_")
    return any(token in normalized for token in _SENSITIVE_KEY_TOKENS)


class Redactor:
    """Redact explicit values and bounded sensitive option/environment fields."""

    __slots__ = ("_secret_values",)

    def __init__(self, secret_values: Iterable[str] = ()) -> None:
        values = {value for value in secret_values if isinstance(value, str) and value}
        self._secret_values = tuple(sorted(values, key=len, reverse=True))

    def redact_text(self, value: str) -> str:
        """Replace every exact explicit secret occurrence in text."""

        redacted = value
        for secret in self._secret_values:
            redacted = redacted.replace(secret, REDACTED)
        return redacted

    def redact_argv(self, argv: Sequence[str]) -> tuple[str, ...]:
        """Return diagnostic argv with explicit and option-shaped secrets removed."""

        result: list[str] = []
        redact_next = False
        for argument in argv:
            if redact_next:
                result.append(REDACTED)
                redact_next = False
                continue
            option, separator, _ = argument.partition("=")
            if option.startswith("-") and is_sensitive_environment_key(option.lstrip("-")):
                if separator:
                    result.append(f"{option}={REDACTED}")
                else:
                    result.append(option)
                    redact_next = True
                continue
            result.append(self.redact_text(argument))
        return tuple(result)

    def redact_environment(self, env: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
        """Return only explicit environment overrides with credential values removed."""

        return tuple(
            sorted(
                (
                    key,
                    REDACTED if is_sensitive_environment_key(key) else self.redact_text(value),
                )
                for key, value in env.items()
            )
        )

    def redact_bytes_preview(self, value: bytes, *, limit: int = 4096) -> str:
        """Decode a bounded diagnostic preview and redact explicit secret values."""

        if len(value) > limit:
            head = limit // 2
            tail = limit - head
            value = value[:head] + value[-tail:]
        return self.redact_text(value.decode("utf-8", errors="replace"))
