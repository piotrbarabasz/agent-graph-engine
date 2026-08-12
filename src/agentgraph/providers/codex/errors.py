"""Typed failures for the proposal-only Codex provider."""

from agentgraph.write.errors import ChangeProviderBlockedError, WriteSliceError


class CodexProviderError(WriteSliceError):
    code = "codex_provider_failed"


class CodexCliUnavailableError(CodexProviderError):
    code = "codex_cli_unavailable"


class CodexCliUnsupportedError(ChangeProviderBlockedError):
    code = "codex_cli_unsupported"

    def __init__(self, message: str) -> None:
        super().__init__(self.code, message)


class CodexInvocationError(CodexProviderError):
    code = "codex_invocation_failed"


class CodexTimeoutError(CodexInvocationError):
    code = "codex_timeout"


class CodexResponseError(CodexProviderError):
    code = "codex_response_invalid"


class CodexProposalError(CodexProviderError):
    code = "codex_proposal_invalid"


class CodexProviderContextError(CodexProviderError):
    code = "codex_provider_context_invalid"


class CodexProviderBlockedError(ChangeProviderBlockedError):
    code = "codex_proposal_blocked"
