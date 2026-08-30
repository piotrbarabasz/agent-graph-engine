from __future__ import annotations

from pathlib import Path

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
