from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from agentgraph.infra import CommandSpec, ProcessRunner, ProcessStatus, ProcessTermination


def _is_process_running(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_timeout_terminates_and_reaps_process(tmp_path: Path) -> None:
    code = "import os,time; print(os.getpid(),flush=True); time.sleep(30)"
    started = time.monotonic()
    result = ProcessRunner().run(
        CommandSpec(
            (sys.executable, "-c", code),
            tmp_path,
            timeout_seconds=0.2,
            termination_grace_seconds=0.2,
        )
    )

    pid = int(result.stdout.strip())
    assert result.receipt.status is ProcessStatus.TIMED_OUT
    assert result.receipt.termination in {
        ProcessTermination.GRACEFUL,
        ProcessTermination.FORCED,
    }
    assert time.monotonic() - started < 5
    assert not _is_process_running(pid)
