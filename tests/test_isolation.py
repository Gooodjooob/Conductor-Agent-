"""Real Seatbelt black-box probes; no real provider credentials or business data."""
import ctypes
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from orchestrator.isolation import IsolationError, IsolationUnavailable, launch, prepare_launch


@unittest.skipUnless(platform.system() == "Darwin", "actual macOS Seatbelt required")
class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orch-isolation-qa-")
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "work"
        self.state = self.root / "control"
        self.workspace.mkdir()
        self.state.mkdir()
        self.secret = self.state / "token"
        self.secret.write_text("SYNTHETIC_CONTROL_SECRET")
        self.homes = []
        # Python's sys.prefix contains its dylibs/stdlib, but the selected
        # executable may be a symlink to that prefix.
        self.python = str(Path(sys.executable).resolve())
        self.runtime = [str(Path(sys.prefix).resolve())]

    def tearDown(self):
        for home in self.homes:
            shutil.rmtree(home, ignore_errors=True)
        self.temp.cleanup()

    def run_worker(self, source, extra=()):
        proc = launch([self.python, "-c", source] + list(extra), str(self.workspace),
                      self.runtime, str(self.state), stdout=subprocess.PIPE,
                      stderr=subprocess.PIPE, text=True)
        self.homes.append(proc.isolation_home)
        out, err = proc.communicate(timeout=12)
        self.assertEqual(proc.returncode, 0, (out, err))
        return json.loads(out)

    def test_worker_can_execute_and_write_only_own_workspace_home_tmp(self):
        result = self.run_worker('''import json,os,pathlib
for p in [pathlib.Path("ok.txt"),pathlib.Path(os.environ["HOME"])/"ok.txt",pathlib.Path(os.environ["TMPDIR"])/"ok.txt"]: p.write_text("ok")
print(json.dumps({"cwd":os.getcwd(),"home":os.environ["HOME"],"dsh":os.environ["DSH_HOME"],"env":list(os.environ)}))''')
        self.assertEqual(result["cwd"], str(self.workspace))
        self.assertTrue(result["dsh"].startswith(result["home"] + "/"))
        self.assertEqual((self.workspace / "ok.txt").read_text(), "ok")
        self.assertNotIn("PAPERCLIP_API_KEY", result["env"])

    def test_control_secret_outside_write_and_symlink_escape_denied(self):
        (self.workspace / "escape").symlink_to(self.state, target_is_directory=True)
        result = self.run_worker('''import json,pathlib,sys
r={}
for name,path,mode in [("read",sys.argv[1],"r"),("write",sys.argv[2],"w"),("symlink","escape/token","r")]:
 try:
  with open(path,mode) as f:
   f.read() if mode=="r" else f.write("bad")
  r[name]="ALLOWED"
 except OSError as e:r[name]=e.errno
print(json.dumps(r))''', [str(self.secret), str(self.root / "outside")])
        self.assertTrue(all(value in (1, 13) for value in result.values()), result)
        self.assertFalse((self.root / "outside").exists())
        self.assertEqual(self.secret.read_text(), "SYNTHETIC_CONTROL_SECRET")

    def test_local_http_and_descendant_network_denied(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            port = listener.getsockname()[1]
            result = self.run_worker('''import json,socket,subprocess,sys
r={}
try:
 s=socket.create_connection(("127.0.0.1",int(sys.argv[1])),timeout=2);s.sendall(b"GET / HTTP/1.0\\r\\n\\r\\n");r["direct"]="ALLOWED"
except OSError as e:r["direct"]=e.errno
c=subprocess.run([sys.executable,"-c","import socket,sys; socket.create_connection(('127.0.0.1',int(sys.argv[1])),timeout=2)",sys.argv[1]],capture_output=True,text=True)
r["childCode"]=c.returncode;r["childError"]=c.stderr
print(json.dumps(r))''', [str(port)])
            self.assertIn(result["direct"], (1, 13))
            self.assertNotEqual(result["childCode"], 0)
            self.assertIn("Operation not permitted", result["childError"])
            listener.settimeout(0.1)
            with self.assertRaises(socket.timeout):
                listener.accept()
        finally:
            listener.close()

    def test_signal_parent_and_ptrace_denied(self):
        result = self.run_worker('''import ctypes,json,os,signal,sys
r={}
try:os.kill(int(sys.argv[1]),0);r["killCheck"]="ALLOWED"
except OSError as e:r["killCheck"]=e.errno
libc=ctypes.CDLL(None,use_errno=True)
r["ptrace"]=libc.ptrace(10,int(sys.argv[1]),None,0);r["ptraceErrno"]=ctypes.get_errno()
print(json.dumps(r))''', [str(os.getpid())])
        self.assertIn(result["killCheck"], (1, 13))
        self.assertEqual(result["ptrace"], -1)
        self.assertIn(result["ptraceErrno"], (1, 13))

    def test_hardlink_escape_and_inherited_descriptor_denied(self):
        descriptor = os.open(str(self.secret), os.O_RDONLY)
        os.set_inheritable(descriptor, True)
        try:
            result = self.run_worker('''import json,os,sys
r={}
try:os.link(sys.argv[1],"stolen-token");r["link"]="ALLOWED"
except OSError as e:r["link"]=e.errno
try:r["descriptor"]=os.read(int(sys.argv[2]),4096).decode()
except OSError as e:r["descriptor"]=e.errno
print(json.dumps(r))''', [str(self.secret), str(descriptor)])
            self.assertIn(result["link"], (1, 13))
            self.assertEqual(result["descriptor"], 9)
            self.assertFalse((self.workspace / "stolen-token").exists())
        finally:
            os.close(descriptor)

    def test_refuse_overlapping_control_environment_and_unsafe_popen(self):
        with self.assertRaises(IsolationError):
            prepare_launch([self.python], str(self.root), self.runtime, str(self.state))
        with self.assertRaises(IsolationError):
            prepare_launch([self.python], str(self.workspace), [str(self.root)] + self.runtime, str(self.state))
        with self.assertRaises(IsolationError):
            prepare_launch([self.python], str(self.workspace), self.runtime, str(self.state), {"PAPERCLIP_API_KEY": "synthetic"})
        with self.assertRaises(IsolationError):
            launch([self.python], str(self.workspace), self.runtime, str(self.state), close_fds=False)

    def test_unsupported_host_fails_closed(self):
        with mock.patch("orchestrator.isolation.platform.system", return_value="Linux"):
            with self.assertRaises(IsolationUnavailable):
                prepare_launch([self.python], str(self.workspace), self.runtime, str(self.state))

    def test_preexisting_hardlink_is_rejected_before_start(self):
        os.link(str(self.secret), str(self.workspace / "preexisting-token"))
        with self.assertRaises(IsolationError):
            prepare_launch([self.python], str(self.workspace), self.runtime, str(self.state))

    def test_runtime_can_query_uname_system_description(self):
        result = self.run_worker('''import json,os
u=os.uname()
print(json.dumps({"system":u.sysname,"hasRelease":bool(u.release),"hasMachine":bool(u.machine)}))''')
        self.assertEqual(result, {"system": "Darwin", "hasRelease": True, "hasMachine": True})

    def test_parent_process_arguments_denied(self):
        source = '''import ctypes,json,sys
libc=ctypes.CDLL(None,use_errno=True)
mib=(ctypes.c_int*3)(1,49,int(sys.argv[1]))
size=ctypes.c_size_t(1024*1024)
buf=ctypes.create_string_buffer(size.value)
rc=libc.sysctl(mib,3,buf,ctypes.byref(size),None,0)
print(json.dumps({"rc":rc,"errno":ctypes.get_errno(),"size":size.value}))'''
        sibling = subprocess.Popen([self.python, "-c", "import time;time.sleep(20)", "SYNTHETIC_ARG"])
        try:
            for pid in (os.getpid(), sibling.pid):
                with self.subTest(pid=pid):
                    result = self.run_worker(source, [str(pid)])
                    self.assertEqual(result["rc"], -1)
                    self.assertIn(result["errno"], (1, 13))
        finally:
            sibling.terminate()
            sibling.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
