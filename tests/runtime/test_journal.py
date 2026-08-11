import json

import pytest

from agentgraph.runtime.codec import canonical_json_bytes
from agentgraph.runtime.errors import JournalCorruptionError, TruncatedJournalError
from agentgraph.runtime.journal import Journal, JournalRecordType


def make_journal(tmp_path):
    journal = Journal(tmp_path / "journal.jsonl", "run_a")
    journal.initialize()
    journal.append(JournalRecordType.RUN_STARTED, {"version": 0})
    journal.append(JournalRecordType.NODE_STARTED, {"node_id": "START"})
    return journal


def test_journal_sequence_and_checksum_chain(tmp_path) -> None:
    records = make_journal(tmp_path).load()
    assert [item.seq for item in records] == [1, 2]
    assert records[1].previous_checksum == records[0].checksum


@pytest.mark.parametrize("mutation", ["checksum", "previous", "sequence", "gap", "run", "type"])
def test_journal_tampering_fails_closed(tmp_path, mutation: str) -> None:
    journal = make_journal(tmp_path)
    lines = journal.path.read_bytes().splitlines()
    record = json.loads(lines[1])
    if mutation == "checksum":
        record["checksum"] = "sha256:" + "0" * 64
    elif mutation == "previous":
        record["previous_checksum"] = "sha256:" + "1" * 64
    elif mutation == "sequence":
        record["seq"] = 1
    elif mutation == "gap":
        record["seq"] = 4
    elif mutation == "run":
        record["run_id"] = "other"
    else:
        record["record_type"] = "UNKNOWN"
    lines[1] = canonical_json_bytes(record)
    journal.path.write_bytes(b"\n".join(lines) + b"\n")
    with pytest.raises(JournalCorruptionError):
        journal.load()


def test_corrupt_middle_line_is_not_treated_as_truncated_tail(tmp_path) -> None:
    journal = make_journal(tmp_path)
    lines = journal.path.read_bytes().splitlines()
    journal.path.write_bytes(lines[0] + b"\n{" + b"\n" + lines[1] + b"\n")
    with pytest.raises(JournalCorruptionError):
        journal.load()


def test_truncated_tail_is_preserved_then_repaired_with_note(tmp_path) -> None:
    journal = make_journal(tmp_path)
    with journal.path.open("ab") as stream:
        stream.write(b'{"partial":')
    with pytest.raises(TruncatedJournalError):
        journal.load()
    note = journal.repair_truncated_tail(tmp_path / "recovery")
    assert note.record_type is JournalRecordType.RECOVERY_NOTE
    assert list((tmp_path / "recovery").glob("*.tail"))
    assert journal.load()[-1].record_type is JournalRecordType.RECOVERY_NOTE
