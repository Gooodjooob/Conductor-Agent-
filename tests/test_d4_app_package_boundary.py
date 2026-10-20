from test_d4_app_package import PackageFixtureTestCase, pkg, OUTPUT_NAME

class EntryBoundaryTests(PackageFixtureTestCase):
    def test_arbitrary_entry_cannot_be_packaged(self):
        other = self.root / "other.py"
        other.write_bytes(b'print("other")\n')
        with self.assertRaises(pkg.PackageError) as error:
            self.build(entry_script=str(other))
        self.assertEqual(error.exception.code, "entry_invalid")
        self.assertFalse((self.artifact_root / OUTPUT_NAME).exists())
