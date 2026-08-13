from __future__ import annotations

from agentgraph.agents import SEMANTIC_REVIEW_SCHEMA, AgentRequest
from agentgraph.agents.prompts import build_semantic_review_prompt
from agentgraph.core import RiskLevel
from agentgraph.write import SemanticReviewContext
from agentgraph.write.analysis import semantic_review_request


def context() -> SemanticReviewContext:
    return SemanticReviewContext.create(
        cycle=0,
        item_id="T001",
        scope_id="E001",
        goal="Implement the bounded task.",
        current_manifest_digest="sha256:" + "1" * 64,
        current_changed_paths=("src/service.py",),
        effective_requirements=("REQ-1",),
        effective_acceptance_criteria=("AC-1",),
        architecture_invariants=("target_main_read_only",),
        derived_constraints=("bounded",),
        validation_diagnostics=(),
        allowed_paths=(),
        baseline_head="a" * 40,
        source_revision="sha256:" + "2" * 64,
        risk_level=RiskLevel.MEDIUM,
        relevant_files=("src/service.py",),
    )


def rebuilt(value: SemanticReviewContext, **changes) -> SemanticReviewContext:
    values = {
        field: getattr(value, field) for field in value.__dataclass_fields__ if field != "digest"
    }
    values.update(changes)
    return SemanticReviewContext.create(**values)


def test_semantic_request_digest_binds_context_prompt_schema_and_schema_id() -> None:
    original_context = context()
    original = semantic_review_request(original_context)
    changed_contexts = (
        rebuilt(original_context, current_manifest_digest="sha256:" + "3" * 64),
        rebuilt(original_context, cycle=1),
        rebuilt(original_context, effective_requirements=("REQ-1", "REQ-2")),
    )

    assert all(
        semantic_review_request(changed).input_digest != original.input_digest
        for changed in changed_contexts
    )
    changed_prompt = AgentRequest.create(
        "semantic_review",
        build_semantic_review_prompt(original_context) + "\n",
        SEMANTIC_REVIEW_SCHEMA,
        "agentgraph.semantic-review.v1",
    )
    changed_schema = AgentRequest.create(
        "semantic_review",
        original.prompt,
        {**SEMANTIC_REVIEW_SCHEMA, "title": "changed"},
        "agentgraph.semantic-review.v1",
    )
    changed_schema_id = AgentRequest.create(
        "semantic_review",
        original.prompt,
        SEMANTIC_REVIEW_SCHEMA,
        "agentgraph.semantic-review.v2",
    )

    assert changed_prompt.input_digest != original.input_digest
    assert changed_schema.input_digest != original.input_digest
    assert changed_schema_id.input_digest != original.input_digest
