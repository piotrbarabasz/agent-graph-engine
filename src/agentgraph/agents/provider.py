"""Provider boundary for advisory repository analysis."""

from dataclasses import dataclass
from typing import Protocol

from agentgraph.runtime.codec import sha256_digest

from .models import AgentContext, AgentRequest, AgentResponse


class AgentProvider(Protocol):
    def invoke(self, request: AgentRequest, context: AgentContext) -> AgentResponse:
        """Return one schema-validated response without write authority."""


@dataclass(slots=True)
class DeclaredWorkAgentProvider:
    """Compatibility provider preserving the pre-M008 deterministic bridge."""

    def invoke(self, request: AgentRequest, context: AgentContext) -> AgentResponse:
        del context
        if request.operation_id == "explore":
            payload = {
                "schema_version": 1,
                "status": "success",
                "relevant_files": [],
                "architecture_observations": [],
                "derived_requirements": [],
                "derived_acceptance_criteria": [],
                "derived_constraints": [],
                "architecture_invariants": [],
                "uncertainties": [],
                "reason_code": None,
                "message": None,
            }
        elif request.operation_id == "build_task_package":
            payload = {
                "schema_version": 1,
                "status": "success",
                "objective": "Implement the selected declared work item.",
                "implementation_steps": ["Apply the smallest change within declared capability."],
                "recommended_change_paths": [],
                "supporting_read_paths": [],
                "validation_focus": [],
                "assumptions": [],
                "unresolved_questions": [],
                "reason_code": None,
                "message": None,
            }
        elif request.operation_id == "assess_risk":
            payload = {
                "schema_version": 1,
                "status": "success",
                "risk_level": "low",
                "reasons": ["No additional risk beyond the authoritative source declaration."],
                "sensitive_areas": [],
                "destructive_change_concerns": [],
                "requests_human_checkpoint": False,
                "reason_code": None,
                "message": None,
            }
        elif request.operation_id == "classify_failure":
            payload = {
                "schema_version": 1,
                "status": "success",
                "classification": "debugger",
                "rationale": "Validation evidence indicates a bounded implementation defect.",
                "signals": ["validation_failed"],
                "reason_code": None,
                "message": None,
            }
        else:
            raise ValueError("unsupported declared-work agent operation")
        return AgentResponse(
            payload,
            "declared-work",
            "1",
            None,
            request.input_digest,
            sha256_digest(payload),
            "host-response.json",
        )

    evidence_namespace = "declared-work"
