from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from agentgraph.infra import (
    CancellationToken,
    CommandSpec,
    ProcessRunner,
    ProcessStatus,
    ProcessTermination,
)


def test_cancellation_before_spawn_does_not_start_child(tmp_path: Path) -> None:
    marker = tmp_path / "started.txt"
    token = CancellationToken()
    token.cancel()
    result = ProcessRunner().run(
        CommandSpec(
            (sys.executable, "-c", f"open({str(marker)!r},'w').write('started')"),
            tmp_path,
        ),
        cancellation=token,
    )

    assert result.receipt.status is ProcessStatus.CANCELLED
    assert result.receipt.termination is ProcessTermination.NOT_STARTED
    assert result.receipt.exit_code is None
    assert not marker.exists()


def test_cancellation_while_running_terminates_child(tmp_path: Path) -> None:
    token = CancellationToken()
    runner = ProcessRunner()
    result_holder = []

    def invoke() -> None:
        result_holder.append(
            runner.run(
                CommandSpec(
                    (
                        sys.executable,
                        "-c",
                        "import time; print('started',flush=True); time.sleep(30)",
                    ),
                    tmp_path,
                    termination_grace_seconds=0.2,
                ),
                cancellation=token,
            )
        )

    thread = threading.Thread(target=invoke)
    thread.start()
    time.sleep(0.2)
    token.cancel()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert result_holder[0].receipt.status is ProcessStatus.CANCELLED
    assert result_holder[0].stdout.strip() == b"started"
