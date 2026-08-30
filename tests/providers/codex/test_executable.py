from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentgraph.providers.codex.executable import resolve_codex_executable


def test_windows_npm_shim_resolves_to_packaged_native_executable(tmp_path: Path) -> None:
    npm = tmp_path / "npm"
    native = (
        npm
        / "node_modules"
        / "@openai"
        / "codex"
        / "node_modules"
        / "@openai"
        / "codex-win32-x64"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / "bin"
        / "codex.exe"
    )
    native.parent.mkdir(parents=True)
    native.write_bytes(b"native fixture")
    (npm / "codex.cmd").write_text("npm shim", encoding="utf-8")

    resolved = resolve_codex_executable(
        "codex", windows=True, search_path=str(npm), machine="AMD64"
    )

    assert resolved == str(native.resolve())


def test_earlier_windows_npm_shim_precedes_later_direct_executable(tmp_path: Path) -> None:
    npm = tmp_path / "npm"
    native = (
        npm
        / "node_modules"
        / "@openai"
        / "codex"
        / "node_modules"
        / "@openai"
        / "codex-win32-x64"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / "bin"
        / "codex.exe"
    )
    native.parent.mkdir(parents=True)
    native.write_bytes(b"packaged native fixture")
    (npm / "codex.cmd").write_text("npm shim", encoding="utf-8")
    later = tmp_path / "later"
    later.mkdir()
    (later / "codex.exe").write_bytes(b"direct native fixture")

    resolved = resolve_codex_executable(
        "codex",
        windows=True,
        search_path=os.pathsep.join((str(npm), str(later))),
        machine="AMD64",
    )

    assert resolved == str(native.resolve())


def test_earlier_direct_executable_precedes_later_windows_npm_shim(tmp_path: Path) -> None:
    earlier = tmp_path / "earlier"
    earlier.mkdir()
    direct = earlier / "codex.exe"
    direct.write_bytes(b"direct native fixture")
    npm = tmp_path / "npm"
    native = (
        npm
        / "node_modules"
        / "@openai"
        / "codex"
        / "node_modules"
        / "@openai"
        / "codex-win32-x64"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / "bin"
        / "codex.exe"
    )
    native.parent.mkdir(parents=True)
    native.write_bytes(b"packaged native fixture")
    (npm / "codex.cmd").write_text("npm shim", encoding="utf-8")

    resolved = resolve_codex_executable(
        "codex",
        windows=True,
        search_path=os.pathsep.join((str(earlier), str(npm))),
        machine="AMD64",
    )

    assert resolved == str(direct.resolve())


@pytest.mark.parametrize(
    ("machine", "package", "target"),
    (
        ("x86_64", "codex-win32-x64", "x86_64-pc-windows-msvc"),
        ("ARM64", "codex-win32-arm64", "aarch64-pc-windows-msvc"),
        ("aarch64", "codex-win32-arm64", "aarch64-pc-windows-msvc"),
    ),
)
def test_windows_npm_resolution_supports_architecture_aliases(
    tmp_path: Path, machine: str, package: str, target: str
) -> None:
    native = (
        tmp_path
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
    native.parent.mkdir(parents=True)
    native.write_bytes(b"native fixture")
    (tmp_path / "codex.ps1").write_text("npm shim", encoding="utf-8")

    resolved = resolve_codex_executable(
        "codex", windows=True, search_path=str(tmp_path), machine=machine
    )

    assert resolved == str(native.resolve())


def test_windows_executable_resolution_rejects_symlink_candidate(tmp_path: Path) -> None:
    target = tmp_path / "native-target.exe"
    target.write_bytes(b"native fixture")
    try:
        (tmp_path / "codex.exe").symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    resolved = resolve_codex_executable(
        "codex", windows=True, search_path=str(tmp_path), machine="AMD64"
    )

    assert resolved == "codex"


def test_executable_resolution_preserves_non_windows_and_custom_selectors(tmp_path: Path) -> None:
    assert resolve_codex_executable("codex", windows=False) == "codex"
    assert (
        resolve_codex_executable(
            "company-codex", windows=True, search_path=str(tmp_path), machine="AMD64"
        )
        == "company-codex"
    )


def test_unresolved_windows_shim_fails_later_through_normal_start_diagnostic(
    tmp_path: Path,
) -> None:
    assert (
        resolve_codex_executable("codex", windows=True, search_path=str(tmp_path), machine="AMD64")
        == "codex"
    )
