import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from orchestrator.codex import CodexAdapter
from orchestrator.dsh import AdapterError


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name); self.work=self.root/'work'; self.work.mkdir(); self.home=self.root/'home'; self.home.mkdir()
        self.fixture=self.root/'codex-fixture.py'
        self.fixture.write_text('''import json,sys\nfor x in ({"type":"thread.started","thread_id":"t"},{"type":"item.completed","item":{"type":"agent_message","text":"fixture result"}},{"type":"turn.completed"}): print(json.dumps(x),flush=True)\n'''); os.chmod(self.fixture,0o700)
    def tearDown(self): self.tmp.cleanup()
    def make(self): return CodexAdapter(self.root/'store',[sys.executable,str(self.fixture)],str(self.work),{'HOME':str(self.home),'PATH':'/usr/bin:/bin'},sandbox='read-only',timeout=2,execution_timeout=5)
    def test_jsonl_success_only_reaches_validation(self):
        a=self.make(); a.start(); a.submit('t',1,'a','fixture','d'); deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            r=a.get_status('a')
            if r['state'] in ('awaiting_validation','failed','unknown'):break
            time.sleep(.02)
        self.assertEqual(r['state'],'awaiting_validation'); self.assertTrue(r['accepted']); a.close()
    def test_deduplicate_and_config_drift(self):
        a=self.make(); a.start(); first=a.submit('t',1,'a','fixture','d'); second=a.submit('t',1,'a','fixture','d'); self.assertFalse(first['deduplicated']); self.assertTrue(second['deduplicated'])
        deadline=time.monotonic()+2
        while time.monotonic()<deadline and a.get_status('a')['state'] not in ('awaiting_validation','failed','unknown'): time.sleep(.02)
        self.assertEqual(a.get_status('a')['state'],'awaiting_validation'); a.close()
        b=self.make(); self.assertEqual(b.get_status('a')['state'],'awaiting_validation');
    def test_dangerous_sandbox_rejected(self):
        with self.assertRaisesRegex(ValueError,'SANDBOX'): self.make_bad()
    def make_bad(self): return CodexAdapter(self.root/'bad',[sys.executable],str(self.work),{'HOME':str(self.home)},sandbox='danger-full-access')


if __name__=='__main__':unittest.main()
