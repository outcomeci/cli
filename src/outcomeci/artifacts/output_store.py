"""Portable, rebuildable SQLite projection of authoritative JSON step outputs.

The caller owns authorization, source-artifact retention checks and publication.
Snapshots are standalone SQLite backups, never copies of an active WAL file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import sqlite3
import uuid
from pathlib import Path
from typing import Any, TypedDict

SCHEMA_VERSION = 1
MAX_ROWS = 100
MAX_FIELDS = 100
MAX_PREDICATES = 20
MAX_ORDERING = 10
RUN_STATUSES = frozenset({"running", "succeeded", "failed", "cancelled"})
MAX_BYTES = 256 * 1024
MAX_DATABASE_BYTES = 256 * 1024 * 1024
PROVENANCE_FIELDS = frozenset(
    {
        "workspace_id",
        "workflow_id",
        "workflow_revision",
        "run_id",
        "step_id",
        "execution_instance_id",
        "attempt_id",
        "output_name",
        "artifact_id",
        "artifact_version",
        "content_hash",
        "completed_at",
        "output_id",
    }
)
SYSTEM_FIELDS = {"completed_at", "workflow_revision", "run_id", "step_id", "output_id"}


class SnapshotInfo(TypedDict):
    generation: int
    digest: str
    size_bytes: int


class QueryResult(TypedDict):
    records: list[dict[str, Any]]
    next_cursor: str | None
    truncated: bool
    snapshot: dict[str, Any]
    publication_status: str


class OutputStoreError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float) and math.isfinite(value):
        return "number"
    if isinstance(value, str):
        return "string"
    return "complex"


def _compatible(declared: str, value: Any) -> bool:
    actual = _type(value)
    return actual == declared or (declared == "number" and actual == "integer")


class OutputStore:
    """One workflow's history. All writes are transactional and idempotent."""

    def __init__(
        self, path: str | Path, workspace_id: str, workflow_id: str, read_only: bool = False
    ):
        try:
            self._initialize(path, workspace_id, workflow_id, read_only)
        except sqlite3.DatabaseError as exc:
            if hasattr(self, "db"):
                self.db.close()
            raise OutputStoreError("invalid_snapshot", "Invalid output database") from exc

    def _initialize(
        self, path: str | Path, workspace_id: str, workflow_id: str, read_only: bool
    ) -> None:
        self.path = Path(path)
        self.workspace_id = workspace_id
        self.workflow_id = workflow_id
        self.read_only = read_only
        if read_only:
            if not self.path.is_file():
                raise OutputStoreError(
                    "snapshot_unavailable", "Pinned output snapshot is unavailable"
                )
            self.db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.db = sqlite3.connect(self.path, timeout=30)
            self.path.chmod(0o600)
        if hasattr(self.db, "enable_load_extension"):
            self.db.enable_load_extension(False)
        self.db.execute("PRAGMA trusted_schema=OFF")
        self.db.row_factory = sqlite3.Row
        if self.path.stat().st_size > MAX_DATABASE_BYTES:
            self.close()
            raise OutputStoreError("capacity_exceeded", "Output database exceeds its size limit")
        if self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('trigger', 'view') LIMIT 1"
        ).fetchone():
            self.close()
            raise OutputStoreError(
                "invalid_snapshot", "Output database must not contain triggers or views"
            )
        if not read_only:
            with self.db:
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS outputs (id TEXT PRIMARY KEY, instance TEXT NOT NULL, record TEXT NOT NULL, accepted INTEGER NOT NULL DEFAULT 1)"
                )
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, status TEXT NOT NULL)"
                )
                self.db.execute("CREATE INDEX IF NOT EXISTS outputs_instance ON outputs(instance)")
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS indexed_values (output_id TEXT NOT NULL, field TEXT NOT NULL, declared_type TEXT NOT NULL, position INTEGER NOT NULL, value, PRIMARY KEY(output_id, field))"
                )
                self.db.execute(
                    "CREATE INDEX IF NOT EXISTS indexed_values_lookup ON indexed_values(field, declared_type, position, value, output_id)"
                )
                self.db.execute(
                    "CREATE INDEX IF NOT EXISTS outputs_scope ON outputs(accepted, instance)"
                )
                for key, value in {
                    "schema_version": str(SCHEMA_VERSION),
                    "workspace_id": workspace_id,
                    "workflow_id": workflow_id,
                    "generation": "0",
                    "incarnation": str(uuid.uuid4()),
                }.items():
                    self.db.execute("INSERT OR IGNORE INTO metadata VALUES (?, ?)", (key, value))
        try:
            meta = dict(self.db.execute("SELECT key, value FROM metadata").fetchall())
        except sqlite3.DatabaseError as exc:
            self.close()
            raise OutputStoreError("invalid_snapshot", "Invalid output database") from exc
        try:
            self.incarnation = str(uuid.UUID(meta["incarnation"]))
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            self.close()
            raise OutputStoreError(
                "invalid_snapshot", "Output database has no valid incarnation"
            ) from exc
        if meta.get("schema_version") != str(SCHEMA_VERSION):
            self.close()
            raise OutputStoreError("unsupported_schema", "Unsupported output database schema")
        if meta.get("workspace_id") != workspace_id or meta.get("workflow_id") != workflow_id:
            self.close()
            raise OutputStoreError(
                "identity_mismatch", "Output database belongs to another workflow"
            )

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> OutputStore:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _advance(self) -> None:
        self.db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='generation'")

    def ingest(self, record: dict[str, Any]) -> bool:
        try:
            record = json.loads(_json(record))
        except (TypeError, ValueError) as exc:
            raise OutputStoreError(
                "invalid_output", "Output must contain finite JSON values"
            ) from exc
        if not isinstance(record, dict):
            raise OutputStoreError("invalid_output", "Output record must be an object")
        for field in (
            "run_id",
            "step_id",
            "workflow_revision",
            "artifact_id",
            "content_hash",
            "completed_at",
        ):
            if not isinstance(record.get(field), str) or not record[field]:
                raise OutputStoreError("invalid_output", f"Missing output provenance: {field}")
        if not isinstance(record.get("values"), dict):
            raise OutputStoreError("invalid_output", "Output values must be an object")
        for field, value in {
            "workspace_id": self.workspace_id,
            "workflow_id": self.workflow_id,
        }.items():
            if record.get(field, value) != value:
                raise OutputStoreError("identity_mismatch", "Output belongs to another workflow")
            record[field] = value
        record.setdefault("execution_instance_id", record["step_id"])
        record.setdefault("attempt_id", "1")
        record.setdefault("output_name", "returns")
        record.setdefault("artifact_version", "1")
        record.setdefault("indexed_fields", [])
        record.setdefault("field_types", {})
        for field in PROVENANCE_FIELDS - {"output_id"}:
            if not isinstance(record.get(field), str) or not record[field]:
                raise OutputStoreError("invalid_output", f"Invalid output provenance: {field}")
        if not isinstance(record["indexed_fields"], list) or any(
            not isinstance(field, str) for field in record["indexed_fields"]
        ):
            raise OutputStoreError("invalid_schema", "Indexed fields must be a string list")
        if (
            len(record["indexed_fields"]) > MAX_FIELDS
            or len(set(record["indexed_fields"])) != len(record["indexed_fields"])
            or set(record["indexed_fields"]) & SYSTEM_FIELDS
        ):
            raise OutputStoreError(
                "invalid_schema",
                "Indexed fields exceed limits, duplicate, or use reserved metadata names",
            )
        if not isinstance(record["field_types"], dict):
            raise OutputStoreError("invalid_schema", "Field types must be an object")
        for field in record["indexed_fields"]:
            value = record["values"].get(field)
            declared = record["field_types"].get(field)
            if not isinstance(declared, str) or declared not in {
                "string",
                "integer",
                "number",
                "boolean",
            }:
                raise OutputStoreError(
                    "invalid_schema", f"Indexed field {field} needs a scalar type"
                )
            if isinstance(value, int) and not -(2**63) <= value < 2**63:
                raise OutputStoreError(
                    "type_mismatch", f"Indexed integer {field} exceeds signed 64-bit range"
                )
            if value is not None and not _compatible(declared, value):
                raise OutputStoreError("type_mismatch", f"Invalid indexed value for {field}")
        identity = [
            record[k]
            for k in (
                "workspace_id",
                "workflow_id",
                "run_id",
                "step_id",
                "execution_instance_id",
                "output_name",
            )
        ]
        instance = _hash(identity)
        record["output_id"] = _hash([*identity, record["attempt_id"]])
        status = record.pop("run_status", "running")
        if not isinstance(status, str) or status not in RUN_STATUSES:
            raise OutputStoreError("invalid_output", "Invalid run status")
        serialized = _json(record)
        if len(serialized.encode()) > MAX_BYTES:
            raise OutputStoreError("output_too_large", "Output projection exceeds size limit")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            previous = self.db.execute(
                "SELECT record FROM outputs WHERE id=?", (record["output_id"],)
            ).fetchone()
            if previous:
                if previous[0] != serialized:
                    raise OutputStoreError(
                        "output_conflict", "Output identity already has different content"
                    )
                return False
            if self.path.stat().st_size + len(serialized.encode()) > MAX_DATABASE_BYTES:
                raise OutputStoreError(
                    "capacity_exceeded", "Workflow output database exceeds its size limit"
                )
            self.db.execute("UPDATE outputs SET accepted=0 WHERE instance=?", (instance,))
            self.db.execute(
                "INSERT INTO outputs VALUES (?, ?, ?, 1)",
                (record["output_id"], instance, serialized),
            )
            for field in record["indexed_fields"]:
                value = record["values"].get(field)
                position = 2 if field not in record["values"] else 1 if value is None else 0
                self.db.execute(
                    "INSERT INTO indexed_values VALUES (?, ?, ?, ?, ?)",
                    (record["output_id"], field, record["field_types"][field], position, value),
                )
            self.db.execute("INSERT OR IGNORE INTO runs VALUES (?, ?)", (record["run_id"], status))
            self._advance()
            pages = self.db.execute("PRAGMA page_count").fetchone()[0]
            page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
            if pages * page_size > MAX_DATABASE_BYTES:
                raise OutputStoreError(
                    "capacity_exceeded", "Workflow output database exceeds its size limit"
                )
        return True

    def set_run_status(self, run_id: str, status: str) -> None:
        if (
            not isinstance(run_id, str)
            or not run_id
            or not isinstance(status, str)
            or status not in RUN_STATUSES
        ):
            raise OutputStoreError("invalid_output", "Invalid run identity or status")
        with self.db:
            self.db.execute(
                "INSERT INTO runs VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET status=excluded.status",
                (run_id, status),
            )
            self._advance()

    def output_id(self, record: dict[str, Any]) -> str:
        """Canonical immutable output identity, independent of artifact uploads."""
        return _hash(
            [
                self.workspace_id,
                self.workflow_id,
                record["run_id"],
                record["step_id"],
                record.get("execution_instance_id", record["step_id"]),
                record.get("output_name", "returns"),
                record.get("attempt_id", "1"),
            ]
        )

    def existing_reference(self, record: dict[str, Any]) -> dict[str, str] | None:
        """Find immutable source provenance when a checkpoint reuploads an output.

        The caller must verify matching content hashes and live source retention;
        this helper never changes an output or silently adopts new source bytes.
        """
        output_id = self.output_id(record)
        fields = ("artifact_id", "artifact_version", "content_hash", "output_id", "completed_at")
        selected = ", ".join(f"json_extract(record, '$.{field}') AS {field}" for field in fields)
        row = self.db.execute(f"SELECT {selected} FROM outputs WHERE id=?", (output_id,)).fetchone()
        return dict(row) if row else None

    def artifact_references(self) -> list[dict[str, Any]]:
        """References for the caller's live retention/deletion barrier."""
        fields = ("artifact_id", "artifact_version", "content_hash", "run_id", "output_id")
        selected = ", ".join(f"json_extract(record, '$.{field}') AS {field}" for field in fields)
        return [
            dict(row) for row in self.db.execute(f"SELECT {selected} FROM outputs WHERE accepted=1")
        ]

    def delete_artifact(self, artifact_id: str) -> None:
        with self.db:
            self.db.execute(
                "DELETE FROM indexed_values WHERE output_id IN (SELECT id FROM outputs WHERE json_extract(record, '$.artifact_id')=?)",
                (artifact_id,),
            )
            self.db.execute(
                "DELETE FROM outputs WHERE json_extract(record, '$.artifact_id')=?", (artifact_id,)
            )
            self._advance()

    def snapshot(self, destination: str | Path) -> SnapshotInfo:
        destination = Path(destination)
        if destination.resolve() == self.path.resolve() or destination.exists():
            raise OutputStoreError("snapshot_conflict", "Snapshot destination must be a new file")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            destination.touch(mode=0o600, exist_ok=False)
        except FileExistsError as exc:
            raise OutputStoreError(
                "snapshot_conflict", "Snapshot destination already exists"
            ) from exc
        with sqlite3.connect(destination) as target:
            self.db.backup(target)
            generation = int(
                target.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()[0]
            )
        destination.chmod(0o600)
        return {
            "generation": generation,
            "digest": hashlib.sha256(destination.read_bytes()).hexdigest(),
            "size_bytes": destination.stat().st_size,
        }

    def query(
        self,
        request: dict[str, Any],
        *,
        source: dict[str, Any],
        consuming_run_id: str | None = None,
    ) -> QueryResult:
        """Evaluate against one SQLite read transaction, including cursor identity."""
        self.db.execute("BEGIN")
        try:
            return self._query(request, source=source, consuming_run_id=consuming_run_id)
        except sqlite3.DatabaseError as exc:
            raise OutputStoreError(
                "invalid_snapshot", "Output database query failed integrity checks"
            ) from exc
        finally:
            self.db.rollback()

    def _query(
        self,
        request: dict[str, Any],
        *,
        source: dict[str, Any],
        consuming_run_id: str | None = None,
    ) -> QueryResult:
        if not isinstance(request, dict) or not isinstance(source, dict):
            raise OutputStoreError("invalid_query", "Request and source must be objects")
        if source.get("workflow", "self") != "self":
            raise OutputStoreError("forbidden_source", "Only this workflow may be queried")
        if not isinstance(source.get("run_status", "succeeded"), str) or source.get(
            "run_status", "succeeded"
        ) not in {"succeeded", "step_succeeded"}:
            raise OutputStoreError("invalid_query", "Invalid source run eligibility")
        for key in ("steps", "fields"):
            if (
                not isinstance(source.get(key), list)
                or not source[key]
                or len(source[key]) > MAX_FIELDS
                or any(not isinstance(v, str) for v in source[key])
            ):
                raise OutputStoreError(
                    "invalid_query", f"Source {key} must be a nonempty string list"
                )
        if (
            type(source.get("max_results", 20)) is not int
            or not 1 <= source.get("max_results", 20) <= MAX_ROWS
        ):
            raise OutputStoreError("query_budget", "Invalid source row budget")
        for key in ("workflow_revision", "output_id", "step_id", "cursor"):
            if key in request and not isinstance(request[key], str):
                raise OutputStoreError("invalid_query", f"{key} must be a string")
        if "required" in request and type(request["required"]) is not bool:
            raise OutputStoreError("invalid_query", "required must be a boolean")
        unknown = set(request) - {
            "where",
            "order_by",
            "limit",
            "cursor",
            "workflow_revision",
            "output_id",
            "fields",
            "step_id",
            "required",
        }
        if unknown:
            raise OutputStoreError("invalid_query", "Unsupported query attributes")
        fields = request.get("fields", source.get("fields", []))
        if (
            not isinstance(fields, list)
            or not fields
            or len(fields) > MAX_FIELDS
            or any(not isinstance(field, str) for field in fields)
            or not set(fields) <= set(source.get("fields", []))
        ):
            raise OutputStoreError(
                "forbidden_field", "Query projection exceeds source field grants"
            )
        limit = request.get("limit", min(20, source.get("max_results", 20)))
        if type(limit) is not int or not 1 <= limit <= min(MAX_ROWS, source.get("max_results", 20)):
            raise OutputStoreError("query_budget", "Query row limit exceeds source budget")
        steps = source.get("steps", [])
        if request.get("step_id") and request["step_id"] not in steps:
            raise OutputStoreError("forbidden_step", "Producing step is not granted")
        where = request.get("where", {})
        order = request.get("order_by", [{"field": "completed_at", "direction": "desc"}])
        if not isinstance(where, dict) or not isinstance(order, list):
            raise OutputStoreError("invalid_query", "Invalid predicates or ordering")
        if len(where) > MAX_PREDICATES or len(order) > MAX_ORDERING:
            raise OutputStoreError("query_budget", "Query exceeds predicate or ordering limit")
        if (
            sum(len(predicates) for predicates in where.values() if isinstance(predicates, dict))
            > MAX_PREDICATES
        ):
            raise OutputStoreError("query_budget", "Query exceeds predicate limit")
        query_fields = set(where)
        for item in order:
            if (
                not isinstance(item, dict)
                or set(item) != {"field", "direction"}
                or not isinstance(item.get("field"), str)
                or not isinstance(item.get("direction"), str)
                or item["direction"] not in {"asc", "desc"}
            ):
                raise OutputStoreError("invalid_query", "Invalid ordering")
            query_fields.add(item["field"])
        if not query_fields - SYSTEM_FIELDS <= set(source.get("fields", [])):
            raise OutputStoreError("forbidden_field", "Query field is not granted")
        for predicates in where.values():
            if not isinstance(predicates, dict) or not predicates:
                raise OutputStoreError("invalid_query", "Predicates must be nonempty objects")
            for op, value in predicates.items():
                if (
                    op not in {"eq", "lt", "lte", "gt", "gte", "is_null"}
                    or (type(value) is int and not -(2**63) <= value < 2**63)
                    or (op == "is_null" and type(value) is not bool)
                    or (op != "is_null" and _type(value) in {"null", "complex"})
                ):
                    raise OutputStoreError("invalid_query", "Unsupported predicate or value")
        generation = int(
            self.db.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()[0]
        )
        fingerprint = _hash(
            {
                "workspace": self.workspace_id,
                "workflow": self.workflow_id,
                "generation": generation,
                "incarnation": self.incarnation,
                "query": {k: v for k, v in request.items() if k != "cursor"},
                "source": source,
                "consumer": consuming_run_id,
            }
        )
        offset = 0
        if request.get("cursor"):
            if len(request["cursor"]) > 2048:
                raise OutputStoreError("invalid_cursor", "Cursor exceeds size limit")
            try:
                token = json.loads(base64.urlsafe_b64decode(request["cursor"]))
                if (
                    token["query"] != fingerprint
                    or type(token["offset"]) is not int
                    or not 0 <= token["offset"] < 2**63
                ):
                    raise ValueError
                offset = token["offset"]
            except (ValueError, KeyError, TypeError) as exc:
                raise OutputStoreError(
                    "invalid_cursor", "Cursor does not match this snapshot and query"
                ) from exc
        clauses = [
            "o.accepted=1",
            "json_extract(o.record, '$.step_id') IN (" + ",".join("?" for _ in steps) + ")",
        ]
        params: list[Any] = list(steps)
        if consuming_run_id is not None:
            clauses.append("json_extract(o.record, '$.run_id') != ?")
            params.append(consuming_run_id)
        if source.get("run_status", "succeeded") == "succeeded":
            clauses.append(
                "EXISTS (SELECT 1 FROM runs r WHERE r.id=json_extract(o.record, '$.run_id') AND r.status='succeeded')"
            )
        for key in ("workflow_revision", "output_id", "step_id"):
            if request.get(key) is not None:
                clauses.append(f"json_extract(o.record, '$.{key}') = ?")
                params.append(request[key])
        base = " FROM outputs o WHERE " + " AND ".join(clauses)
        schemas: dict[str, set[str]] = {}
        for field in query_fields - SYSTEM_FIELDS:
            rows = self.db.execute(
                "SELECT DISTINCT (SELECT v.declared_type FROM indexed_values v WHERE v.output_id=o.id AND v.field=?)"
                + base,
                [field, *params],
            ).fetchall()
            if any(row[0] is None for row in rows):
                raise OutputStoreError(
                    "unindexed_field", f"Field {field} was not indexed in matching history"
                )
            schemas[field] = {row[0] for row in rows}
        for field, types in schemas.items():
            if len(types) > 1:
                raise OutputStoreError(
                    "schema_incompatible",
                    f"Field {field} has incompatible historical schemas; select a workflow revision",
                )
            if types:
                declared = next(iter(types))
                for op, value in where.get(field, {}).items():
                    if op != "is_null" and (
                        not _compatible(declared, value) or (declared == "boolean" and op != "eq")
                    ):
                        raise OutputStoreError(
                            "type_mismatch", f"Predicate type does not match {field}"
                        )
        for field in query_fields & SYSTEM_FIELDS:
            for op, value in where.get(field, {}).items():
                if op != "is_null" and not isinstance(value, str):
                    raise OutputStoreError(
                        "type_mismatch", f"Metadata field {field} requires a string"
                    )

        operators = {"eq": "=", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}
        for field, predicates in where.items():
            for op, expected in predicates.items():
                if field in SYSTEM_FIELDS:
                    expression = f"json_extract(o.record, '$.{field}')"
                    if op == "is_null":
                        base += f" AND {expression} IS {'NULL' if expected else 'NOT NULL'}"
                    else:
                        base += f" AND {expression} {operators[op]} ?"
                        params.append(expected)
                else:
                    base += " AND o.id IN (SELECT output_id FROM indexed_values WHERE field=? AND "
                    params.append(field)
                    if op == "is_null":
                        base += "position=?)"
                        params.append(1 if expected else 0)
                    else:
                        base += f"position=0 AND value {operators[op]} ?)"
                        params.append(expected)
        ordering = []
        order_params: list[Any] = []
        for item in order:
            field, direction = item["field"], item["direction"]
            if field in SYSTEM_FIELDS:
                ordering.append(f"json_extract(o.record, '$.{field}') {direction}")
            else:
                ordering.append(
                    "(SELECT position FROM indexed_values v WHERE v.output_id=o.id AND v.field=?) ASC"
                )
                ordering.append(
                    f"(SELECT value FROM indexed_values v WHERE v.output_id=o.id AND v.field=?) {direction}"
                )
                order_params.extend([field, field])
        ordering.append("o.id ASC")
        statement = (
            "SELECT o.record" + base + " ORDER BY " + ", ".join(ordering) + " LIMIT ? OFFSET ?"
        )
        records = [
            json.loads(row[0])
            for row in self.db.execute(statement, [*params, *order_params, limit + 1, offset])
        ]
        page = []
        size = 2
        for record in records[:limit]:
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("values"), dict)
                or record.get("workspace_id") != self.workspace_id
                or record.get("workflow_id") != self.workflow_id
                or any(not isinstance(record.get(key), str) for key in PROVENANCE_FIELDS)
            ):
                raise OutputStoreError("invalid_snapshot", "Invalid output record in snapshot")
            projected = {k: v for k, v in record.items() if k in PROVENANCE_FIELDS}
            projected["values"] = {k: record["values"][k] for k in fields if k in record["values"]}
            count = len(_json(projected).encode()) + 1
            if count + 2 > MAX_BYTES:
                raise OutputStoreError(
                    "result_too_large", "A single output exceeds the query byte budget"
                )
            if size + count > MAX_BYTES:
                break
            page.append(projected)
            size += count
        more = len(page) < len(records)
        cursor = (
            base64.urlsafe_b64encode(
                _json({"query": fingerprint, "offset": offset + len(page)}).encode()
            ).decode()
            if more
            else None
        )
        if request.get("required") and not page:
            raise OutputStoreError(
                "required_output_missing", "Required output query returned no matches"
            )
        return {
            "records": page,
            "next_cursor": cursor,
            "truncated": more,
            "snapshot": {"generation": generation},
            "publication_status": "ready",
        }
