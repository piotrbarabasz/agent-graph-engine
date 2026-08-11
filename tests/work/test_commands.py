from __future__ import annotations

import pytest

from agentgraph.work import (
    SourceLocation,
    ValidationOrigin,
    WorkSourceFormatError,
    parse_validation_checks,
)


def parse(raw: str):
    return parse_validation_checks(
        raw,
        origin=ValidationOrigin.ITEM,
        source_location=SourceLocation("source/tasks.md", 10),
    )


def test_semicolon_is_declarative_separator_and_quotes_are_platform_independent() -> None:
    checks = parse('python -m pytest "tests/a file.py"; git --no-pager diff --check')

    assert tuple(check.argv for check in checks) == (
        ("python", "-m", "pytest", "tests/a file.py"),
        ("git", "--no-pager", "diff", "--check"),
    )
    assert checks[0].raw == 'python -m pytest "tests/a file.py"'


@pytest.mark.parametrize(
    "raw",
    [
        "python -m pytest && echo bad",
        "python -m pytest || echo bad",
        "python -m pytest | tool",
        "tool > output",
        "tool < input",
        "`tool`",
        "tool\nother",
        "tool; ; other",
        'tool "unterminated',
    ],
)
def test_shell_operators_empty_segments_and_invalid_quotes_are_rejected(raw: str) -> None:
    with pytest.raises(WorkSourceFormatError):
        parse(raw)


@pytest.mark.parametrize(
    "operation",
    [
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
    ],
)
def test_mutating_or_network_git_validation_commands_are_rejected(operation: str) -> None:
    with pytest.raises(WorkSourceFormatError):
        parse(f"git {operation}")


def test_parser_only_returns_argv_and_never_executes(tmp_path) -> None:
    marker = tmp_path / "marker"
    checks = parse(f"python -c open({str(marker)!r},'w')")

    assert checks[0].argv[0] == "python"
    assert not marker.exists()
