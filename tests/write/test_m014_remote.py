from __future__ import annotations

import pytest

from agentgraph.runtime.codec import canonical_json_bytes
from agentgraph.write import (
    DraftPullRequestRequest,
    GitHubRemoteProvider,
    HttpResponse,
    RemoteAuthenticationError,
    RemoteContractError,
    RemoteServiceError,
    UnsafeRemoteUrlError,
    UnsupportedRemoteHostError,
    parse_github_remote_url,
)


@pytest.mark.parametrize(
    "value",
    (
        "https://github.com/owner/repo.git",
        "git@github.com:owner/repo.git",
        "ssh://git@github.com/owner/repo.git",
    ),
)
def test_common_github_remote_urls_have_one_canonical_identity(value) -> None:
    assert parse_github_remote_url(value).full_name == "owner/repo"


def test_remote_url_rejects_custom_hosts_and_https_credentials() -> None:
    with pytest.raises(UnsupportedRemoteHostError):
        parse_github_remote_url("https://example.test/owner/repo.git")
    with pytest.raises(UnsafeRemoteUrlError):
        parse_github_remote_url("https://sentinel@github.com/owner/repo.git")


class CapturingTransport:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.calls = []

    def request(self, method, url, *, headers, body, max_response_bytes):
        self.calls.append((method, url, dict(headers), body, max_response_bytes))
        return self.response


def test_github_token_is_header_only_and_errors_do_not_echo_it() -> None:
    token = "m014-sentinel-token"
    transport = CapturingTransport(HttpResponse(401, b'{"message":"bad credentials"}'))
    provider = GitHubRemoteProvider(transport, token_provider=lambda: token)

    with pytest.raises(RemoteAuthenticationError) as raised:
        provider.inspect_repository(parse_github_remote_url("https://github.com/o/r.git"))

    assert transport.calls[0][2]["Authorization"] == f"Bearer {token}"
    assert token not in str(raised.value)


@pytest.mark.parametrize(
    ("status", "error"),
    (
        (401, RemoteAuthenticationError),
        (403, RemoteAuthenticationError),
        (429, RemoteAuthenticationError),
        (500, RemoteServiceError),
        (503, RemoteServiceError),
    ),
)
def test_github_operational_failures_are_typed_without_response_leak(status, error) -> None:
    sentinel = "m014-sensitive-response"
    transport = CapturingTransport(HttpResponse(status, sentinel.encode()))
    provider = GitHubRemoteProvider(transport, token_provider=lambda: "secret")

    with pytest.raises(error) as raised:
        provider.inspect_repository(parse_github_remote_url("https://github.com/o/r.git"))

    assert sentinel not in str(raised.value)


def test_github_malformed_json_and_pull_request_contract_fail_closed() -> None:
    malformed = GitHubRemoteProvider(
        CapturingTransport(HttpResponse(200, b"not-json")), token_provider=lambda: "secret"
    )
    with pytest.raises(RemoteContractError, match="malformed JSON"):
        malformed.inspect_repository(parse_github_remote_url("https://github.com/o/r.git"))

    repository = GitHubRemoteProvider._repository({"id": 123, "full_name": "owner/repo"})
    invalid_pr = GitHubRemoteProvider(
        CapturingTransport(HttpResponse(201, canonical_json_bytes({"id": 456}))),
        token_provider=lambda: "secret",
    )
    request = DraftPullRequestRequest(
        repository,
        "AgentGraph: E001",
        "<!-- agentgraph-publish:sha256:marker -->",
        "work/e001",
        "main",
        "a" * 40,
    )
    with pytest.raises(RemoteContractError, match="response contract"):
        invalid_pr.create_draft_pull_request(request)


def _pr_payload():
    repository = {"id": 123, "full_name": "owner/repo"}
    return {
        "id": 456,
        "number": 7,
        "html_url": "https://github.com/owner/repo/pull/7",
        "state": "open",
        "draft": True,
        "head": {"ref": "work/e001", "sha": "a" * 40, "repo": repository},
        "base": {"ref": "main", "repo": repository},
        "title": "AgentGraph: E001",
        "body": "<!-- agentgraph-publish:sha256:marker -->",
    }


def test_github_create_sends_exact_draft_fields_and_validates_response() -> None:
    transport = CapturingTransport(HttpResponse(201, canonical_json_bytes(_pr_payload())))
    provider = GitHubRemoteProvider(transport, token_provider=lambda: "secret")
    repository = provider._repository({"id": 123, "full_name": "owner/repo"})
    request = DraftPullRequestRequest(
        repository,
        "AgentGraph: E001",
        "<!-- agentgraph-publish:sha256:marker -->",
        "work/e001",
        "main",
        "a" * 40,
    )

    pull_request = provider.create_draft_pull_request(request)

    assert pull_request.number == 7
    assert transport.calls[0][0] == "POST"
    assert transport.calls[0][3] == canonical_json_bytes(
        {
            "title": request.title,
            "body": request.body,
            "head": request.head_branch,
            "base": request.base_branch,
            "draft": True,
        }
    )
