"""Neutral read-only AgentProvider backed by the shared Codex runtime."""

from __future__ import annotations

from agentgraph.agents import (
    AgentContext,
    AgentProvider,
    AgentRequest,
    AgentResponse,
    parse_explore_payload,
    parse_risk_payload,
    parse_task_package_payload,
)
from agentgraph.infra import CancellationToken, ProcessRunner
from agentgraph.runtime.codec import encode_value, parse_json_bytes, sha256_digest
from agentgraph.write.evidence import write_evidence

from .config import CodexProviderConfig
from .errors import CodexResponseError
from .runtime import CodexInvocationRuntime


class CodexAgentProvider(AgentProvider):
    """Invoke Codex once and expose only validated structured advisory data."""

    def __init__(
        self,
        runtime: CodexInvocationRuntime | None = None,
        *,
        process_runner: ProcessRunner | None = None,
        config: CodexProviderConfig | None = None,
        cancellation: CancellationToken | None = None,
    ) -> None:
        if runtime is not None and any(
            value is not None for value in (process_runner, config, cancellation)
        ):
            raise ValueError("explicit Codex runtime cannot be combined with runtime settings")
        if runtime is None:
            runner = process_runner or ProcessRunner()
            runtime = CodexInvocationRuntime(
                runner, config or CodexProviderConfig(), cancellation=cancellation
            )
        self.runtime = runtime

    def invoke(self, request: AgentRequest, context: AgentContext) -> AgentResponse:
        evidence_context = {
            "project_id": context.project_id,
            "run_id": context.run_id,
            "node_id": context.node_id,
            "node_attempt_id": context.node_attempt_id,
            "provider_invocation_id": context.provider_invocation_id,
            "provider": "codex",
            "model": self.runtime.config.model,
            "input_digest": request.input_digest,
            "source_revision": context.source_revision,
            "baseline_head": context.baseline_head,
        }
        result = self.runtime.invoke_structured(
            prompt=request.prompt.encode("utf-8"),
            schema=request.output_schema,
            repository_root=context.repository_root,
            artifact_directory=context.runtime_directory,
            evidence_context=evidence_context,
            receipt_name="receipt.json",
        )
        try:
            document = parse_json_bytes(result.raw)
        except Exception as exc:
            raise CodexResponseError("Codex agent response is not strict JSON") from exc
        parser = {
            "agentgraph.explore.v1": parse_explore_payload,
            "agentgraph.task-package.v1": parse_task_package_payload,
            "agentgraph.risk.v1": parse_risk_payload,
        }.get(request.output_schema_id)
        if parser is None:
            raise ValueError("unsupported Codex agent schema ID")
        try:
            parsed = parser(document)
        except Exception as exc:
            raise CodexResponseError("Codex agent response violates its schema") from exc
        payload = encode_value(parsed)
        output_digest = sha256_digest(payload)
        write_evidence(
            context.runtime_directory / "response.json",
            context={
                **evidence_context,
                "provider_version": result.capabilities.version,
                "output_digest": output_digest,
                "raw_output_digest": result.output_digest,
            },
            payload={"response": payload, "command_receipt": "receipt.json"},
        )
        return AgentResponse(
            payload,
            "codex",
            result.capabilities.version,
            self.runtime.config.model,
            request.input_digest,
            output_digest,
            "response.json",
            encode_value(result.receipt),
        )

    evidence_namespace = "codex"
