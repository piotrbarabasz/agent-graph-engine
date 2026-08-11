"""Immutable resolved policy for one graph run."""

from __future__ import annotations

from dataclasses import dataclass

from .enums import CommitMode
from .errors import ContractValidationError


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    """Resolved policy that cannot change during a run."""

    max_repair_cycles: int = 2
    max_work_items_per_run: int = 10
    commit_mode: CommitMode = CommitMode.DISABLED
    push_mode: bool = False
    pull_request_mode: bool = False
    merge_allowed: bool = False
    deployment_allowed: bool = False

    def __post_init__(self) -> None:
        if type(self.max_repair_cycles) is not int or self.max_repair_cycles < 0:
            raise ContractValidationError("max_repair_cycles cannot be negative")
        if type(self.max_work_items_per_run) is not int or self.max_work_items_per_run < 1:
            raise ContractValidationError("max_work_items_per_run must be positive")
        if not isinstance(self.commit_mode, CommitMode):
            raise ContractValidationError("commit_mode must be a CommitMode")
        if not all(
            type(value) is bool
            for value in (
                self.push_mode,
                self.pull_request_mode,
                self.merge_allowed,
                self.deployment_allowed,
            )
        ):
            raise ContractValidationError("policy flags must be boolean")
        if self.merge_allowed:
            raise ContractValidationError("v1 policy cannot allow merge")
        if self.deployment_allowed:
            raise ContractValidationError("v1 policy cannot allow deployment")
