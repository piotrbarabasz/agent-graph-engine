"""Neutral remote-publication contracts and a strict GitHub.com provider."""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from agentgraph.runtime.codec import canonical_json_bytes, parse_json_bytes

MAX_HTTP_RESPONSE_BYTES = 1024 * 1024
MAX_REMOTE_TEXT = 4096
_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", re.ASCII)
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}", re.ASCII)
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}", re.ASCII)


class RemoteProviderError(Exception):
    code = "remote_provider_failed"


class RemoteServiceError(RemoteProviderError):
    code = "remote_service_unavailable"


class RemoteAuthenticationError(RemoteServiceError):
    code = "remote_authentication_failed"


class RemoteContractError(RemoteProviderError):
    code = "remote_response_contract_invalid"


class UnsupportedRemoteHostError(RemoteProviderError):
    code = "unsupported_remote_host"


class UnsafeRemoteUrlError(RemoteProviderError):
    code = "unsafe_remote_url"


@dataclass(frozen=True, slots=True)
class RemoteRepositoryRef:
    host: str
    owner: str
    repository: str

    def __post_init__(self) -> None:
        if self.host != "github.com":
            raise UnsupportedRemoteHostError("unsupported_remote_host")
        if not _OWNER.fullmatch(self.owner) or not _REPOSITORY.fullmatch(self.repository):
            raise UnsafeRemoteUrlError("unsafe_remote_url")

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repository}"


@dataclass(frozen=True, slots=True)
class RemoteRepositoryIdentity:
    host: str
    repository_id: str
    full_name: str

    def __post_init__(self) -> None:
        if self.host != "github.com" or not self.repository_id or "\x00" in self.repository_id:
            raise RemoteContractError("remote repository identity is invalid")
        owner, separator, repository = self.full_name.partition("/")
        if separator != "/" or self.full_name.count("/") != 1:
            raise RemoteContractError("remote repository identity is invalid")
        try:
            RemoteRepositoryRef(self.host, owner, repository)
        except RemoteProviderError as exc:
            raise RemoteContractError("remote repository identity is invalid") from exc


@dataclass(frozen=True, slots=True)
class RemotePullRequest:
    repository: RemoteRepositoryIdentity
    pr_id: str
    number: int
    url: str
    state: str
    draft: bool
    head_branch: str
    head_sha: str
    base_branch: str
    title: str
    body: str

    def __post_init__(self) -> None:
        if (
            not self.pr_id
            or len(self.pr_id) > 128
            or type(self.number) is not int
            or self.number < 1
            or self.state != "open"
            or type(self.draft) is not bool
            or not _SHA.fullmatch(self.head_sha)
            or not self.head_branch
            or not self.base_branch
            or any(
                "\x00" in value or len(value) > 255
                for value in (self.head_branch, self.base_branch)
            )
            or "\x00" in self.title
            or "\x00" in self.body
            or len(self.title) > 256
            or len(self.body) > 65536
        ):
            raise RemoteContractError("pull request contract is invalid")
        parsed = urllib.parse.urlsplit(self.url)
        expected = f"/{self.repository.full_name}/pull/{self.number}"
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != expected
            or parsed.query
            or parsed.fragment
        ):
            raise RemoteContractError("pull request URL is invalid")


@dataclass(frozen=True, slots=True)
class DraftPullRequestRequest:
    repository: RemoteRepositoryIdentity
    title: str
    body: str
    head_branch: str
    base_branch: str
    final_head: str
    draft: bool = True

    def __post_init__(self) -> None:
        if (
            not self.draft
            or not self.title
            or len(self.title) > 256
            or not self.body
            or len(self.body) > 65536
            or not _SHA.fullmatch(self.final_head)
        ):
            raise RemoteContractError("draft pull request request is invalid")


class RemoteProvider(Protocol):
    def inspect_repository(self, repository: RemoteRepositoryRef) -> RemoteRepositoryIdentity: ...

    def find_open_pull_requests(
        self,
        repository: RemoteRepositoryIdentity,
        *,
        head_branch: str,
        base_branch: str,
    ) -> tuple[RemotePullRequest, ...]: ...

    def create_draft_pull_request(self, request: DraftPullRequestRequest) -> RemotePullRequest: ...


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        max_response_bytes: int,
    ) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibHttpTransport:
    """Bounded transport that never forwards authorization across redirects."""

    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("HTTP timeout must be positive")
        self.timeout_seconds = timeout_seconds
        self._opener = urllib.request.build_opener(_NoRedirect())

    def request(self, method, url, *, headers, body, max_response_bytes):
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                raw = response.read(max_response_bytes + 1)
                if len(raw) > max_response_bytes:
                    raise RemoteContractError("remote response exceeded the size bound")
                return HttpResponse(response.status, raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read(min(max_response_bytes, 4096))
            return HttpResponse(exc.code, raw)
        except (OSError, urllib.error.URLError) as exc:
            raise RemoteServiceError("remote service request failed") from exc


def parse_github_remote_url(value: str) -> RemoteRepositoryRef:
    """Parse common credential-free GitHub remote URL forms."""

    if not isinstance(value, str) or not value or "\x00" in value or len(value) > MAX_REMOTE_TEXT:
        raise UnsafeRemoteUrlError("unsafe_remote_url")
    if value.startswith("git@github.com:"):
        host = "github.com"
        path = value.removeprefix("git@github.com:")
    else:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in {"https", "ssh"}:
            raise UnsupportedRemoteHostError("unsupported_remote_host")
        if parsed.scheme == "https" and (
            parsed.username is not None or parsed.password is not None
        ):
            raise UnsafeRemoteUrlError("unsafe_remote_url")
        if parsed.scheme == "ssh" and parsed.username not in {None, "git"}:
            raise UnsafeRemoteUrlError("unsafe_remote_url")
        if parsed.port is not None:
            raise UnsupportedRemoteHostError("unsupported_remote_host")
        host = (parsed.hostname or "").lower()
        path = parsed.path.lstrip("/")
        if parsed.query or parsed.fragment:
            raise UnsafeRemoteUrlError("unsafe_remote_url")
    if host != "github.com":
        raise UnsupportedRemoteHostError("unsupported_remote_host")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) != 2:
        raise UnsafeRemoteUrlError("unsafe_remote_url")
    return RemoteRepositoryRef(host, parts[0], parts[1])


class GitHubRemoteProvider:
    """Strict GitHub.com REST implementation of the neutral remote role."""

    evidence_namespace = "github"

    def __init__(
        self,
        transport: HttpTransport | None = None,
        *,
        token_provider: Callable[[], str | None] | None = None,
    ) -> None:
        self.transport = transport or UrllibHttpTransport()
        self.token_provider = token_provider or (
            lambda: os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        )

    def inspect_repository(self, repository: RemoteRepositoryRef) -> RemoteRepositoryIdentity:
        payload = self._json("GET", f"/repos/{repository.full_name}")
        identity = self._repository(payload)
        if identity.full_name.lower() != repository.full_name.lower():
            raise RemoteContractError("remote repository identity mismatch")
        return identity

    def find_open_pull_requests(self, repository, *, head_branch, base_branch):
        owner = repository.full_name.split("/", 1)[0]
        query = urllib.parse.urlencode(
            {"state": "open", "head": f"{owner}:{head_branch}", "base": base_branch}
        )
        payload = self._json("GET", f"/repos/{repository.full_name}/pulls?{query}")
        if not isinstance(payload, list) or len(payload) > 100:
            raise RemoteContractError("pull request list contract is invalid")
        return tuple(self._pull_request(repository, item) for item in payload)

    def create_draft_pull_request(self, request):
        payload = self._json(
            "POST",
            f"/repos/{request.repository.full_name}/pulls",
            {
                "title": request.title,
                "body": request.body,
                "head": request.head_branch,
                "base": request.base_branch,
                "draft": True,
            },
        )
        return self._pull_request(request.repository, payload)

    def _json(self, method: str, path: str, payload: object | None = None):
        token = self.token_provider()
        if not isinstance(token, str) or not token or "\x00" in token:
            raise RemoteAuthenticationError("GitHub credentials are unavailable")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agentgraph",
        }
        body = None if payload is None else canonical_json_bytes(payload)
        if body is not None:
            headers["Content-Type"] = "application/json"
        response = self.transport.request(
            method,
            f"https://api.github.com{path}",
            headers=headers,
            body=body,
            max_response_bytes=MAX_HTTP_RESPONSE_BYTES,
        )
        if response.status in {401, 403, 429}:
            raise RemoteAuthenticationError("GitHub rejected the request")
        if response.status >= 500:
            raise RemoteServiceError("GitHub service is unavailable")
        expected = 201 if method == "POST" else 200
        if response.status != expected:
            raise RemoteServiceError(f"GitHub request failed with status {response.status}")
        try:
            return parse_json_bytes(response.body)
        except Exception as exc:
            raise RemoteContractError("GitHub returned malformed JSON") from exc

    @staticmethod
    def _repository(payload: object) -> RemoteRepositoryIdentity:
        if not isinstance(payload, dict):
            raise RemoteContractError("repository response contract is invalid")
        repository_id = payload.get("id")
        full_name = payload.get("full_name")
        if type(repository_id) is not int or repository_id < 1 or not isinstance(full_name, str):
            raise RemoteContractError("repository response contract is invalid")
        return RemoteRepositoryIdentity("github.com", str(repository_id), full_name)

    @staticmethod
    def _pull_request(repository: RemoteRepositoryIdentity, payload: object) -> RemotePullRequest:
        try:
            assert isinstance(payload, dict)
            head = payload["head"]
            base = payload["base"]
            assert isinstance(head, dict) and isinstance(base, dict)
            head_repository = head["repo"]
            base_repository = base["repo"]
            assert isinstance(head_repository, dict) and isinstance(base_repository, dict)
            for returned in (head_repository, base_repository):
                assert returned["full_name"] == repository.full_name
                assert str(returned["id"]) == repository.repository_id
            assert type(payload["id"]) is int and payload["id"] > 0
            return RemotePullRequest(
                repository,
                str(payload["id"]),
                payload["number"],
                payload["html_url"],
                payload["state"],
                payload["draft"],
                head["ref"],
                head["sha"],
                base["ref"],
                payload["title"],
                payload.get("body") or "",
            )
        except (AssertionError, KeyError, TypeError, ValueError) as exc:
            raise RemoteContractError("pull request response contract is invalid") from exc
