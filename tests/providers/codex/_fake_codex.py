"""Cross-platform fake for the supported Codex CLI contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    if args == ["--version"]:
        print("codex-cli 9.9.9-fake")
        return 0
    if args == ["exec", "--help"]:
        if os.environ.get("FAKE_CODEX_UNSUPPORTED") == "1":
            print("Run Codex non-interactively")
            return 0
        print(
            "Run Codex non-interactively --sandbox read-only --config --strict-config "
            "--ephemeral --ignore-user-config --ignore-rules --output-schema "
            "--output-last-message --model"
        )
        return 0
    if not args or args[0] != "exec":
        return 2
    prompt = sys.stdin.buffer.read().decode("utf-8")
    output = Path(args[args.index("--output-last-message") + 1])
    mode = os.environ.get("FAKE_CODEX_MODE", "success")
    _increment(os.environ.get("FAKE_CODEX_COUNT"))
    capture = os.environ.get("FAKE_CODEX_CAPTURE")
    if capture:
        Path(capture).write_text(
            json.dumps(
                {
                    "argv": args,
                    "prompt": prompt,
                    "cwd": os.getcwd(),
                    "sensitive_visible": {
                        key: key in os.environ
                        for key in (
                            "GITHUB_TOKEN",
                            "AWS_SECRET_ACCESS_KEY",
                            "SOME_TEST_SECRET",
                            "OPENAI_API_KEY",
                        )
                    },
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    if mode == "hang":
        time.sleep(30)
    if mode == "nonzero":
        print("fake invocation failed", file=sys.stderr)
        return 7
    if mode == "tracked":
        Path("tracked.txt").write_text("mutated by fake Codex\n", encoding="utf-8")
    elif mode == "untracked":
        Path("codex.tmp").write_text("untracked\n", encoding="utf-8")
    elif mode == "staged":
        candidate = Path("src/t001.py")
        candidate.parent.mkdir(exist_ok=True)
        candidate.write_text("staged by fake Codex\n", encoding="utf-8")
        subprocess.run(("git", "add", "--", "src/t001.py"), check=True, shell=False)
    elif mode == "head":
        Path("head-mutation.txt").write_text("commit\n", encoding="utf-8")
        subprocess.run(("git", "add", "--", "head-mutation.txt"), check=True, shell=False)
        subprocess.run(
            (
                "git",
                "-c",
                "user.name=Fake Codex",
                "-c",
                "user.email=fake@example.test",
                "commit",
                "-m",
                "forbidden fake commit",
            ),
            check=True,
            shell=False,
            stdout=subprocess.DEVNULL,
        )
    if mode == "malformed":
        output.write_text("{not json", encoding="utf-8")
    elif mode == "freeform":
        output.write_text('Sure! {"schema_version":1}', encoding="utf-8")
    elif mode == "oversized":
        output.write_bytes(b"x" * (1024 * 1024 + 1))
    elif mode == "symlink":
        target = output.with_name("symlink-target.json")
        target.write_text(_proposal(), encoding="utf-8")
        output.symlink_to(target)
    else:
        output.write_text(os.environ.get("FAKE_CODEX_RESULT", _proposal()), encoding="utf-8")
    return 0


def _proposal() -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "status": "changes",
            "changes": [{"path": "src/new.py", "content": "value = 42\n"}],
            "reason_code": None,
            "message": None,
        },
        separators=(",", ":"),
    )


def _increment(raw: str | None) -> None:
    if raw is None:
        return
    path = Path(raw)
    count = int(path.read_text(encoding="ascii")) if path.exists() else 0
    path.write_text(str(count + 1), encoding="ascii")


if __name__ == "__main__":
    raise SystemExit(main())
