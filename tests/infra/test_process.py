from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agentgraph.infra import CommandSpec, ProcessRunner, ProcessStatus
from agentgraph.infra.errors import InvalidCommandSpecError, ProcessStartError


def python_spec(tmp_path: Path, code: str, **kwargs) -> CommandSpec:
    return CommandSpec((sys.executable, "-c", code), tmp_path, **kwargs)


def test_success_nonzero_and_raw_stdout_stderr(tmp_path) -> None:
    runner = ProcessRunner()
    success = runner.run(
        python_spec(
            tmp_path,
            "import os; os.write(1,b'raw\\x00out'); os.write(2,b'raw-error')",
        )
    )
    failed = runner.run(python_spec(tmp_path, "import sys; sys.exit(7)"))

    assert success.receipt.status is ProcessStatus.SUCCEEDED
    assert success.receipt.exit_code == 0
    assert success.stdout == b"raw\x00out"
    assert success.stderr == b"raw-error"
    assert failed.receipt.status is ProcessStatus.FAILED
    assert failed.receipt.exit_code == 7


def test_missing_executable_is_typed_start_error(tmp_path) -> None:
    spec = CommandSpec((str(tmp_path / "definitely-missing-executable"),), tmp_path)
    with pytest.raises(ProcessStartError, match="failed to start command"):
        ProcessRunner().run(spec)


@pytest.mark.parametrize(
    "factory",
    [
        lambda root: CommandSpec((), root),
        lambda root: CommandSpec("python", root),
        lambda root: CommandSpec((sys.executable,), root / "missing"),
        lambda root: CommandSpec((sys.executable,), root, timeout_seconds=0),
        lambda root: CommandSpec((sys.executable,), root, max_stdout_bytes=0),
        lambda root: CommandSpec((sys.executable,), root, stdin="text"),
        lambda root: CommandSpec((sys.executable,), root, env={"OK": 1}),
        lambda root: CommandSpec((sys.executable,), root, unset_env=["VALUE"]),
        lambda root: CommandSpec((sys.executable,), root, unset_env=("",)),
        lambda root: CommandSpec((sys.executable,), root, unset_env=("BAD=KEY",)),
        lambda root: CommandSpec((sys.executable,), root, unset_env=("VALUE", "VALUE")),
    ],
)
def test_invalid_command_specs_fail_closed(tmp_path, factory) -> None:
    with pytest.raises(InvalidCommandSpecError):
        factory(tmp_path)


def test_stdin_cwd_and_environment_override_without_global_mutation(tmp_path, monkeypatch) -> None:
    key = "AGENTGRAPH_M003_TEST_VALUE"
    monkeypatch.delenv(key, raising=False)
    code = (
        "import os,sys; "
        "sys.stdout.buffer.write(sys.stdin.buffer.read()+b'|'+os.getcwd().encode()+b'|'"
        f"+os.environ['{key}'].encode())"
    )
    result = ProcessRunner().run(python_spec(tmp_path, code, stdin=b"input", env={key: "override"}))

    assert result.stdout == b"input|" + os.fsencode(tmp_path.resolve()) + b"|override"
    assert key not in os.environ


def test_unset_env_removes_child_value_without_mutating_parent_and_override_wins(
    tmp_path, monkeypatch
) -> None:
    key = "AGENTGRAPH_REMOVE_ME"
    monkeypatch.setenv(key, "poison")
    code = f"import os; print(os.environ.get('{key}', 'missing'))"
    removed = ProcessRunner().run(python_spec(tmp_path, code, unset_env=(key,)))
    overridden = ProcessRunner().run(
        python_spec(tmp_path, code, unset_env=(key,), env={key: "safe"})
    )

    assert removed.stdout.strip() == b"missing"
    assert overridden.stdout.strip() == b"safe"
    assert os.environ[key] == "poison"


def test_stdout_and_stderr_are_drained_without_pipe_deadlock(tmp_path) -> None:
    size = 2 * 1024 * 1024
    result = ProcessRunner().run(
        python_spec(
            tmp_path,
            f"import os; os.write(1,b'a'*{size}); os.write(2,b'b'*{size})",
            timeout_seconds=10,
        )
    )

    assert result.receipt.status is ProcessStatus.SUCCEEDED
    assert result.receipt.stdout_size == size
    assert result.receipt.stderr_size == size
    assert len(result.stdout) == size
    assert len(result.stderr) == size


def test_output_capture_is_bounded_and_preserves_head_and_tail(tmp_path) -> None:
    result = ProcessRunner().run(
        python_spec(
            tmp_path,
            "import os; os.write(1,b'H'*5000000+b'T'*5000000)",
            max_stdout_bytes=1024,
        )
    )

    assert result.receipt.stdout_size == 10_000_000
    assert result.receipt.stdout_truncated is True
    assert len(result.stdout) == 1024
    assert result.stdout[:512] == b"H" * 512
    assert result.stdout[512:] == b"T" * 512
    assert len(repr(result)) < 10_000


def test_one_runner_supports_parallel_independent_invocations(tmp_path) -> None:
    runner = ProcessRunner()

    def invoke(value: str) -> bytes:
        return runner.run(python_spec(tmp_path, f"print({value!r})")).stdout.strip()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = set(pool.map(invoke, ("one", "two")))

    assert results == {b"one", b"two"}


def test_runner_creates_no_capture_artifacts_in_target_cwd(tmp_path) -> None:
    marker = tmp_path / "keep.txt"
    marker.write_text("unchanged", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    ProcessRunner().run(python_spec(tmp_path, "print('output')"))

    after = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert after == before
