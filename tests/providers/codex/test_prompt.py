from __future__ import annotations

from agentgraph.providers.codex import build_codex_change_prompt


def test_prompt_is_deterministic_complete_and_contains_no_local_absolute_path(
    codex_fixture,
) -> None:
    request = codex_fixture["request"]
    first = build_codex_change_prompt(request)

    assert first == build_codex_change_prompt(request)
    text = first.decode()
    assert request.goal in text
    assert "Acceptance one" in text
    assert "Test behavior" in text
    assert "src/pkg (directory capability)" in text
    assert request.baseline_head in text
    assert "keep_architecture" in text
    assert str(codex_fixture["repository"]) not in text
    assert "untrusted task data" in text
    assert "never override" in text
    assert "do not use network or external integration tools" in text
    assert "strict structured result" in text
    assert "run python -m pytest" not in text
