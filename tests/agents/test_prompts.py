from __future__ import annotations

from types import SimpleNamespace

from agentgraph.agents import (
    AgentAnalysisStatus,
    AgentTaskPackage,
    ExploreAnalysis,
)
from agentgraph.agents.prompts import (
    build_delivery_review_prompt,
    build_explore_prompt,
    build_failure_classification_prompt,
    build_risk_prompt,
    build_semantic_review_prompt,
    build_task_package_prompt,
)
from agentgraph.core import RiskLevel


def test_explore_prompt_states_success_and_blocked_output_contract() -> None:
    prompt = build_explore_prompt(_inputs())

    _assert_common_status_contract(prompt)
    assert "do not put an exploration summary in message" in prompt
    for field in (
        "relevant_files",
        "architecture_observations",
        "derived_requirements",
        "derived_acceptance_criteria",
        "derived_constraints",
        "architecture_invariants",
        "uncertainties",
    ):
        assert field in prompt
    assert prompt.index("OUTPUT CONTRACT") < prompt.index("INPUT")


def test_task_package_prompt_states_success_requirements() -> None:
    prompt = build_task_package_prompt(_inputs(), _explore())

    _assert_common_status_contract(prompt)
    assert "objective MUST be a non-empty string" in prompt
    assert "implementation_steps MUST contain at least one non-empty step" in prompt
    assert "do not fabricate a successful objective or implementation plan" in prompt
    assert prompt.index("OUTPUT CONTRACT") < prompt.index("INPUT")


def test_risk_prompt_states_successful_risk_requirement() -> None:
    prompt = build_risk_prompt(_inputs(), _explore(), _task_package())

    _assert_common_status_contract(prompt)
    assert "risk_level MUST be one of low, medium, high, or critical" in prompt
    assert "risk_level MAY be null" in prompt
    assert prompt.index("OUTPUT CONTRACT") < prompt.index("INPUT")


def test_failure_classification_prompt_states_success_and_blocked_routes() -> None:
    prompt = build_failure_classification_prompt(_failure_context())

    _assert_common_status_contract(prompt)
    assert 'classification MUST be "programmer" or "debugger"' in prompt
    assert "rationale MUST be a non-empty string" in prompt
    assert "classification MUST be null" in prompt
    assert "rationale MUST be null" in prompt
    assert prompt.index("OUTPUT CONTRACT") < prompt.index("FAILURE CONTEXT")


def test_semantic_review_prompt_states_exact_decision_contract() -> None:
    prompt = build_semantic_review_prompt(_semantic_context())

    _assert_common_status_contract(prompt)
    _assert_review_contract(prompt)
    assert "SEMANTIC REVIEW RULES" in prompt
    assert prompt.index("OUTPUT CONTRACT") < prompt.index("ENGINE-BOUND REVIEW CONTEXT")


def test_delivery_review_prompt_states_exact_decision_contract() -> None:
    prompt = build_delivery_review_prompt(_delivery_context())

    _assert_common_status_contract(prompt)
    _assert_review_contract(prompt)
    assert "DELIVERY REVIEW RULES" in prompt
    assert prompt.index("OUTPUT CONTRACT") < prompt.index("ENGINE-BOUND DELIVERY CONTEXT")


def _assert_common_status_contract(prompt: str) -> None:
    assert 'For status="success":' in prompt
    assert "reason_code MUST be null" in prompt
    assert "message MUST be null" in prompt
    assert "Do NOT use message as a success summary" in prompt
    assert 'For status="blocked":' in prompt
    assert "reason_code MUST be a non-empty lowercase machine-readable reason code" in prompt
    assert "message MUST be a non-empty human-readable explanation" in prompt


def _assert_review_contract(prompt: str) -> None:
    assert 'verdict="pass"' in prompt
    assert "findings MUST be empty" in prompt
    assert 'verdict="fail"' in prompt
    assert "findings MUST contain at least one material finding" in prompt
    assert "summary MUST be a non-empty string" in prompt
    assert "verdict MUST be null" in prompt
    assert "summary MUST be null" in prompt


def _inputs():
    package = SimpleNamespace(
        item_id="T001",
        scope_id="E001",
        title="Test item",
        goal="Implement the bounded behavior.",
        acceptance_criteria=("Behavior is deterministic.",),
        test_requirements=("Run focused tests.",),
        risk=SimpleNamespace(value="medium"),
    )
    return SimpleNamespace(
        package=package,
        expected_allowed_paths=(SimpleNamespace(path="src/example.py"),),
        baseline_head="a" * 40,
    )


def _explore() -> ExploreAnalysis:
    return ExploreAnalysis(
        schema_version=1,
        status=AgentAnalysisStatus.SUCCESS,
        relevant_files=("src/example.py",),
        architecture_observations=("The boundary is stable.",),
        derived_requirements=(),
        derived_acceptance_criteria=(),
        derived_constraints=(),
        architecture_invariants=(),
        uncertainties=(),
        reason_code=None,
        message=None,
    )


def _task_package() -> AgentTaskPackage:
    return AgentTaskPackage(
        schema_version=1,
        status=AgentAnalysisStatus.SUCCESS,
        objective="Implement the bounded behavior.",
        implementation_steps=("Update the declared module.",),
        recommended_change_paths=("src/example.py",),
        supporting_read_paths=(),
        validation_focus=(),
        assumptions=(),
        unresolved_questions=(),
        reason_code=None,
        message=None,
    )


def _failure_context():
    return SimpleNamespace(
        failure_source_node="VALIDATE",
        failure_category=SimpleNamespace(value="validation"),
        failure_code="declared_validation_failed",
        current_changed_paths=("src/example.py",),
        current_manifest_digest="sha256:" + "1" * 64,
        validation_diagnostics=(),
        review_findings=(),
        effective_requirements=("REQ-1",),
        effective_acceptance_criteria=("AC-1",),
    )


def _semantic_context():
    return SimpleNamespace(
        cycle=0,
        item_id="T001",
        scope_id="E001",
        goal="Implement the bounded behavior.",
        effective_requirements=("REQ-1",),
        effective_acceptance_criteria=("AC-1",),
        architecture_invariants=(),
        derived_constraints=(),
        current_manifest_digest="sha256:" + "1" * 64,
        current_changed_paths=("src/example.py",),
        validation_diagnostics=(),
        allowed_paths=(),
        baseline_head="a" * 40,
        source_revision="sha256:" + "2" * 64,
        risk_level=RiskLevel.MEDIUM,
        relevant_files=("src/example.py",),
        digest="sha256:" + "3" * 64,
    )


def _delivery_context():
    return SimpleNamespace(
        scope_id="E001",
        source_revision="sha256:" + "1" * 64,
        work_plan_digest="sha256:" + "2" * 64,
        target_baseline_head="a" * 40,
        final_head="b" * 40,
        final_tree_id="c" * 40,
        delivery_manifest_digest="sha256:" + "3" * 64,
        final_changed_paths=("src/example.py",),
        delivery_allowed_paths=(),
        completed_items=(),
        declared_work=(),
        architecture_invariants=(),
        context_digest="sha256:" + "4" * 64,
    )
