import unittest
from pathlib import Path
from orchestrator.clawhive import ClawHiveAdapter, ClawHiveAdapterError, probe_clawhive


class ClawHiveTests(unittest.TestCase):
    def test_installed_probe_is_explicitly_not_task_api_proof(self):
        result = probe_clawhive()
        self.assertEqual(result['host'],'clawhive')
        self.assertFalse(result['taskSubmitSupported'])
        self.assertFalse(result['statusQuerySupported'])
        self.assertFalse(result['cancelSupported'])

    def test_missing_bridge_fails_closed(self):
        adapter=ClawHiveAdapter()
        self.assertFalse(adapter.probe()['bridgeConfigured'])
        with self.assertRaisesRegex(ClawHiveAdapterError,'EXTERNAL_TASK_API_UNVERIFIED'):
            adapter.start()

    def test_relative_bridge_rejected(self):
        with self.assertRaisesRegex(ValueError,'ABSOLUTE_CLAWHIVE_COMMAND_REQUIRED'):
            ClawHiveAdapter(['clawhive'])


if __name__=='__main__':unittest.main()
