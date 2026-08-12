"""Typed failures for advisory read-only agents."""


class AgentError(Exception):
    code = "agent_error"


class AgentContextError(AgentError):
    code = "agent_context_invalid"


class AgentInvocationError(AgentError):
    code = "agent_invocation_failed"


class AgentResponseError(AgentError):
    code = "agent_response_invalid"


class AgentResponseContractError(AgentResponseError):
    code = "agent_response_contract_invalid"


class AgentMutationError(AgentError):
    code = "agent_provider_mutated_repository"


class AgentEvidenceError(AgentError):
    code = "agent_analysis_evidence_mismatch"


class AgentScopeExpansionError(AgentResponseContractError):
    code = "agent_task_package_scope_expansion"


class AgentAnalysisDriftError(AgentError):
    code = "agent_analysis_baseline_drift"
