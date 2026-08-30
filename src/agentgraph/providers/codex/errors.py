"""Typed failures for the proposal-only Codex provider."""

from agentgraph.write.errors import ChangeProviderBlockedError, WriteSliceError


class CodexProviderError(WriteSliceError):
    code = "codex_provider_failed"


class CodexCliUnavailableError(CodexProviderError):
    code = "codex_cli_unavailable"


class CodexCliProbeError(CodexProviderError):
    code = "codex_cli_probe_failed"


class CodexCliUnsupportedError(ChangeProviderBlockedError):
    code = "codex_cli_unsupported"

    def __init__(self, message: str) -> None:
        super().__init__(self.code, message)


class CodexPermissionProfileUnsupportedError(CodexCliUnsupportedError):
    code = "codex_permission_profile_unsupported"


class CodexInvocationError(CodexProviderError):
    code = "codex_invocation_failed"


class CodexTimeoutError(CodexInvocationError):
    code = "codex_timeout"


class CodexOutputSchemaRejectedError(CodexInvocationError):
    code = "codex_output_schema_rejected"


class CodexSchemaProjectionError(CodexProviderError):
    code = "codex_output_schema_incompatible"


class CodexResponseError(CodexProviderError):
    code = "codex_response_invalid"


class CodexProposalError(CodexProviderError):
    code = "codex_proposal_invalid"


class CodexProviderContextError(CodexProviderError):
    code = "codex_provider_context_invalid"


class CodexProviderBlockedError(ChangeProviderBlockedError):
    code = "codex_proposal_blocked"
