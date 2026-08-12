"""Typed failures for the read-only integration boundary."""


class IntegrationError(Exception):
    """Base integration contract failure."""


class ProjectInspectionError(IntegrationError):
    """Project inspection could not produce trusted inputs."""


class RepositoryRootMismatchError(ProjectInspectionError):
    """The configured target is not the canonical Git root."""


class WorkSourceRepositoryMismatchError(ProjectInspectionError):
    """Work-source revision documents do not belong to the configured repository."""


class ShadowRequestError(IntegrationError):
    """A shadow selection request is structurally invalid."""


class ShadowSelectionError(IntegrationError):
    """Selection preparation violated an integration contract."""


class ShadowDriftError(IntegrationError):
    """Prepared inputs changed during a shadow probe."""


class ShadowIntegrationError(IntegrationError):
    """The bounded in-memory graph probe could not complete."""
