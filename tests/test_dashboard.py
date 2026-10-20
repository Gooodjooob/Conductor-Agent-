import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from orchestrator.dashboard import make_dashboard, render_html


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.snapshot={'generatedAt':1,'quota':'disabled','live':True,'cards':[{
            'taskId':'task-1','title':'实现适配','revision':1,'memberId':'dev-1','host':'claude','role':'developer',
            'state':'unknown','executionState':'unknown','validationState':'pending','syncState':'not_synced',
            'verdicts':{},'dependsOn':[],'blockers':[],'error':'EXECUTION_UNCONFIRMED_NO_REPLAY',
            'nextAction':'核对执行事实，禁止自动重派','cancelRequested':False,'stopConfirmed':False,
            'issueId':'issue-1','attemptId':'a1','resultDigest':None}]}
        self.token='t'*40
        self.server=make_dashboard(lambda:self.snapshot,self.token,0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()

    def tearDown(self): self.server.shutdown(); self.server.server_close(); self.thread.join()

    def req(self,path,token=None):
        r=urllib.request.Request('http://127.0.0.1:'+str(self.server.server_port)+path)
        if token:r.add_header('Authorization','Bearer '+token)
        try:
            with urllib.request.urlopen(r,timeout=2) as x:return x.status,x.read()
        except urllib.error.HTTPError as e:return e.code,e.read()

    def test_html_is_static_escaped_and_live_requires_token(self):
        html=render_html(self.snapshot)
        self.assertIn('任务有去向',html); self.assertIn('EXECUTION_UNCONFIRMED_NO_REPLAY',html)
        self.assertNotIn('<script>alert',html)
        self.assertEqual(self.req('/')[0],200); self.assertEqual(self.req('/api/snapshot')[0],401)
        self.assertEqual(self.req('/api/snapshot',self.token)[0],200)
        self.assertEqual(self.req('/api/snapshot','wrong')[0],401)

    def test_dashboard_is_read_only_and_host_bound(self):
        self.assertEqual(self.req('/api/snapshot',self.token)[0],200)
        r=urllib.request.Request('http://127.0.0.1:'+str(self.server.server_port)+'/api/snapshot',method='POST')
        try: urllib.request.urlopen(r,timeout=2)
        except urllib.error.HTTPError as e:self.assertEqual(e.code,405)


if __name__=='__main__':unittest.main()
