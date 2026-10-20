import hashlib
import json
import sqlite3
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from scripts.d4_board import board_data, make_handler
from storage.sqlite_store import SqliteStore
from test_d4ctl import D4CtlTestCase, ResponsiveTransport


class BoardTests(D4CtlTestCase):
    def setUp(self):
        super().setUp()
        self.task_id = self.create_task()[1]["data"]["taskId"]

    def test_read_only_rejects_writes_and_does_not_create_missing_db(self):
        digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        store = SqliteStore(self.db, read_only=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                store.register_agent("forbidden")
            with self.assertRaises(sqlite3.OperationalError):
                store.conn.executescript("CREATE TABLE forbidden (id TEXT)")
        finally:
            store.close()
        self.assertEqual(digest, hashlib.sha256(self.db.read_bytes()).hexdigest())
        missing = self.root / "missing.sqlite"
        with self.assertRaises(sqlite3.OperationalError):
            SqliteStore(missing, read_only=True)
        self.assertFalse(missing.exists())

    def test_projects_detail_refresh_and_synthetic_label(self):
        first = board_data(self.db, "/api/tasks/" + self.task_id)
        self.assertEqual(first["evidenceType"], "unconfirmed")
        self.assertEqual(first["attempts"], [])
        self.submit_first_attempt(self.task_id, ResponsiveTransport())
        second = board_data(self.db, "/api/tasks/" + self.task_id)
        self.assertEqual(len(second["attempts"]), 1)
        self.assertEqual(second["evidenceType"], "synthetic")
        writer = SqliteStore(self.db)
        writer.update_task(self.task_id, status="approved")
        writer.close()
        self.assertEqual(board_data(self.db, "/api/tasks/" + self.task_id)["taskDisplayState"], "approved")
        projects = board_data(self.db, "/api/projects")["projects"]
        self.assertEqual(projects[0]["tasks"][0]["taskId"], self.task_id)

    def test_http_anonymous_read_and_write_rejection(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.db, "test-token"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_port
        try:
            self.assertTrue(json.load(urlopen(base + "/api/projects"))["projects"])
            for method in ("POST", "PATCH", "PUT", "DELETE"):
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(base + "/api/tasks", data=b"{}", method=method))
                self.assertEqual(error.exception.code, 405)
            self.assertEqual(urlopen(base + "/").status, 200)
            with self.assertRaises(HTTPError) as error:
                urlopen(base + "/../storage/sqlite_store.py")
            self.assertEqual(error.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
