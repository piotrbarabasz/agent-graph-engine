from __future__ import annotations

import sys

from agentgraph.infra import CommandSpec, ProcessRunner, Redactor
from agentgraph.infra.redaction import REDACTED, is_sensitive_environment_key


def test_sensitive_environment_key_heuristic_is_bounded() -> None:
    assert is_sensitive_environment_key("GITHUB_TOKEN")
    assert is_sensitive_environment_key("api-key")
    assert is_sensitive_environment_key("Authorization")
    assert is_sensitive_environment_key("database_password")
    assert not is_sensitive_environment_key("MONKEY")
    assert not is_sensitive_environment_key("KEYBOARD_LAYOUT")


def test_explicit_and_argv_option_redaction() -> None:
    redactor = Redactor(("abcd1234",))
    argv = redactor.redact_argv(
        ("tool", "--token", "abcd1234", "--api-key=other", "prefix-abcd1234-suffix")
    )

    assert argv == (
        "tool",
        "--token",
        REDACTED,
        f"--api-key={REDACTED}",
        f"prefix-{REDACTED}-suffix",
    )


def test_raw_secrets_remain_available_but_never_enter_receipt_or_repr(tmp_path) -> None:
    secret = "top-secret-value"
    code = "import os,sys; value=os.environ['API_TOKEN']; print(value); sys.stderr.write(value)"
    result = ProcessRunner().run(
        CommandSpec(
            (sys.executable, "-c", code, f"--token={secret}"),
            tmp_path,
            env={"API_TOKEN": secret, "MONKEY": "visible"},
            secret_values=(secret,),
        )
    )

    assert secret.encode() in result.stdout
    assert secret.encode() in result.stderr
    diagnostic = repr(result.receipt) + repr(result)
    assert secret not in diagnostic
    assert REDACTED in diagnostic
    assert ("API_TOKEN", REDACTED) in result.receipt.env_overrides
    assert ("MONKEY", "visible") in result.receipt.env_overrides


def test_command_spec_repr_and_receipt_cwd_do_not_leak_explicit_secret(tmp_path) -> None:
    secret = "directory-secret"
    cwd = tmp_path / secret
    cwd.mkdir()
    spec = CommandSpec(
        (sys.executable, "-c", "pass", f"--token={secret}"),
        cwd,
        env={"API_TOKEN": secret},
        secret_values=(secret,),
    )

    result = ProcessRunner().run(spec)

    assert secret not in repr(spec)
    assert secret not in repr(result.receipt)
    assert REDACTED in result.receipt.cwd


def test_inherited_sensitive_environment_value_is_redacted_but_not_recorded(
    tmp_path, monkeypatch
) -> None:
    secret = "inherited-parent-secret"
    monkeypatch.setenv("PARENT_API_TOKEN", secret)
    result = ProcessRunner().run(
        CommandSpec(
            (
                sys.executable,
                "-c",
                "import os; print(os.environ['PARENT_API_TOKEN'])",
            ),
            tmp_path,
        )
    )

    assert result.stdout.strip() == secret.encode()
    assert secret not in repr(result.receipt)
    assert result.receipt.stdout_preview.strip() == REDACTED
    assert result.receipt.env_overrides == ()
