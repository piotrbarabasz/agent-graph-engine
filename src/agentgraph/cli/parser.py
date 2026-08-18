"""Argparse command surface for AgentGraph v1."""

from __future__ import annotations

import argparse
from importlib.metadata import PackageNotFoundError, version


def package_version() -> str:
    try:
        return version("agent-graph-engine")
    except PackageNotFoundError:
        return "0+uninstalled"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentgraph", description="Run AgentGraph safely")
    parser.add_argument("--version", action="version", version=f"%(prog)s {package_version()}")
    parser.add_argument("--repo", default=".", help="path inside the target Git repository")
    parser.add_argument("--home", help="external AgentGraph runtime home")
    parser.add_argument("--json", action="store_true", help="emit one JSON result object")
    parser.add_argument("--codex-executable", help="host-selected Codex executable")
    commands = parser.add_subparsers(dest="command", required=True)

    config = commands.add_parser("config", help="configuration operations")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("validate", help="validate the canonical repository config")

    run = commands.add_parser("run", help="start a new scope run")
    selector = run.add_mutually_exclusive_group()
    selector.add_argument("--scope", dest="scope_id")
    selector.add_argument("--parent-scope", dest="parent_scope_id")

    resume = commands.add_parser("resume", help="continue an existing durable run")
    resume.add_argument("run_id", nargs="?")

    status = commands.add_parser("status", help="read durable run status")
    status.add_argument("run_id", nargs="?")

    checkpoint = commands.add_parser("checkpoint", help="inspect or decide a checkpoint")
    checkpoint_commands = checkpoint.add_subparsers(dest="checkpoint_command", required=True)
    show = checkpoint_commands.add_parser("show", help="show a materialized checkpoint")
    show.add_argument("run_id", nargs="?")
    for name in ("approve", "reject", "cancel"):
        decision = checkpoint_commands.add_parser(name, help=f"record checkpoint {name}")
        decision.add_argument("run_id", nargs="?")
        decision.add_argument("--actor", required=True)
    return parser
