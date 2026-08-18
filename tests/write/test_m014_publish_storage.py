from __future__ import annotations

import os

import pytest

from agentgraph.write import PublishEvidenceError
from agentgraph.write.publish_storage import verify_publish_storage


@pytest.mark.parametrize(
    "target", ("publish", "plan.json", "push.json", "pull-request.json", "result.json")
)
def test_publish_storage_never_follows_links(tmp_path, target) -> None:
    run = tmp_path / "run"
    run.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    publish = run / "publish"
    try:
        if target == "publish":
            os.symlink(outside, publish, target_is_directory=True)
            relevant = ()
        else:
            publish.mkdir()
            os.symlink(sentinel, publish / target)
            relevant = (publish / target,)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(PublishEvidenceError, match="publish_storage_invalid"):
        verify_publish_storage(run, *relevant)

    assert sentinel.read_text(encoding="utf-8") == "unchanged"
