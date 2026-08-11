"""Crash-safe atomic writes on one filesystem."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path


def atomic_write_bytes(
    destination: Path,
    data: bytes,
    *,
    before_replace: Callable[[Path, Path], None] | None = None,
    replace: Callable[
        [str | bytes | os.PathLike[str], str | bytes | os.PathLike[str]], None
    ] = os.replace,
) -> None:
    """Durably replace a file using a temporary sibling and fsync."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if before_replace is not None:
            before_replace(temp, destination)
        replace(temp, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
