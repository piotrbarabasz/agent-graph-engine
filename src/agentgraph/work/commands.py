"""Platform-independent shell-free parsing of declarative validation commands."""

from __future__ import annotations

import shlex

from .errors import WorkSourceFormatError
from .models import SourceLocation, ValidationCheck, ValidationOrigin

_FORBIDDEN_GIT_OPERATIONS = {
    "push",
    "fetch",
    "pull",
    "merge",
    "rebase",
    "reset",
    "clean",
    "stash",
    "commit",
    "add",
    "checkout",
    "switch",
    "cherry-pick",
    "tag",
}
_FORBIDDEN_TEXT = ("&&", "||", "|", ">", "<", "`", "\n", "\r")


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
    segments = _split_segments(raw)
    checks = []
    for segment in segments:
        try:
            argv = tuple(shlex.split(segment, posix=True))
        except ValueError as exc:
            raise WorkSourceFormatError("validation command contains invalid quoting") from exc
        if not argv:
            raise WorkSourceFormatError("validation command segment is empty")
        if argv[0].casefold() == "git" and any(
            argument.casefold() in _FORBIDDEN_GIT_OPERATIONS for argument in argv[1:]
        ):
            raise WorkSourceFormatError("destructive or network Git validation is forbidden")
        checks.append(ValidationCheck(argv, segment, origin, source_location))
    return tuple(checks)


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
