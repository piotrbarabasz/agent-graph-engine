"""Executable AgentGraph v1 CLI."""

from __future__ import annotations

import sys

from agentgraph.config import ConfigError
from agentgraph.core import CheckpointOutcome
from agentgraph.infra.errors import AgentGraphInfraError
from agentgraph.runtime.errors import AgentGraphRuntimeError, RuntimePathError
from agentgraph.write import WriteSliceOutcome

from .application import build_application
from .errors import CliError
from .models import CliErrorView, CliResultV1
from .output import from_status, from_write_report, render_human, render_json, with_profile
from .parser import build_parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = _command_name(args)
    try:
        app = build_application(
            args.repo,
            runtime_home=args.home,
            codex_executable=args.codex_executable,
        )
        result = _execute(app, args, command)
        code = _exit_code(result)
        _emit(result, json_mode=args.json, error=False)
        return code
    except ConfigError as exc:
        result = CliResultV1(
            command=command,
            ok=False,
            error=CliErrorView(exc.code, exc.message, None if exc.path is None else str(exc.path)),
        )
        _emit(result, json_mode=args.json, error=True)
        return 2
    except CliError as exc:
        result = CliResultV1(
            command=command,
            ok=False,
            error=CliErrorView(exc.code, exc.message),
        )
        _emit(result, json_mode=args.json, error=True)
        return (
            3
            if exc.code
            not in {"invalid_run_id", "codex_executable_invalid", "checkpoint_actor_invalid"}
            else 2
        )
    except (AgentGraphInfraError, RuntimePathError):
        result = CliResultV1(
            command=command,
            ok=False,
            error=CliErrorView(
                "environment_invalid",
                "repository or external runtime configuration is invalid",
            ),
        )
        _emit(result, json_mode=args.json, error=True)
        return 2
    except AgentGraphRuntimeError:
        result = CliResultV1(
            command=command,
            ok=False,
            error=CliErrorView("runtime_blocked", "durable runtime operation is blocked"),
        )
        _emit(result, json_mode=args.json, error=True)
        return 3
    except KeyboardInterrupt:
        result = CliResultV1(
            command=command,
            ok=False,
            error=CliErrorView("interrupted", "command interrupted"),
        )
        _emit(result, json_mode=args.json, error=True)
        return 130
    except Exception:
        result = CliResultV1(
            command=command,
            ok=False,
            error=CliErrorView("internal_error", "unexpected AgentGraph CLI failure"),
        )
        _emit(result, json_mode=args.json, error=True)
        return 5


def _execute(app, args, command: str) -> CliResultV1:
    if command == "config validate":
        publish = (
            f"github draft via {app.config.publish.remote}"
            if app.config.publish.enabled
            else "disabled"
        )
        return CliResultV1(
            command=command,
            ok=True,
            outcome="CONFIG_VALID",
            repository=str(app.repository_root),
            work_source=app.config.work.source,
            agent_provider=app.config.agents.provider,
            semantic_review=app.config.review.semantic,
            delivery_review=app.config.review.delivery,
            publish_description=publish,
            profile_digest=app.profile.digest,
        )
    if command == "run":
        return with_profile(
            from_write_report(command, app.run(args.scope_id, args.parent_scope_id)),
            app.profile.digest,
        )
    if command == "resume":
        return with_profile(from_write_report(command, app.resume(args.run_id)), app.profile.digest)
    if command == "status":
        return from_status(command, app.status(args.run_id))
    if command == "checkpoint show":
        run_id, checkpoint = app.show_checkpoint(args.run_id)
        return CliResultV1(
            command=command,
            ok=True,
            outcome="CHECKPOINT_PENDING",
            run_id=run_id,
            checkpoint=checkpoint,
            profile_digest=app.profile.digest,
        )
    outcomes = {
        "checkpoint approve": CheckpointOutcome.APPROVED,
        "checkpoint reject": CheckpointOutcome.REJECTED,
        "checkpoint cancel": CheckpointOutcome.CANCELLED,
    }
    outcome = outcomes[command]
    run_id, checkpoint = app.submit_checkpoint(args.run_id, outcome=outcome, actor=args.actor)
    return CliResultV1(
        command=command,
        ok=True,
        outcome="CHECKPOINT_DECISION_RECORDED",
        run_id=run_id,
        checkpoint=checkpoint,
        profile_digest=app.profile.digest,
        decision=outcome.value.upper(),
        actor=args.actor,
    )


def _command_name(args) -> str:
    if args.command == "config":
        return f"config {args.config_command}"
    if args.command == "checkpoint":
        return f"checkpoint {args.checkpoint_command}"
    return args.command


def _exit_code(result: CliResultV1) -> int:
    if result.error is not None:
        return 5
    if result.outcome in {
        WriteSliceOutcome.BLOCKED.name,
        WriteSliceOutcome.INVALID_SOURCE.name,
        WriteSliceOutcome.RECOVERY_REQUIRED.name,
        WriteSliceOutcome.PUBLISH_PREPARATION_BLOCKED.name,
    }:
        return 3
    if result.outcome == WriteSliceOutcome.FAILED.name:
        return 4
    return 0


def _emit(result: CliResultV1, *, json_mode: bool, error: bool) -> None:
    text = render_json(result) if json_mode else render_human(result)
    stream = sys.stdout if json_mode or not error else sys.stderr
    print(text, file=stream)


if __name__ == "__main__":
    raise SystemExit(main())
