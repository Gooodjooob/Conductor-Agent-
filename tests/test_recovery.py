import json
import os
import tempfile
import unittest
from pathlib import Path
from orchestrator.recovery import RecoveryError, activate_restored_state, backup_state, restore_state, verify_backup


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root/'state'; self.state.mkdir(mode=0o700)
        (self.state/'nested').mkdir(mode=0o700)
        (self.state/'nested'/'receipt.json').write_text('{"state":"awaiting_validation"}')
        os.chmod(self.state/'nested'/'receipt.json', 0o600)

    def tearDown(self): self.tmp.cleanup()

    def test_backup_manifest_and_restore_hold(self):
        backup = self.root/'backup'; restored = self.root/'restored'
        report = backup_state(str(self.state), str(backup))
        self.assertEqual(report['manifestSha256'], verify_backup(str(backup))['manifestSha256'])
        hold = restore_state(str(backup), str(restored))
        self.assertTrue((restored/'RESTORE_PENDING.json').exists())
        self.assertEqual((restored/'nested'/'receipt.json').read_text(), '{"state":"awaiting_validation"}')
        with self.assertRaises(RecoveryError): activate_restored_state(str(restored), '0'*64)
        self.assertEqual(activate_restored_state(str(restored), hold['manifestSha256'])['state'], 'active')

    def test_backup_refuses_tamper_symlink_and_destination_reuse(self):
        backup = self.root/'backup'; backup_state(str(self.state), str(backup))
        (backup/'nested'/'receipt.json').write_text('tampered')
        with self.assertRaises(RecoveryError): verify_backup(str(backup))
        with self.assertRaises(RecoveryError): backup_state(str(self.state), str(backup))
        link = self.state/'nested'/'link'; link.symlink_to(self.state/'nested'/'receipt.json')
        with self.assertRaises(RecoveryError): backup_state(str(self.state), str(self.root/'b2'))

    def test_restore_never_overwrites_existing_directory(self):
        backup = self.root/'backup'; backup_state(str(self.state), str(backup))
        existing = self.root/'existing'; existing.mkdir()
        with self.assertRaises(RecoveryError): restore_state(str(backup), str(existing))

    def test_nested_lease_does_not_deadlock(self):
        from orchestrator.recovery import state_lease
        with state_lease(str(self.state), exclusive=True):
            with state_lease(str(self.state)):
                self.assertTrue(True)


if __name__ == '__main__': unittest.main()
