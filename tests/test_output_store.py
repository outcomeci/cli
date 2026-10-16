import hashlib
import json

import pytest

from outcomeci.artifacts.output_store import OutputStore, OutputStoreError


def record(run="r1", **changes):
    values = changes.pop("values", {"customer": "a", "score": 2, "secret": "hidden"})
    result = dict(
        run_id=run,
        step_id="assess",
        workflow_revision="v1",
        artifact_id=f"artifact-{run}",
        content_hash=hashlib.sha256(json.dumps(values).encode()).hexdigest(),
        completed_at="2026-10-16T00:00:00Z",
        values=values,
        indexed_fields=["customer", "score"],
        field_types={"customer": "string", "score": "integer"},
        run_status="succeeded",
    )
    return {**result, **changes}


@pytest.fixture
def store(tmp_path):
    with OutputStore(tmp_path / "history.sqlite", "ws", "wf") as value:
        yield value


SOURCE = {"steps": ["assess"], "fields": ["customer", "score"], "max_results": 20}


def test_idempotence_conflict_and_projection(store):
    assert store.ingest(record())
    assert not store.ingest(record())
    with pytest.raises(OutputStoreError, match="different content"):
        store.ingest(record(values={"customer": "b", "score": 3}))
    result = store.query({}, source=SOURCE)
    assert len(result["records"]) == 1
    assert result["records"][0]["values"] == {"customer": "a", "score": 2}
    assert "hidden" not in json.dumps(result)


def test_run_eligibility_and_consumer_exclusion(store):
    store.ingest(record("r1", run_status="running"))
    assert store.query({}, source=SOURCE)["records"] == []
    assert len(store.query({}, source={**SOURCE, "run_status": "step_succeeded"})["records"]) == 1
    store.set_run_status("r1", "succeeded")
    assert store.query({}, source=SOURCE, consuming_run_id="r1")["records"] == []
    assert len(store.query({}, source=SOURCE)["records"]) == 1


def test_snapshots_preserve_history_and_identity(store, tmp_path):
    store.ingest(record())
    path = tmp_path / "snapshot.sqlite"
    metadata = store.snapshot(path)
    assert metadata["digest"] == hashlib.sha256(path.read_bytes()).hexdigest()
    store.ingest(record("r2"))
    with OutputStore(path, "ws", "wf", read_only=True) as pinned:
        assert len(pinned.query({}, source=SOURCE)["records"]) == 1
    with pytest.raises(OutputStoreError, match="another workflow"):
        OutputStore(path, "ws", "other", read_only=True)


def test_pagination_bound_to_snapshot_query_and_scope(store):
    for run in ("r1", "r2", "r3"):
        store.ingest(record(run))
    first = store.query({"limit": 1}, source=SOURCE)
    second = store.query({"limit": 1, "cursor": first["next_cursor"]}, source=SOURCE)
    assert first["records"][0]["output_id"] != second["records"][0]["output_id"]
    with pytest.raises(OutputStoreError, match="Cursor"):
        store.query({"limit": 2, "cursor": first["next_cursor"]}, source=SOURCE)
    store.ingest(record("r4"))
    with pytest.raises(OutputStoreError, match="Cursor"):
        store.query({"limit": 1, "cursor": first["next_cursor"]}, source=SOURCE)


def test_null_missing_and_type_validation(store):
    store.ingest(record("missing", values={"customer": "a"}))
    store.ingest(record("null", values={"customer": "a", "score": None}))
    store.ingest(record("value"))
    result = store.query({"where": {"score": {"is_null": True}}}, source=SOURCE)
    assert [r["run_id"] for r in result["records"]] == ["null"]
    result = store.query({"where": {"score": {"gte": 2}}}, source=SOURCE)
    assert [r["run_id"] for r in result["records"]] == ["value"]
    with pytest.raises(OutputStoreError, match="Predicate type"):
        store.query({"where": {"score": {"eq": True}}}, source=SOURCE)
    with pytest.raises(OutputStoreError, match="not granted"):
        store.query({"where": {"secret": {"eq": "hidden"}}}, source=SOURCE)


def test_schema_revision_filter(store):
    store.ingest(record())
    store.ingest(
        record(
            "r2",
            workflow_revision="v2",
            values={"customer": "a", "score": "high"},
            field_types={"customer": "string", "score": "string"},
        )
    )
    with pytest.raises(OutputStoreError, match="incompatible"):
        store.query({"where": {"score": {"eq": 2}}}, source=SOURCE)
    result = store.query({"workflow_revision": "v1", "where": {"score": {"eq": 2}}}, source=SOURCE)
    assert len(result["records"]) == 1


def test_replaced_attempt_and_loop_instances(store):
    store.ingest(record(attempt_id="1"))
    store.ingest(record(attempt_id="2"))
    store.ingest(record(execution_instance_id="loop-2"))
    results = store.query({}, source=SOURCE)["records"]
    assert len(results) == 2
    assert {(r["execution_instance_id"], r["attempt_id"]) for r in results} == {
        ("assess", "2"),
        ("loop-2", "1"),
    }


def test_exact_get_cannot_bypass_grants_or_deletion(store):
    store.ingest(record())
    output = store.query({}, source=SOURCE)["records"][0]
    with pytest.raises(OutputStoreError, match="projection"):
        store.query({"output_id": output["output_id"], "fields": ["secret"]}, source=SOURCE)
    assert len(store.artifact_references()) == 1
    store.delete_artifact("artifact-r1")
    assert store.query({"output_id": output["output_id"]}, source=SOURCE)["records"] == []


def test_required_and_budgets(store):
    with pytest.raises(OutputStoreError, match="Required"):
        store.query({"required": True}, source=SOURCE)
    with pytest.raises(OutputStoreError, match="row limit"):
        store.query({"limit": 21}, source=SOURCE)


def test_concurrent_writers_preserve_all_outputs(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "concurrent.sqlite"
    with OutputStore(path, "ws", "wf"):
        pass

    def write(number):
        with OutputStore(path, "ws", "wf") as database:
            return database.ingest(record(f"run-{number}"))

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert all(executor.map(write, range(20)))
    with OutputStore(path, "ws", "wf") as database:
        assert len(database.query({}, source=SOURCE)["records"]) == 20


def test_read_only_snapshot_and_schema_rejection(store, tmp_path):
    import sqlite3

    path = tmp_path / "readonly.sqlite"
    store.snapshot(path)
    with (
        OutputStore(path, "ws", "wf", read_only=True) as snapshot,
        pytest.raises(sqlite3.OperationalError),
    ):
        snapshot.ingest(record())
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
    with pytest.raises(OutputStoreError) as error:
        OutputStore(path, "ws", "wf", read_only=True)
    assert error.value.code == "unsupported_schema"


def test_ordering_null_missing_and_ties(store):
    store.ingest(record("missing", values={"customer": "a"}))
    store.ingest(record("null", values={"customer": "a", "score": None}))
    store.ingest(record("low", values={"customer": "a", "score": 1}))
    store.ingest(record("high", values={"customer": "a", "score": 5}))
    for direction, expected in [
        ("asc", ["low", "high", "null", "missing"]),
        ("desc", ["high", "low", "null", "missing"]),
    ]:
        result = store.query(
            {"order_by": [{"field": "score", "direction": direction}]}, source=SOURCE
        )
        assert [r["run_id"] for r in result["records"]] == expected


def test_sql_predicates_use_index_and_return_bounded_page(store):
    for n in range(50):
        store.ingest(record(str(n), values={"customer": "a" if n % 2 else "b", "score": n}))
    statements = []
    store.db.set_trace_callback(statements.append)
    result = store.query(
        {
            "where": {"customer": {"eq": "a"}, "score": {"gte": 40}},
            "order_by": [{"field": "score", "direction": "asc"}],
            "limit": 2,
        },
        source=SOURCE,
    )
    assert [r["values"]["score"] for r in result["records"]] == [41, 43]
    assert result["truncated"] and result["next_cursor"]
    sql = next(s for s in statements if s.startswith("SELECT o.record"))
    assert "LIMIT 3 OFFSET 0" in sql
    plan = store.db.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
    assert any("indexed_values_lookup" in str(tuple(row)) for row in plan)


def test_query_byte_budget_reports_truncation(store, monkeypatch):
    import outcomeci.artifacts.output_store as module

    for n in range(3):
        store.ingest(record(str(n)))
    one = store.query({"limit": 1}, source=SOURCE)["records"][0]
    budget = len(json.dumps(one, sort_keys=True, separators=(",", ":")).encode()) + 3
    monkeypatch.setattr(module, "MAX_BYTES", budget)
    result = store.query({}, source=SOURCE)
    assert len(result["records"]) == 1
    assert result["truncated"] and result["next_cursor"]
    monkeypatch.setattr(module, "MAX_BYTES", 10)
    with pytest.raises(OutputStoreError) as error:
        store.query({}, source=SOURCE)
    assert error.value.code == "result_too_large"


def test_unknown_metadata_never_bypasses_field_projection(store):
    store.ingest(record(private_note="do-not-disclose", nested={"secret": "do-not-disclose"}))
    result = store.query({}, source=SOURCE)
    assert "do-not-disclose" not in json.dumps(result)
    assert "private_note" not in result["records"][0]


def test_untrusted_snapshot_rejects_views_and_triggers(store, tmp_path):
    import sqlite3

    snapshot = tmp_path / "unsafe.sqlite"
    store.snapshot(snapshot)
    with sqlite3.connect(snapshot) as db:
        db.execute("CREATE VIEW unsafe AS SELECT 1")
    with pytest.raises(OutputStoreError) as error:
        OutputStore(snapshot, "ws", "wf", read_only=True)
    assert error.value.code == "invalid_snapshot"


def test_query_transaction_prevents_mixed_generations(store):
    store.db.execute("PRAGMA journal_mode=WAL")
    store.ingest(record())
    generation = store.query({}, source=SOURCE)["snapshot"]["generation"]
    changed = False

    def interleave(statement):
        nonlocal changed
        if statement.startswith("SELECT DISTINCT") and not changed:
            changed = True
            with OutputStore(store.path, "ws", "wf") as writer:
                writer.ingest(record("new-run"))

    store.db.set_trace_callback(interleave)
    result = store.query({"where": {"score": {"eq": 2}}}, source=SOURCE)
    store.db.set_trace_callback(None)
    assert changed
    assert result["snapshot"]["generation"] == generation
    assert len(result["records"]) == 1
    assert len(store.query({}, source=SOURCE)["records"]) == 2


def test_malformed_cursor_is_bounded_and_typed(store):
    import base64

    for cursor in ["x" * 3000, "not base64", base64.urlsafe_b64encode(b"[]").decode()]:
        with pytest.raises(OutputStoreError) as error:
            store.query({"cursor": cursor}, source=SOURCE)
        assert error.value.code == "invalid_cursor"


@pytest.mark.parametrize(
    "change",
    [
        {"indexed_fields": ["score", "score"]},
        {"indexed_fields": ["output_id"], "field_types": {"output_id": "string"}},
        {"run_status": "invented"},
    ],
)
def test_invalid_projection_contract_rejected(store, change):
    with pytest.raises(OutputStoreError):
        store.ingest(record(**change))
    assert store.query({}, source=SOURCE)["records"] == []


def test_query_complexity_limits(store):
    with pytest.raises(OutputStoreError) as error:
        store.query(
            {"order_by": [{"field": "completed_at", "direction": "asc"}] * 11}, source=SOURCE
        )
    assert error.value.code == "query_budget"
    with pytest.raises(OutputStoreError) as error:
        store.query({"where": {f"field_{n}": {"eq": "a"} for n in range(21)}}, source=SOURCE)
    assert error.value.code == "query_budget"


def test_corrupt_database_reports_typed_error(tmp_path):
    path = tmp_path / "corrupt.sqlite"
    path.write_bytes(b"not a sqlite database")
    with pytest.raises(OutputStoreError) as error:
        OutputStore(path, "ws", "wf", read_only=True)
    assert error.value.code == "invalid_snapshot"


def test_existing_reference_preserves_first_artifact_across_checkpoint_uploads(store):
    original = record()
    assert store.existing_reference(original) is None
    store.ingest(original)
    reference = store.existing_reference(
        {**original, "artifact_id": "new-upload", "artifact_version": "2"}
    )
    assert reference["artifact_id"] == original["artifact_id"]
    assert reference["artifact_version"] == "1"
    assert reference["content_hash"] == original["content_hash"]
    assert reference["completed_at"] == original["completed_at"]
    assert store.existing_reference({**original, "attempt_id": "2"}) is None


def test_cursor_rejects_rebuilt_database_with_same_generation(store, tmp_path):
    store.ingest(record("first"))
    store.ingest(record("second"))
    first = store.query({"limit": 1}, source=SOURCE)
    with OutputStore(tmp_path / "rebuilt.sqlite", "ws", "wf") as rebuilt:
        rebuilt.ingest(record("replacement-first"))
        rebuilt.ingest(record("replacement-second"))
        assert rebuilt.query({}, source=SOURCE)["snapshot"] == first["snapshot"]
        with pytest.raises(OutputStoreError) as error:
            rebuilt.query({"limit": 1, "cursor": first["next_cursor"]}, source=SOURCE)
        assert error.value.code == "invalid_cursor"
    backup = tmp_path / "same-history.sqlite"
    store.snapshot(backup)
    with OutputStore(backup, "ws", "wf", read_only=True) as pinned:
        next_page = pinned.query({"limit": 1, "cursor": first["next_cursor"]}, source=SOURCE)
        assert len(next_page["records"]) == 1
        assert next_page["records"][0]["output_id"] != first["records"][0]["output_id"]


def test_public_output_identity_matches_ingested_record(store):
    value = record()
    expected = store.output_id(value)
    store.ingest(value)
    assert store.query({}, source=SOURCE)["records"][0]["output_id"] == expected
