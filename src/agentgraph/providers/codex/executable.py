"""Deterministic native Codex executable resolution."""

from __future__ import annotations

import os
import platform
from pathlib import Path

_WINDOWS_NATIVE_LAYOUTS = {
    "amd64": (
        "codex-win32-x64",
        "x86_64-pc-windows-msvc",
    ),
    "x86_64": (
        "codex-win32-x64",
        "x86_64-pc-windows-msvc",
    ),
    "arm64": (
        "codex-win32-arm64",
        "aarch64-pc-windows-msvc",
    ),
    "aarch64": (
        "codex-win32-arm64",
        "aarch64-pc-windows-msvc",
    ),
}


def resolve_codex_executable(
    selector: str,
    *,
    windows: bool | None = None,
    search_path: str | None = None,
    machine: str | None = None,
) -> str:
    """Resolve the native executable behind a standard Windows npm shim.

    Explicit nonstandard selectors are preserved. If no safe native candidate can
    be proven, the original selector is returned so process startup reports the
    ordinary typed unavailable diagnostic.
    """

    if windows is None:
        windows = os.name == "nt"
    if not windows or Path(selector).name.casefold() not in {
        "codex",
        "codex.cmd",
        "codex.ps1",
    }:
        return selector

    architecture = (machine or platform.machine()).casefold()
    layout = _WINDOWS_NATIVE_LAYOUTS.get(architecture)
    if layout is None:
        return selector

    roots = _candidate_roots(selector, search_path=search_path)
    for root in roots:
        direct = root / "codex.exe"
        if _is_regular_file(direct):
            return str(direct.resolve(strict=True))

    package, target = layout
    for root in roots:
        if not _has_npm_shim(root, selector):
            continue
        candidate = (
            root
            / "node_modules"
            / "@openai"
            / "codex"
            / "node_modules"
            / "@openai"
            / package
            / "vendor"
            / target
            / "bin"
            / "codex.exe"
        )
        if _is_regular_file(candidate):
            return str(candidate.resolve(strict=True))
    return selector


def _candidate_roots(selector: str, *, search_path: str | None) -> tuple[Path, ...]:
    selected = Path(selector)
    values: list[Path] = []
    if selected.parent != Path("."):
        values.append(selected.parent)
    else:
        raw_path = os.environ.get("PATH", "") if search_path is None else search_path
        values.extend(
            Path(entry.strip('"')) for entry in raw_path.split(os.pathsep) if entry.strip('"')
        )
    unique: list[Path] = []
    seen: set[str] = set()
    for value in values:
        identity = os.path.normcase(os.path.abspath(value))
        if identity not in seen:
            seen.add(identity)
            unique.append(value)
    return tuple(unique)


def _has_npm_shim(root: Path, selector: str) -> bool:
    selected = Path(selector)
    if selected.parent != Path("."):
        return _is_regular_file(selected)
    return any(_is_regular_file(root / name) for name in ("codex.cmd", "codex.ps1", "codex"))


def _is_regular_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False
