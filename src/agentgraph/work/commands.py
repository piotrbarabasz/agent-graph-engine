"""Platform-independent shell-free parsing of declarative validation commands."""

from __future__ import annotations

import shlex

from .errors import WorkSourceFormatError
from .models import SourceLocation, ValidationCheck, ValidationOrigin

_FORBIDDEN_TEXT = ("&&", "||", "|", ">", "<", "\n", "\r")
_SAFE_GIT_GLOBAL_OPTIONS = {"--no-pager"}
_SAFE_GIT_SUBCOMMANDS = {"diff", "ls-files", "rev-parse", "status"}
_FORBIDDEN_GIT_DIFF_OPTIONS = {"--ext-diff", "--textconv"}
_SAFE_GIT_DIFF_OPTIONS = {"--check", "--no-ext-diff", "--no-textconv"}


def parse_validation_checks(
    raw: str,
    *,
    origin: ValidationOrigin,
    source_location: SourceLocation,
) -> tuple[ValidationCheck, ...]:
    """Parse semicolon-separated command declarations with POSIX shlex syntax."""

    if not isinstance(raw, str) or not raw.strip():
        raise WorkSourceFormatError("validation command declaration is empty")
    if any(token in raw for token in _FORBIDDEN_TEXT):
        raise WorkSourceFormatError("validation command contains a forbidden shell operator")
    declaration = _strip_markdown_code_span(raw.strip())
    segments = _split_segments(declaration)
    checks = []
    for segment in segments:
        segment = _strip_markdown_code_span(segment)
        if "`" in segment:
            raise WorkSourceFormatError("validation command contains a forbidden backtick")
        try:
            argv = tuple(shlex.split(segment, posix=True))
        except ValueError as exc:
            raise WorkSourceFormatError("validation command contains invalid quoting") from exc
        if not argv:
            raise WorkSourceFormatError("validation command segment is empty")
        if argv[0].casefold() in {"git", "git.exe"}:
            _validate_git_command(argv)
        checks.append(ValidationCheck(argv, segment, origin, source_location))
    return tuple(checks)


def _strip_markdown_code_span(value: str) -> str:
    """Remove exactly one outer Markdown code span, never embedded backticks."""

    stripped = value.strip()
    if stripped.count("`") == 2 and stripped.startswith("`") and stripped.endswith("`"):
        stripped = stripped[1:-1].strip()
    if not stripped:
        raise WorkSourceFormatError("validation command segment is empty")
    return stripped


def _validate_git_command(argv: tuple[str, ...]) -> None:
    """Allow only a small read-only Git command grammar without config or alias hooks."""

    position = 1
    while position < len(argv) and argv[position].casefold() in _SAFE_GIT_GLOBAL_OPTIONS:
        position += 1
    if position >= len(argv):
        raise WorkSourceFormatError("Git validation command requires a safe subcommand")
    subcommand = argv[position].casefold()
    if subcommand not in _SAFE_GIT_SUBCOMMANDS:
        raise WorkSourceFormatError("Git validation subcommand is not allowlisted")
    if subcommand == "diff":
        for argument in argv[position + 1 :]:
            option = argument.casefold()
            if option in _FORBIDDEN_GIT_DIFF_OPTIONS or any(
                option.startswith(f"{forbidden}=") for forbidden in _FORBIDDEN_GIT_DIFF_OPTIONS
            ):
                raise WorkSourceFormatError("Git diff execution hooks are forbidden")
            if option not in _SAFE_GIT_DIFF_OPTIONS:
                raise WorkSourceFormatError("Git diff validation option is not allowlisted")


def _split_segments(raw: str) -> tuple[str, ...]:
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    for character in raw:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\" and quote != "'":
            current.append(character)
            escaped = True
        elif character in {'"', "'"}:
            current.append(character)
            quote = None if quote == character else character if quote is None else quote
        elif character == ";" and quote is None:
            segment = "".join(current).strip()
            if not segment:
                raise WorkSourceFormatError("validation command segment is empty")
            segments.append(segment)
            current = []
        else:
            current.append(character)
    segment = "".join(current).strip()
    if not segment:
        raise WorkSourceFormatError("validation command segment is empty")
    segments.append(segment)
    return tuple(segments)
