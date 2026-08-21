from __future__ import annotations

import pytest

from agentgraph.cli.parser import build_parser


def test_run_selectors_are_mutually_exclusive() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--scope", "E001", "--parent-scope", "P001"])


@pytest.mark.parametrize(
    "arguments",
    [
        ["config", "validate"],
        ["run", "--scope", "E001"],
        ["resume"],
        ["status", "run_0123456789ABCDEFGHJKMNPQRS"],
        ["checkpoint", "show"],
        ["checkpoint", "approve", "--actor", "Piotr"],
        ["checkpoint", "reject", "--actor", "Piotr"],
        ["checkpoint", "cancel", "--actor", "Piotr"],
    ],
)
def test_required_commands_parse(arguments: list[str]) -> None:
    assert build_parser().parse_args(arguments).command


def test_actor_is_required() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["checkpoint", "approve"])
