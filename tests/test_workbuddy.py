import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from orchestrator.workbuddy import WorkBuddyAdapter


class WorkBuddyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name); self.work=self.root/'work'; self.work.mkdir(); self.home=self.root/'home'; self.home.mkdir()
        self.fixture=self.root/'codebuddy-fixture.py'
        self.fixture.write_text('''import json,sys\nsid=sys.argv[sys.argv.index("--session-id")+1]\nprint(json.dumps({"type":"result","subtype":"success","is_error":False,"session_id":sid,"uuid":"wb-fixture-1","result":"fixture result","permission_denials":[]}))\n'''); os.chmod(self.fixture,0o700)
    def tearDown(self): self.tmp.cleanup()
    def test_host_probe_and_json_success(self):
        a=WorkBuddyAdapter(self.root/'store',[sys.executable,str(self.fixture)],str(self.work),{'HOME':str(self.home),'PATH':'/usr/bin:/bin'},model='deepseek-v4-pro',sandbox='read-only')
        self.assertEqual(a.probe()['host'],'workbuddy'); a.start();
        a.submit('t',1,'a','fixture','d')
        self.assertIn('--output-format', a._command_args('fixture'))
        self.assertIn('stream-json', a._command_args('fixture'))
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            r=a.get_status('a')
            if r['state'] in ('awaiting_validation','failed','unknown'):break
            time.sleep(.02)
        self.assertEqual(r['state'],'awaiting_validation'); a.close()


if __name__=='__main__':unittest.main()
