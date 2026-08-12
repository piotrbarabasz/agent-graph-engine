"""Machine-readable, local-only Git infrastructure built on ProcessRunner."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .errors import (
    GitCommandError,
    GitOutputError,
    GitPathError,
    GitUnavailableError,
    InvalidGitOperationError,
    InvalidGitReferenceError,
    NotAGitRepositoryError,
    NothingToCommitError,
    ProcessStartError,
)
from .process import CommandSpec, ProcessRunner
from .receipts import CommandReceipt, CommandResult, ProcessStatus


@dataclass(frozen=True, slots=True)
class GitRepository:
    """Canonical local Git worktree and repository metadata location."""

    root: Path
    git_dir: Path


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    """Immutable machine-readable state of one local repository."""

    root: Path
    head_sha: str | None
    branch: str | None
    detached_head: bool
    upstream: str | None
    staged_paths: tuple[Path, ...]
    unstaged_paths: tuple[Path, ...]
    untracked_paths: tuple[Path, ...]
    conflicted_paths: tuple[Path, ...]
    dirty: bool


@dataclass(frozen=True, slots=True)
class DiffCheckResult:
    """Whitespace-check outcomes for working-tree and staged diffs."""

    working_tree_ok: bool
    staged_ok: bool
    receipts: tuple[CommandReceipt, CommandReceipt]

    @property
    def ok(self) -> bool:
        return self.working_tree_ok and self.staged_ok


@dataclass(frozen=True, slots=True)
class GitCommitIdentity:
    """Invocation-local Git author/committer identity."""

    name: str
    email: str

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.email.strip() or "\x00" in self.name + self.email:
            raise InvalidGitOperationError("Git commit identity must be non-empty and NUL-free")


@dataclass(frozen=True, slots=True)
class GitCommitResult:
    """Successful local commit identity and its command receipt."""

    commit_sha: str
    receipt: CommandReceipt


@dataclass(frozen=True, slots=True)
class GitWorktreeResult:
    """A newly-created local worktree plus the structural command receipt."""

    repository: GitRepository
    receipt: CommandReceipt


@dataclass(frozen=True, slots=True)
class _StatusPaths:
    staged: tuple[Path, ...]
    unstaged: tuple[Path, ...]
    untracked: tuple[Path, ...]
    conflicted: tuple[Path, ...]


class GitAdapter:
    """Explicit non-destructive local Git operations over ProcessRunner only."""

    def __init__(
        self,
        runner: ProcessRunner | None = None,
        *,
        executable: str = "git",
        timeout_seconds: float = 30.0,
        commit_timeout_seconds: float = 120.0,
    ) -> None:
        if not executable or "\x00" in executable:
            raise InvalidGitOperationError("Git executable must be non-empty and NUL-free")
        if timeout_seconds <= 0 or commit_timeout_seconds <= 0:
            raise InvalidGitOperationError("Git command timeouts must be positive")
        self.runner = runner or ProcessRunner()
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.commit_timeout_seconds = commit_timeout_seconds

    def discover_repository(self, path: Path | str) -> GitRepository:
        """Discover a canonical repository without creating or mutating it."""

        candidate = Path(path).expanduser()
        if not candidate.exists():
            raise NotAGitRepositoryError("repository discovery path does not exist")
        cwd = candidate if candidate.is_dir() else candidate.parent
        root_result = self._execute_at(cwd, ("rev-parse", "--show-toplevel"))
        if root_result.receipt.status is ProcessStatus.FAILED:
            raise NotAGitRepositoryError("path is not inside a Git repository")
        self._require_success(root_result, "Git repository discovery failed")
        root = Path(self._single_path(root_result)).resolve()
        git_dir_result = self._execute_at(root, ("rev-parse", "--absolute-git-dir"))
        self._require_success(git_dir_result, "Git directory discovery failed")
        git_dir = Path(self._single_path(git_dir_result)).resolve()
        return GitRepository(root, git_dir)

    def snapshot(self, repository: GitRepository) -> RepositorySnapshot:
        """Read repository identity and porcelain-v2 status without hidden mutations."""

        head_result = self._run(repository, ("rev-parse", "--verify", "HEAD"))
        if head_result.receipt.status is ProcessStatus.SUCCEEDED:
            head_sha = self._single_text(head_result)
        elif head_result.receipt.status is ProcessStatus.FAILED:
            head_sha = None
        else:
            self._require_success(head_result, "Git HEAD inspection failed")
            raise AssertionError("unreachable")

        branch_result = self._run(repository, ("symbolic-ref", "--quiet", "--short", "HEAD"))
        if branch_result.receipt.status is ProcessStatus.SUCCEEDED:
            branch = self._single_text(branch_result)
        elif branch_result.receipt.status is ProcessStatus.FAILED:
            branch = None
        else:
            self._require_success(branch_result, "Git branch inspection failed")
            raise AssertionError("unreachable")
        if head_sha is None and branch is None:
            raise GitCommandError("Git HEAD inspection failed", head_result.receipt)

        upstream_result = self._run(
            repository,
            ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"),
        )
        if upstream_result.receipt.status is ProcessStatus.SUCCEEDED:
            upstream = self._single_text(upstream_result)
        elif upstream_result.receipt.status is ProcessStatus.FAILED:
            upstream = None
        else:
            self._require_success(upstream_result, "Git upstream inspection failed")
            raise AssertionError("unreachable")

        status_result = self._run(
            repository,
            ("status", "--porcelain=v2", "-z", "--untracked-files=all"),
        )
        self._require_success(status_result, "Git status inspection failed")
        paths = parse_porcelain_v2(status_result.stdout)
        dirty = any((paths.staged, paths.unstaged, paths.untracked, paths.conflicted))
        return RepositorySnapshot(
            root=repository.root,
            head_sha=head_sha,
            branch=branch,
            detached_head=branch is None and head_sha is not None,
            upstream=upstream,
            staged_paths=paths.staged,
            unstaged_paths=paths.unstaged,
            untracked_paths=paths.untracked,
            conflicted_paths=paths.conflicted,
            dirty=dirty,
        )

    def unstaged_diff_paths(self, repository: GitRepository) -> tuple[Path, ...]:
        return self._diff_paths(repository, cached=False)

    def staged_diff_paths(self, repository: GitRepository) -> tuple[Path, ...]:
        return self._diff_paths(repository, cached=True)

    def diff_check(self, repository: GitRepository) -> DiffCheckResult:
        """Check whitespace errors independently in unstaged and staged diffs."""

        working = self._run(
            repository,
            ("--no-pager", "diff", "--no-ext-diff", "--no-textconv", "--check"),
        )
        staged = self._run(
            repository,
            (
                "--no-pager",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--cached",
                "--check",
            ),
        )
        for result in (working, staged):
            if result.receipt.status in {ProcessStatus.TIMED_OUT, ProcessStatus.CANCELLED}:
                self._require_success(result, "Git diff check did not complete")
        return DiffCheckResult(
            working.receipt.exit_code == 0,
            staged.receipt.exit_code == 0,
            (working.receipt, staged.receipt),
        )

    def create_branch(
        self,
        repository: GitRepository,
        name: str,
        start_point: str | None = None,
    ) -> CommandReceipt:
        self._validate_branch(repository, name)
        arguments = ["switch", "-c", name]
        if start_point is not None:
            self._validate_start_point(repository, start_point)
            arguments.append(start_point)
        result = self._run(repository, tuple(arguments))
        self._require_success(result, "Git branch creation failed")
        return result.receipt

    def switch_branch(self, repository: GitRepository, name: str) -> CommandReceipt:
        self._validate_branch(repository, name)
        result = self._run(repository, ("switch", name))
        self._require_success(result, "Git branch switch failed")
        return result.receipt

    def local_branch_exists(self, repository: GitRepository, name: str) -> bool:
        """Return whether an exact local branch ref exists, without remote lookup."""

        self._validate_branch(repository, name)
        result = self._run(repository, ("show-ref", "--verify", "--quiet", f"refs/heads/{name}"))
        if result.receipt.status is ProcessStatus.SUCCEEDED:
            return True
        if result.receipt.status is ProcessStatus.FAILED and result.receipt.exit_code == 1:
            return False
        self._require_success(result, "Git local branch inspection failed")
        raise AssertionError("unreachable")

    def resolve_ref(self, repository: GitRepository, reference: str) -> str | None:
        """Resolve an exact local commit-ish to a commit SHA, or return None."""

        if (
            not isinstance(reference, str)
            or not reference
            or reference.startswith("-")
            or "\x00" in reference
        ):
            raise InvalidGitReferenceError("invalid Git reference")
        result = self._run(
            repository, ("rev-parse", "--verify", "--quiet", f"{reference}^{{commit}}")
        )
        if result.receipt.status is ProcessStatus.SUCCEEDED:
            return self._single_text(result)
        if result.receipt.status is ProcessStatus.FAILED:
            return None
        self._require_success(result, "Git reference resolution failed")
        raise AssertionError("unreachable")

    def add_worktree(
        self,
        repository: GitRepository,
        workspace: Path | str,
        branch: str,
        start_sha: str,
    ) -> GitWorktreeResult:
        """Create one new branch in a new local worktree from a pinned commit."""

        self._validate_branch(repository, branch)
        self._validate_start_point(repository, start_sha)
        destination = Path(workspace).expanduser().resolve()
        if destination.exists() or destination.is_symlink():
            raise GitPathError("worktree destination already exists")
        if self.local_branch_exists(repository, branch):
            raise InvalidGitOperationError("local branch already exists")
        result = self._run(
            repository,
            ("worktree", "add", "-b", branch, os.fspath(destination), start_sha),
            timeout_seconds=self.commit_timeout_seconds,
        )
        self._require_success(result, "Git worktree creation failed")
        return GitWorktreeResult(self.discover_repository(destination), result.receipt)

    def stage_paths(self, repository: GitRepository, paths: Iterable[Path | str]) -> CommandReceipt:
        relative_paths = self._normalize_paths(repository, paths)
        if not relative_paths:
            raise GitPathError("stage_paths requires at least one explicit path")
        result = self._run(
            repository,
            (
                "--literal-pathspecs",
                "add",
                "--",
                *(path.as_posix() for path in relative_paths),
            ),
        )
        self._require_success(result, "Git staging failed")
        return result.receipt

    def commit(
        self,
        repository: GitRepository,
        message: str,
        *,
        expected_paths: Iterable[Path | str] | None = None,
        identity: GitCommitIdentity | None = None,
    ) -> GitCommitResult:
        if not isinstance(message, str) or not message.strip() or "\x00" in message:
            raise InvalidGitOperationError("commit message must be non-empty and NUL-free")
        staged = self.staged_diff_paths(repository)
        if not staged:
            raise NothingToCommitError("no staged changes to commit")
        if expected_paths is not None:
            expected = self._normalize_paths(repository, expected_paths)
            if set(expected) != set(staged):
                raise GitPathError("staged paths do not match expected_paths")
        arguments: list[str] = []
        if identity is not None:
            arguments.extend(("-c", f"user.name={identity.name}"))
            arguments.extend(("-c", f"user.email={identity.email}"))
        arguments.extend(("commit", "-m", message))
        result = self._run(
            repository,
            tuple(arguments),
            timeout_seconds=self.commit_timeout_seconds,
        )
        self._require_success(result, "Git commit failed")
        head = self._run(repository, ("rev-parse", "--verify", "HEAD"))
        self._require_success(head, "Git commit identity lookup failed")
        return GitCommitResult(self._single_text(head), result.receipt)

    def _validate_branch(self, repository: GitRepository, name: str) -> None:
        if not isinstance(name, str) or not name or name.startswith("-") or "\x00" in name:
            raise InvalidGitReferenceError("invalid Git branch name")
        result = self._run(repository, ("check-ref-format", "--branch", name))
        if result.receipt.status is ProcessStatus.FAILED:
            raise InvalidGitReferenceError("invalid Git branch name")
        self._require_success(result, "Git branch validation did not complete")

    def _validate_start_point(self, repository: GitRepository, start_point: str) -> None:
        if (
            not isinstance(start_point, str)
            or not start_point
            or start_point.startswith("-")
            or "\x00" in start_point
        ):
            raise InvalidGitReferenceError("invalid Git start point")
        result = self._run(
            repository,
            ("rev-parse", "--verify", "--quiet", f"{start_point}^{{commit}}"),
        )
        if result.receipt.status is ProcessStatus.FAILED:
            raise InvalidGitReferenceError("invalid Git start point")
        self._require_success(result, "Git start-point validation did not complete")

    def _normalize_paths(
        self, repository: GitRepository, paths: Iterable[Path | str]
    ) -> tuple[Path, ...]:
        if isinstance(paths, (str, Path)):
            raise GitPathError("paths must be an iterable of explicit path values")
        try:
            requested = tuple(paths)
        except TypeError as exc:
            raise GitPathError("paths must be an iterable") from exc
        root = repository.root.resolve()
        normalized = []
        for value in requested:
            if not isinstance(value, (str, Path)) or not os.fspath(value):
                raise GitPathError("Git paths must be non-empty strings or Paths")
            raw = Path(value)
            candidate = (raw if raw.is_absolute() else root / raw).resolve()
            try:
                relative = candidate.relative_to(root)
            except ValueError as exc:
                raise GitPathError("Git path escapes repository root") from exc
            if relative == Path("."):
                raise GitPathError("repository root cannot be used as an implicit stage-all path")
            normalized.append(relative)
        return _sorted_paths(normalized)

    def _diff_paths(self, repository: GitRepository, *, cached: bool) -> tuple[Path, ...]:
        arguments = ["--no-pager", "diff", "--no-ext-diff", "--no-textconv"]
        if cached:
            arguments.append("--cached")
        arguments.extend(("--name-only", "-z"))
        result = self._run(repository, tuple(arguments))
        self._require_success(result, "Git diff path inspection failed")
        return _parse_nul_paths(result.stdout)

    def _run(
        self,
        repository: GitRepository,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        return self._execute_at(repository.root, arguments, timeout_seconds=timeout_seconds)

    def _execute_at(
        self,
        cwd: Path,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        spec = CommandSpec(
            argv=(self.executable, *arguments),
            cwd=cwd,
            timeout_seconds=timeout_seconds or self.timeout_seconds,
            env={"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "LANG": "C"},
            unset_env=tuple(
                sorted(
                    (key for key in os.environ if key.upper().startswith("GIT_")),
                    key=lambda key: (key.upper(), key),
                )
            ),
        )
        try:
            return self.runner.run(spec)
        except ProcessStartError as exc:
            raise GitUnavailableError("configured Git executable is unavailable") from exc

    @staticmethod
    def _require_success(result: CommandResult, message: str) -> None:
        if result.receipt.stdout_truncated or result.receipt.stderr_truncated:
            raise GitOutputError(f"{message}: command output was truncated")
        if result.receipt.status is not ProcessStatus.SUCCEEDED:
            raise GitCommandError(message, result.receipt)

    @staticmethod
    def _single_text(result: CommandResult) -> str:
        if result.receipt.stdout_truncated:
            raise GitOutputError("Git output was truncated")
        value = result.stdout.rstrip(b"\r\n")
        if not value or b"\n" in value or b"\r" in value:
            raise GitOutputError("Git returned an invalid single-line value")
        return value.decode("utf-8", errors="surrogateescape")

    @staticmethod
    def _single_path(result: CommandResult) -> str:
        if result.receipt.stdout_truncated:
            raise GitOutputError("Git path output was truncated")
        value = result.stdout.rstrip(b"\r\n")
        if not value or b"\n" in value or b"\r" in value:
            raise GitOutputError("Git returned an invalid path value")
        return os.fsdecode(value)


def parse_porcelain_v2(output: bytes) -> _StatusPaths:
    """Parse NUL-delimited porcelain-v2 records without whitespace path splitting."""

    if output and not output.endswith(b"\0"):
        raise GitOutputError("porcelain-v2 output is not NUL terminated")
    records = output.split(b"\0")[:-1] if output else []
    staged: list[Path] = []
    unstaged: list[Path] = []
    untracked: list[Path] = []
    conflicted: list[Path] = []
    index = 0
    while index < len(records):
        record = records[index]
        if record.startswith(b"1 "):
            parts = record.split(b" ", 8)
            if len(parts) != 9:
                raise GitOutputError("malformed ordinary porcelain-v2 record")
            _classify(parts[1], Path(os.fsdecode(parts[8])), staged, unstaged)
        elif record.startswith(b"2 "):
            parts = record.split(b" ", 9)
            if len(parts) != 10 or index + 1 >= len(records):
                raise GitOutputError("malformed rename porcelain-v2 record")
            _classify(parts[1], Path(os.fsdecode(parts[9])), staged, unstaged)
            index += 1  # Original path is the following NUL-delimited field.
        elif record.startswith(b"u "):
            parts = record.split(b" ", 10)
            if len(parts) != 11:
                raise GitOutputError("malformed unmerged porcelain-v2 record")
            conflicted.append(Path(os.fsdecode(parts[10])))
        elif record.startswith(b"? "):
            untracked.append(Path(os.fsdecode(record[2:])))
        elif record.startswith(b"! "):
            pass
        else:
            raise GitOutputError("unknown porcelain-v2 record type")
        index += 1
    return _StatusPaths(
        _sorted_paths(staged),
        _sorted_paths(unstaged),
        _sorted_paths(untracked),
        _sorted_paths(conflicted),
    )


def _classify(
    xy: bytes,
    path: Path,
    staged: list[Path],
    unstaged: list[Path],
) -> None:
    if len(xy) != 2:
        raise GitOutputError("invalid porcelain-v2 XY status")
    if xy[:1] != b".":
        staged.append(path)
    if xy[1:] != b".":
        unstaged.append(path)


def _parse_nul_paths(output: bytes) -> tuple[Path, ...]:
    if not output:
        return ()
    if not output.endswith(b"\0"):
        raise GitOutputError("Git path output is not NUL terminated")
    return _sorted_paths(Path(os.fsdecode(value)) for value in output.split(b"\0")[:-1])


def _sorted_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    return tuple(sorted(set(paths), key=lambda value: os.fsencode(value)))
