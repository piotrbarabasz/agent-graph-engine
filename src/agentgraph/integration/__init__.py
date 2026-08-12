"""Public API for source-neutral read-only shadow integration."""

from .errors import (
    IntegrationError,
    ProjectInspectionError,
    RepositoryRootMismatchError,
    ShadowDriftError,
    ShadowIntegrationError,
    ShadowRequestError,
    ShadowSelectionError,
    WorkSourceRepositoryMismatchError,
)
from .inspection import inspect_project, verify_work_source_revision
from .models import (
    BranchDisposition,
    IntegrationIssue,
    IntegrationIssueSeverity,
    PreflightAssessment,
    ProjectInspection,
    SelectionDisposition,
    SelectionPlan,
    ShadowInputs,
    ShadowOutcome,
    ShadowReport,
    ShadowRequest,
)
from .preflight import assess_preflight
from .selection import prepare_selection
from .shadow import ShadowRunner

__all__ = [
    "BranchDisposition",
    "IntegrationError",
    "IntegrationIssue",
    "IntegrationIssueSeverity",
    "PreflightAssessment",
    "ProjectInspection",
    "ProjectInspectionError",
    "RepositoryRootMismatchError",
    "SelectionDisposition",
    "SelectionPlan",
    "ShadowDriftError",
    "ShadowInputs",
    "ShadowIntegrationError",
    "ShadowOutcome",
    "ShadowReport",
    "ShadowRequest",
    "ShadowRequestError",
    "ShadowRunner",
    "ShadowSelectionError",
    "WorkSourceRepositoryMismatchError",
    "assess_preflight",
    "inspect_project",
    "prepare_selection",
    "verify_work_source_revision",
]
