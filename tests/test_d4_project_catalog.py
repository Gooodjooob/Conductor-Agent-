import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from projections.project_catalog import ProjectCatalog
from scripts.d4_board import board_data, make_handler
from test_d4ctl import D4CtlTestCase


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.material = self.root / 'report.md'
        self.material.write_text('Historical result', encoding='utf-8')
        self.data = {'schemaVersion': 'project-catalog.v1', 'projects': [{
            'projectId': 'archive-project', 'name': 'D4',
            'milestones': [{'id': 'foundation', 'title': 'Foundation'}],
            'tasks': [{'taskId': 'archive-task', 'status': 'verified',
                       'milestoneId': 'foundation', 'documents': [{
                           'name': 'Report', 'kind': 'report',
                           'snapshot': 'report.md', 'sourcePath': '/original/report.md',
                           'sha256': hashlib.sha256(self.material.read_bytes()).hexdigest()}]}],
            'issues': [{'taskId': 'archive-task', 'title': 'Next step'}]}]}
        self.catalog = self.root / 'catalog.json'
        self.save()

    def save(self):
        self.catalog.write_text(json.dumps(self.data), encoding='utf-8')

    def test_catalog_only_project_task_and_material(self):
        projects = board_data(None, '/api/projects', self.catalog)['projects']
        self.assertEqual(projects[0]['recordType'], 'archive')
        detail = board_data(None, '/api/tasks/archive-task', self.catalog)
        self.assertEqual(detail['projectId'], 'archive-project')
        self.assertEqual(len(detail['issues']), 1)
        doc = board_data(None, '/api/materials/archive-task/0', self.catalog)
        self.assertEqual(doc['content'], 'Historical result')
        self.assertEqual(board_data(None, '/api/reviews', self.catalog), {'items': []})

    def test_changed_material_rejected(self):
        self.material.write_text('Changed after registration', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'catalog_snapshot_changed'):
            board_data(None, '/api/materials/archive-task/0', self.catalog)

    def test_snapshot_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / 'secret.md'
            target.write_text('Outside', encoding='utf-8')
            self.material.unlink()
            self.material.symlink_to(target)
            with self.assertRaisesRegex(ValueError, 'catalog_snapshot_escape'):
                ProjectCatalog(self.catalog)

    def test_unknown_issue_and_duplicate_id_rejected(self):
        self.data['projects'][0]['issues'][0]['taskId'] = 'unknown'
        self.save()
        with self.assertRaisesRegex(ValueError, 'catalog_issue_task_unknown'):
            ProjectCatalog(self.catalog)
        self.data['projects'][0]['issues'] = []
        self.data['projects'].append(self.data['projects'][0])
        self.save()
        with self.assertRaisesRegex(ValueError, 'catalog_identifier_invalid'):
            ProjectCatalog(self.catalog)

    def test_http_read_only_and_material_route_limits(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(None, catalog_path=self.catalog))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:%s' % server.server_port
        try:
            self.assertEqual(json.load(urlopen(base + '/api/materials/archive-task/0'))['content'], 'Historical result')
            self.assertEqual(urlopen(base + '/project-view.js').status, 200)
            for route in ['/api/materials/archive-task/99', '/api/materials/%2Fetc%2Fpasswd/0', '/api/materials/archive-task/../report.md']:
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + route)
                self.assertEqual(error.exception.code, 404)
            for method in ['POST', 'PUT', 'PATCH', 'DELETE']:
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(base + '/api/projects', data=b'{}', method=method))
                self.assertEqual(error.exception.code, 405)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class CatalogLiveCompatibilityTests(D4CtlTestCase):
    def test_live_database_still_readable_alongside_catalog(self):
        task_id = self.create_task()[1]['data']['taskId']
        catalog = self.root / 'catalog.json'
        catalog.write_text(json.dumps({'schemaVersion': 'project-catalog.v1', 'projects': [{
            'projectId': 'archive-project', 'name': 'Archive',
            'milestones': [], 'tasks': [], 'issues': []}]}), encoding='utf-8')
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        projects = board_data(self.db, '/api/projects', catalog)['projects']
        self.assertEqual(projects[0]['projectId'], 'archive-project')
        self.assertTrue(any(t['taskId'] == task_id for p in projects for t in p['tasks']))
        self.assertEqual(board_data(self.db, '/api/tasks/' + task_id, catalog)['task']['task_id'], task_id)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
