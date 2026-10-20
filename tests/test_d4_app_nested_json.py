import unittest
from test_d4_health_runtime import RuntimeFixtureTestCase, rt
from test_d4_app_entry import entry

class CatalogNestedTests(RuntimeFixtureTestCase):
    def test_deep_catalog_is_invalid_without_exception(self):
        config = self.prepare_ready()
        frozen = rt.freeze_identity(config, expected_board_sha256=self.board_sha)
        self.catalog_path.write_text("[" * 1200 + "0" + "]" * 1200)
        result = rt.collect_health(frozen)
        self.assertFalse(result["ready"])
        self.assertEqual(result["reason"], "catalog_invalid")

class ConfigNestedTests(unittest.TestCase):
    def test_deep_json_is_generic_parse_failure(self):
        with self.assertRaises(entry._HealthFailure) as error:
            entry._strict_json_object("[" * 1200 + "0" + "]" * 1200)
        self.assertEqual(error.exception.code, "malformed_response")
