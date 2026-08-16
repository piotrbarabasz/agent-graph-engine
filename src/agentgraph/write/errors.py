"""Typed failures at the controlled local-write boundary."""


class WriteSliceError(Exception):
    """Base class for M006 write-slice failures."""


class WritePreparationError(WriteSliceError):
    pass


class WorkItemPolicyError(WritePreparationError):
    code = "work_item_policy_mismatch"


class WorkPlanMismatchError(WritePreparationError):
    code = "work_plan_mismatch"


class WorkCapabilityMismatchError(WritePreparationError):
    pass


class UnsupportedWriteScopeError(WritePreparationError):
    pass


class ChangeSetError(WriteSliceError):
    pass


class ChangeProviderBlockedError(WriteSliceError):
    """Expected provider inability that blocks the graph without a retry."""

    code = "change_provider_blocked"

    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        self.message = message
        super().__init__(message)


class ProviderMutationError(WriteSliceError):
    code = "provider_mutated_repository"


class ChangePathError(ChangeSetError):
    pass


class StaleFileError(ChangeSetError):
    pass


class WorkspaceError(WriteSliceError):
    pass


class WriteBaselineDriftError(WriteSliceError):
    pass


class ValidationExecutionError(WriteSliceError):
    pass


class CommitVerificationError(WriteSliceError):
    pass


class PostCommitRecoveryRequired(WriteSliceError):
    """A commit exists or may exist, so normal node failure is unsafe."""


class RepairPolicyError(WritePreparationError):
    code = "repair_policy_invalid"


class RepairFailureContextError(WriteSliceError):
    code = "repair_failure_context_mismatch"


class RepairLineageError(WorkspaceError):
    code = "repair_lineage_mismatch"


class RepairWorkspaceLineageError(WorkspaceError):
    code = "repair_workspace_lineage_mismatch"


class WorkspaceManifestError(WorkspaceError):
    code = "workspace_manifest_invalid"


class SemanticReviewContextError(WorkspaceError):
    code = "semantic_review_context_mismatch"


class SemanticReviewEvidenceError(WorkspaceError):
    code = "semantic_review_evidence_mismatch"


class SemanticReviewBlockedError(WriteSliceError):
    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        self.message = message
        super().__init__(message)


class ReviewProviderRequiredError(WritePreparationError):
    code = "review_provider_required"


class CheckpointError(WriteSliceError):
    """A durable human checkpoint operation failed closed."""

    code = "checkpoint_error"

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class CheckpointBindingError(CheckpointError):
    pass
