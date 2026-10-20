import json
import os
import socket
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from orchestrator.claude import ClaudeAdapter
from orchestrator.egress import EgressError, FixedUpstreamProxy, redact_provider_secret


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.work = self.root/'work'; self.work.mkdir(); self.home = self.root/'home'; self.home.mkdir()
        self.script = self.root/'claude-fixture.py'
        self.script.write_text('''import json,sys,uuid\nargs=sys.argv\nsid=args[args.index("--session-id")+1]\nprint(json.dumps({"type":"result","subtype":"success","is_error":False,"session_id":sid,"uuid":str(uuid.uuid4()),"result":"fixture ok","permission_denials":[]}))\n''')
        os.chmod(self.script, 0o700)

    def tearDown(self): self.tmp.cleanup()

    def test_claude_json_result_reaches_validation_only(self):
        a = ClaudeAdapter(self.root/'store',[sys.executable,str(self.script)],str(self.work),{'HOME':str(self.home),'PATH':'/usr/bin:/bin'},timeout=2,execution_timeout=5)
        try:
            a.start(); result=a.submit('task-1',1,'attempt-1','do fixture','a'*64)
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                result=a.get_status('attempt-1')
                if result['state'] in ('awaiting_validation','failed','unknown'): break
                time.sleep(.02)
            self.assertEqual(result['state'],'awaiting_validation'); self.assertTrue(result['accepted'])
        finally: a.close()

    def test_claude_rejects_config_drift_and_unknown_result(self):
        a = ClaudeAdapter(self.root/'store',[sys.executable,str(self.script)],str(self.work),{'HOME':str(self.home),'PATH':'/usr/bin:/bin'})
        try:
            a.start(); a.env['PATH']='/bin';
            with self.assertRaisesRegex(Exception,'LAUNCH_CONFIG_CHANGED'): a.submit('t',1,'a','p','d')
        finally: a.close()

    def test_fixed_proxy_is_not_general_proxy(self):
        with self.assertRaisesRegex(EgressError,'FIXED_PROVIDER_REQUIRED'): FixedUpstreamProxy('arbitrary','credential','model')
        p=FixedUpstreamProxy('deepseek','secret-token','deepseek-chat').start()
        try:
            env=p.worker_env(); self.assertIn('DEEPSEEK_API_KEY',env); self.assertNotEqual(env['DEEPSEEK_API_KEY'],'secret-token')
            self.assertEqual(env['DEEPSEEK_BASE_URL'],'http://127.0.0.1:'+str(p.port))
            req=urllib.request.Request(env['DEEPSEEK_BASE_URL']+'/other',data=b'{}',headers={'Authorization':'Bearer '+env['DEEPSEEK_API_KEY'],'Content-Type':'application/json'})
            with self.assertRaises(urllib.error.HTTPError) as ctx: urllib.request.urlopen(req,timeout=2)
            self.assertEqual(ctx.exception.code,403)
        finally: p.close()

    def test_fixed_proxy_rejects_control_plane_and_model_mismatch(self):
        p=FixedUpstreamProxy('anthropic','secret-token','claude-sonnet').start()
        try:
            env=p.worker_env()
            body=json.dumps({'model':'wrong','messages':[]}).encode()
            req=urllib.request.Request(env['ANTHROPIC_BASE_URL']+'/v1/messages',data=body,headers={'x-api-key':env['ANTHROPIC_API_KEY'],'Content-Type':'application/json'})
            with self.assertRaises(urllib.error.HTTPError) as ctx: urllib.request.urlopen(req,timeout=2)
            self.assertEqual(ctx.exception.code,400)
            req=urllib.request.Request(env['ANTHROPIC_BASE_URL']+'/v1/messages',data=b'{}',headers={'x-api-key':env['ANTHROPIC_API_KEY'],'Host':'127.0.0.1:9','Content-Type':'application/json'})
            with self.assertRaises(urllib.error.HTTPError) as ctx: urllib.request.urlopen(req,timeout=2)
            self.assertEqual(ctx.exception.code,403)
        finally: p.close()

    def test_proxy_redacts_secret_in_forwarded_response(self):
        self.assertEqual(redact_provider_secret(b'{"x":"secret-token"}',b'secret-token'),b'{"x":"[redacted]"}')
        self.assertEqual(redact_provider_secret(b'prefixsecret-token-suffix',b'secret-token'),b'prefix[redacted]-suffix')


if __name__=='__main__': unittest.main()
