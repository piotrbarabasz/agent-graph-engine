"""Typed failures for immutable work-source contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import WorkSourceValidation


class WorkSourceError(Exception):
    """Base neutral work-source error."""


class WorkSourceConfigurationError(WorkSourceError):
    """Work-source configuration is malformed."""


class WorkSourcePathError(WorkSourceError):
    """A source or declared repository path is unsafe."""


class WorkSourceFormatError(WorkSourceError):
    """Source syntax or a declarative command is malformed."""


class InvalidWorkSourceError(WorkSourceError):
    """A snapshot cannot be created from an invalid source."""

    def __init__(self, validation: WorkSourceValidation) -> None:
        self.validation = validation
        super().__init__(f"work source is invalid ({len(validation.issues)} validation issue(s))")


class WorkScopeNotFoundError(WorkSourceError):
    """A requested scope does not exist in the supplied snapshot."""


class WorkItemNotFoundError(WorkSourceError):
    """A requested item does not exist in the supplied snapshot."""
