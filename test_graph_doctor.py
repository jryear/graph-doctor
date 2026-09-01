from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from graph_doctor import inspect_graph


class GraphDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "kanban.db"
        connection = sqlite3.connect(self.database_path)
        connection.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                body TEXT,
                assignee TEXT,
                status TEXT NOT NULL,
                workspace_path TEXT,
                branch_name TEXT,
                project_id TEXT,
                current_run_id INTEGER
            );
            CREATE TABLE task_links (
                parent_id TEXT NOT NULL,
                child_id TEXT NOT NULL,
                PRIMARY KEY (parent_id, child_id)
            );
            CREATE TABLE task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                run_id INTEGER,
                kind TEXT NOT NULL,
                payload TEXT,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY,
                task_id TEXT NOT NULL,
                profile TEXT,
                status TEXT NOT NULL,
                outcome TEXT,
                summary TEXT,
                metadata TEXT,
                error TEXT,
                started_at INTEGER NOT NULL,
                ended_at INTEGER
            );
            """
        )
        connection.executemany(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "t_impl",
                    "Fix stable cursor pagination",
                    "Implement the bounded correction.",
                    "coder",
                    "review",
                    "/repo/.worktrees/t_impl",
                    "feature/t_impl",
                    "p_test",
                    10,
                ),
                (
                    "t_review",
                    "Independently review cursor correction",
                    "Strictly read-only review of the parent's exact commit.",
                    "kiki",
                    "todo",
                    "/repo/.worktrees/t_review",
                    "review/t_review",
                    "p_test",
                    None,
                ),
            ],
        )
        connection.execute("INSERT INTO task_links VALUES ('t_impl', 't_review')")
        connection.execute(
            "INSERT INTO task_events(task_id, run_id, kind, payload, created_at) "
            "VALUES ('t_impl', 10, 'review_requested', '{}', 100)"
        )
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_detects_competing_same_card_and_downstream_review_paths(self) -> None:
        report = inspect_graph(self.database_path, task_id="t_impl")

        self.assertEqual(["t_impl", "t_review"], report["affected_task_ids"])
        self.assertEqual("integration_locked", report["derived_state"])
        self.assertEqual(1, len(report["findings"]))
        self.assertEqual("competing_review_paths", report["findings"][0]["code"])
        self.assertEqual("t_impl", report["findings"][0]["task_id"])
        self.assertEqual(["t_review"], report["findings"][0]["downstream_review_task_ids"])

    def test_event_seed_rechecks_only_the_event_neighborhood(self) -> None:
        connection = sqlite3.connect(self.database_path)
        connection.execute(
            "INSERT INTO tasks VALUES "
            "('t_unrelated', 'Unrelated task', '', 'coder', 'todo', NULL, NULL, 'p_test', NULL)"
        )
        connection.execute(
            "INSERT INTO task_events(task_id, run_id, kind, payload, created_at) "
            "VALUES ('t_unrelated', NULL, 'created', '{}', 50)"
        )
        connection.execute(
            "INSERT INTO task_events(task_id, run_id, kind, payload, created_at) "
            "VALUES ('t_impl', 10, 'heartbeat', NULL, 75)"
        )
        event_id = connection.execute(
            "SELECT id FROM task_events WHERE task_id = 't_impl' AND kind = 'review_requested'"
        ).fetchone()[0]
        connection.commit()
        connection.close()

        report = inspect_graph(self.database_path, event_id=event_id)

        self.assertEqual("t_impl", report["trigger"]["task_id"])
        self.assertEqual(["t_impl", "t_review"], report["affected_task_ids"])
        self.assertNotIn("t_unrelated", report["affected_task_ids"])
        self.assertEqual(["review_requested"], [event["kind"] for event in report["timeline"]])


if __name__ == "__main__":
    unittest.main()
