"""Filesystem-safe runtime identifier generation."""

from __future__ import annotations

import secrets
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _base32(number: int, length: int) -> str:
    chars = []
    for _ in range(length):
        number, remainder = divmod(number, 32)
        chars.append(_ALPHABET[remainder])
    return "".join(reversed(chars))


def generate_project_id() -> str:
    """Return an opaque 26-character random project identifier."""

    return f"prj_{_base32(secrets.randbits(130), 26)}"


def generate_run_id() -> str:
    """Return a time-sortable, random, filesystem-safe run identifier."""

    timestamp = _base32(time.time_ns() // 1_000_000, 11)
    random_part = _base32(secrets.randbits(75), 15)
    return f"run_{timestamp}{random_part}"


def generate_record_id() -> str:
    """Return an opaque journal record identifier."""

    return f"rec_{_base32(secrets.randbits(130), 26)}"
