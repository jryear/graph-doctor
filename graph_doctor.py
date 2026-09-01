#!/usr/bin/env python3
"""Read-only integrity projection over one Hermes Kanban task neighborhood."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

REVIEW_WORDS = ("review", "verify", "verification", "qa", "quality assurance")
TERMINAL_STATUSES = {"done", "archived"}


def _connect_read_only(database_path: Path) -> sqlite3.Connection:
    resolved = database_path.expanduser().resolve()
    connection = sqlite3.connect("file:{}?mode=ro".format(resolved), uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _event(connection: sqlite3.Connection, event_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT id, task_id, run_id, kind, payload, created_at "
        "FROM task_events WHERE id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        raise ValueError("event {} does not exist".format(event_id))
    return row


def _task_exists(connection: sqlite3.Connection, task_id: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
    ).fetchone() is not None


def _affected_task_ids(connection: sqlite3.Connection, seed_task_id: str) -> List[str]:
    """Return the seed and its direct dependency neighborhood, deterministically."""
    rows = connection.execute(
        """
        SELECT parent_id AS task_id FROM task_links WHERE child_id = ?
        UNION
        SELECT child_id AS task_id FROM task_links WHERE parent_id = ?
        """,
        (seed_task_id, seed_task_id),
    ).fetchall()
    return sorted({seed_task_id, *(row["task_id"] for row in rows)})


def _tasks(connection: sqlite3.Connection, task_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    identifiers = list(task_ids)
    placeholders = ",".join("?" for _ in identifiers)
    rows = connection.execute(
        "SELECT id, title, body, assignee, status, workspace_path, branch_name, "
        "project_id, current_run_id FROM tasks WHERE id IN ({})".format(placeholders),
        identifiers,
    ).fetchall()
    return {row["id"]: dict(row) for row in rows}


def _links(connection: sqlite3.Connection, task_ids: Iterable[str]) -> List[Dict[str, str]]:
    identifiers = list(task_ids)
    placeholders = ",".join("?" for _ in identifiers)
    rows = connection.execute(
        "SELECT parent_id, child_id FROM task_links "
        "WHERE parent_id IN ({0}) AND child_id IN ({0}) "
        "ORDER BY parent_id, child_id".format(placeholders),
        identifiers + identifiers,
    ).fetchall()
    return [dict(row) for row in rows]


def _is_meaningful_event(event: Dict[str, Any]) -> bool:
    if event["kind"] != "heartbeat":
        return True
    if not event.get("payload"):
        return False
    try:
        payload = json.loads(event["payload"])
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and bool(payload.get("note"))


def _timeline(connection: sqlite3.Connection, task_ids: Iterable[str]) -> List[Dict[str, Any]]:
    identifiers = list(task_ids)
    placeholders = ",".join("?" for _ in identifiers)
    rows = connection.execute(
        "SELECT id, task_id, run_id, kind, payload, created_at "
        "FROM task_events WHERE task_id IN ({}) ORDER BY created_at, id".format(
            placeholders
        ),
        identifiers,
    ).fetchall()
    events = [dict(row) for row in rows]
    return [event for event in events if _is_meaningful_event(event)]


def _looks_like_review(task: Dict[str, Any]) -> bool:
    text = "{}\n{}".format(task.get("title") or "", task.get("body") or "").lower()
    return any(word in text for word in REVIEW_WORDS)


def _competing_review_findings(
    tasks: Dict[str, Dict[str, Any]],
    links: List[Dict[str, str]],
    timeline: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    review_requested: Set[str] = {
        event["task_id"] for event in timeline if event["kind"] == "review_requested"
    }
    children: Dict[str, List[str]] = {}
    for link in links:
        children.setdefault(link["parent_id"], []).append(link["child_id"])

    findings: List[Dict[str, Any]] = []
    for task_id in sorted(review_requested):
        downstream_reviews = sorted(
            child_id
            for child_id in children.get(task_id, [])
            if child_id in tasks and _looks_like_review(tasks[child_id])
        )
        if not downstream_reviews:
            continue
        active = tasks[task_id]["status"] not in TERMINAL_STATUSES
        findings.append(
            {
                "code": "competing_review_paths",
                "severity": "error" if active else "warning",
                "task_id": task_id,
                "downstream_review_task_ids": downstream_reviews,
                "message": (
                    "Task requested same-card review while also owning a downstream "
                    "review dependency; choose exactly one review topology."
                ),
            }
        )
    return findings


def inspect_graph(
    database_path: Path,
    task_id: Optional[str] = None,
    event_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Inspect one event- or task-seeded dependency neighborhood without writes."""
    if (task_id is None) == (event_id is None):
        raise ValueError("provide exactly one of task_id or event_id")

    connection = _connect_read_only(database_path)
    try:
        trigger: Dict[str, Any]
        if event_id is not None:
            event = _event(connection, event_id)
            task_id = event["task_id"]
            trigger = {"kind": "event", **dict(event)}
        else:
            if task_id is None or not _task_exists(connection, task_id):
                raise ValueError("task {} does not exist".format(task_id))
            trigger = {"kind": "task", "task_id": task_id}

        assert task_id is not None
        affected_ids = _affected_task_ids(connection, task_id)
        tasks = _tasks(connection, affected_ids)
        links = _links(connection, affected_ids)
        timeline = _timeline(connection, affected_ids)
        findings = _competing_review_findings(tasks, links, timeline)
        open_reviews = sorted(
            task["id"]
            for task in tasks.values()
            if _looks_like_review(task) and task["status"] not in TERMINAL_STATUSES
        )
        integration_locked = any(
            finding["severity"] == "error" for finding in findings
        ) or bool(open_reviews)
        return {
            "schema_version": 1,
            "read_only": True,
            "trigger": trigger,
            "affected_task_ids": affected_ids,
            "tasks": [tasks[identifier] for identifier in affected_ids],
            "links": links,
            "timeline": timeline,
            "findings": findings,
            "closure_frontier": {
                "open_review_task_ids": open_reviews,
                "integrity_error_count": sum(
                    finding["severity"] == "error" for finding in findings
                ),
            },
            "derived_state": "integration_locked" if integration_locked else "clear",
        }
    finally:
        connection.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only integrity check for an affected Hermes Kanban subgraph"
    )
    parser.add_argument("--db", required=True, type=Path, help="Kanban SQLite path")
    seed = parser.add_mutually_exclusive_group(required=True)
    seed.add_argument("--task-id")
    seed.add_argument("--event-id", type=int)
    parser.add_argument("--compact", action="store_true")
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        report = inspect_graph(
            arguments.db,
            task_id=arguments.task_id,
            event_id=arguments.event_id,
        )
    except (OSError, sqlite3.Error, ValueError) as error:
        print(json.dumps({"error": str(error)}, sort_keys=True))
        return 1
    print(json.dumps(report, indent=None if arguments.compact else 2, sort_keys=True))
    return 2 if report["closure_frontier"]["integrity_error_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
