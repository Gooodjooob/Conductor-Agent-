"""Guard shutdown, owner loss and deadline checks; local child fixtures only."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts import d4_workbuddy_guard as guard


class GuardShutdownTests(unittest.TestCase):
    def test_watcher_is_joined_before_owner_descriptor_closes(self):
        order = []
        fake_done = mock.Mock()
        fake_done.set.side_effect = lambda: order.append('stop')
        fake_watcher = mock.Mock()
        fake_watcher.join.side_effect = lambda: order.append('join')
        with mock.patch.object(sys, 'argv', ['guard', '--owner-fd', '99', '--timeout', '5', '--', '/usr/bin/true']), \
             mock.patch.object(guard.os, 'getpgrp', return_value=1), \
             mock.patch.object(guard.os, 'getpid', return_value=1), \
             mock.patch.object(guard.threading, 'Event', return_value=fake_done), \
             mock.patch.object(guard.threading, 'Thread', return_value=fake_watcher), \
             mock.patch.object(guard.subprocess, 'Popen') as popen, \
             mock.patch.object(guard.os, 'close', side_effect=lambda fd: order.append('close')):
            popen.return_value.wait.return_value = 7
            self.assertEqual(guard.main(), 7)
        self.assertEqual(order, ['stop', 'join', 'close'])

    def launch(self, command, timeout=5):
        readfd, writefd = os.pipe()
        try:
            child = subprocess.Popen([sys.executable, '-B', str(Path(guard.__file__).resolve()),
                                      '--owner-fd', str(readfd), '--timeout', str(timeout), '--'] + command,
                                     pass_fds=(readfd,), start_new_session=True,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        finally:
            os.close(readfd)
        return child, writefd

    def finish(self, child, ownerfd):
        try:
            return child.communicate(timeout=5)
        finally:
            if ownerfd is not None:
                os.close(ownerfd)
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.communicate(timeout=5)

    def test_real_successful_child_exits_cleanly_without_thread_error(self):
        child, ownerfd = self.launch(['/usr/bin/true'])
        out, err = self.finish(child, ownerfd)
        self.assertEqual(child.returncode, 0)
        self.assertEqual(err, b'')

    def test_failed_child_exit_code_is_preserved(self):
        child, ownerfd = self.launch(['/usr/bin/false'])
        out, err = self.finish(child, ownerfd)
        self.assertEqual(child.returncode, 1)
        self.assertEqual(err, b'')

    def test_owner_pipe_eof_still_kills_guard_and_child(self):
        child, ownerfd = self.launch(['/bin/sleep', '20'])
        os.close(ownerfd)
        self.finish(child, None)
        self.assertEqual(child.returncode, -signal.SIGKILL)

    def test_deadline_still_kills_guard_and_child(self):
        child, ownerfd = self.launch(['/bin/sleep', '20'], timeout=0.2)
        self.finish(child, ownerfd)
        self.assertEqual(child.returncode, -signal.SIGKILL)

    def test_launch_failure_also_joins_before_shutdown(self):
        child, ownerfd = self.launch(['/not-a-real-executable'])
        out, err = self.finish(child, ownerfd)
        self.assertEqual(child.returncode, 1)
        self.assertIn(b'FileNotFoundError', err)
        self.assertNotIn(b'Exception in thread', err)
        self.assertNotIn(b'Fatal Python error', err)
