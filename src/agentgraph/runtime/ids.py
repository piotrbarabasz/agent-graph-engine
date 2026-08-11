"""Filesystem-safe runtime identifier generation."""

from __future__ import annotations

import re
import secrets
import time

from .errors import InvalidRuntimeIdentifierError

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
MAX_RUNTIME_IDENTIFIER_LENGTH = 128
_SAFE_SUFFIX = re.compile(r"[A-Za-z0-9_-]+", re.ASCII)


def _validate_identifier(value: str, *, prefix: str, kind: str) -> str:
    if (
        type(value) is not str
        or len(value) > MAX_RUNTIME_IDENTIFIER_LENGTH
        or not value.startswith(prefix)
        or _SAFE_SUFFIX.fullmatch(value[len(prefix) :]) is None
    ):
        raise InvalidRuntimeIdentifierError(f"invalid {kind}")
    return value


def validate_project_id(project_id: str) -> str:
    """Validate and return one canonical filesystem-safe project ID."""

    return _validate_identifier(project_id, prefix="prj_", kind="project_id")


def validate_run_id(run_id: str) -> str:
    """Validate and return one canonical filesystem-safe run ID."""

    return _validate_identifier(run_id, prefix="run_", kind="run_id")


def validate_record_id(record_id: str) -> str:
    """Validate and return one canonical filesystem-safe journal record ID."""

    return _validate_identifier(record_id, prefix="rec_", kind="record_id")


def _base32(number: int, length: int) -> str:
    chars = []
    for _ in range(length):
        number, remainder = divmod(number, 32)
        chars.append(_ALPHABET[remainder])
    return "".join(reversed(chars))


def generate_project_id() -> str:
    """Return an opaque 26-character random project identifier."""

    return validate_project_id(f"prj_{_base32(secrets.randbits(130), 26)}")


def generate_run_id() -> str:
    """Return a time-sortable, random, filesystem-safe run identifier."""

    timestamp = _base32(time.time_ns() // 1_000_000, 11)
    random_part = _base32(secrets.randbits(75), 15)
    return validate_run_id(f"run_{timestamp}{random_part}")


def generate_record_id() -> str:
    """Return an opaque journal record identifier."""

    return validate_record_id(f"rec_{_base32(secrets.randbits(130), 26)}")
