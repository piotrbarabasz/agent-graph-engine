"""Repository containment for source layout and declared work paths."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from agentgraph.work import RepoPathSpec, WorkSourceConfigurationError, WorkSourcePathError


@dataclass(frozen=True, slots=True)
class SpecKitLayout:
    repository_root: Path
    workstreams_dir: str = ".specify/workstreams"
    active_scope_file: str | None = ".specify/runtime/active-epic"

    def __post_init__(self) -> None:
        root = Path(self.repository_root).expanduser().resolve()
        if not root.is_dir():
            raise WorkSourceConfigurationError("repository_root must exist and be a directory")
        object.__setattr__(self, "repository_root", root)
        _, workstreams = resolve_repository_path(root, self.workstreams_dir)
        object.__setattr__(self, "workstreams_dir", workstreams)
        if self.active_scope_file is not None:
            _, active = resolve_repository_path(root, self.active_scope_file)
            object.__setattr__(self, "active_scope_file", active)

    @property
    def workstreams_path(self) -> Path:
        return resolve_repository_path(self.repository_root, self.workstreams_dir)[0]

    @property
    def active_scope_path(self) -> Path | None:
        if self.active_scope_file is None:
            return None
        return resolve_repository_path(self.repository_root, self.active_scope_file)[0]


def resolve_repository_path(root: Path, declared: str) -> tuple[Path, str]:
    """Return a contained resolved path and canonical repository-relative POSIX form."""

    normalized = _normalize_lexical_path(declared)
    candidate = (root / Path(*PurePosixPath(normalized).parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise WorkSourcePathError("source path escapes repository root") from exc
    return candidate, normalized


def parse_repo_path_spec(root: Path, declared: str) -> RepoPathSpec:
    """Parse a possibly non-existing declared path with fail-closed symlink containment."""

    if not isinstance(declared, str):
        raise WorkSourcePathError("declared repository path must be a string")
    value = declared.strip()
    if len(value) >= 2 and value.startswith("`") and value.endswith("`"):
        value = value[1:-1].strip()
    directory_hint = value.endswith(("/", "\\"))
    _, normalized = resolve_repository_path(root, value)
    return RepoPathSpec(normalized, directory_hint)


def parse_repo_path_list(root: Path, raw: str) -> tuple[RepoPathSpec, ...]:
    if raw.strip().casefold() in {"none", "n/a", "na", "[]"}:
        return ()
    values = tuple(part.strip() for part in raw.split(","))
    if any(not value for value in values):
        raise WorkSourcePathError("declared repository path list contains an empty value")
    return tuple(parse_repo_path_spec(root, value) for value in values)


def _normalize_lexical_path(declared: str) -> str:
    if not isinstance(declared, str) or not declared.strip() or "\x00" in declared:
        raise WorkSourcePathError("repository-relative path is empty or contains NUL")
    value = declared.strip()
    windows = PureWindowsPath(value)
    if windows.is_absolute() or windows.drive or value.startswith(("/", "\\", "//")):
        raise WorkSourcePathError("absolute repository paths are forbidden")
    value = value.replace("\\", "/")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts:
        raise WorkSourcePathError("repository path traversal is forbidden")
    parts = tuple(part for part in pure.parts if part not in {"", "."})
    if not parts:
        raise WorkSourcePathError("repository-relative path is empty")
    return PurePosixPath(*parts).as_posix()
