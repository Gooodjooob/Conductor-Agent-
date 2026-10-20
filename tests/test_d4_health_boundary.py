import os
import threading
from unittest import mock
from test_d4_health_runtime import RuntimeFixtureTestCase, rt, catalog_document, material_document

class ReadBoundaryTests(RuntimeFixtureTestCase):
    def test_fifo_open_is_bounded_and_rejected(self):
        fifo = self.root / "pipe"
        os.mkfifo(str(fifo))
        answers = []
        thread = threading.Thread(target=lambda: answers.append(rt._material_bytes(str(self.root), "pipe")), daemon=True)
        thread.start()
        thread.join(.2)
        blocked = thread.is_alive()
        if blocked:
            fd = os.open(str(fifo), os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(blocked, "material FIFO open must never block")
        self.assertEqual(answers, [None])

    def test_material_read_oserror_is_generic_catalog_invalid(self):
        self.write_database(projects=("live",))
        material = self.write_material("doc.md", b"material")
        self.write_catalog(document=catalog_document(documents=[material_document("doc.md", b"material")]))
        frozen = rt.freeze_identity(self.config(), expected_board_sha256=self.board_sha)
        inode = material.stat().st_ino
        original_read = rt.os.read
        def fail_material(fd, count):
            if os.fstat(fd).st_ino == inode:
                raise OSError("synthetic material I/O failure")
            return original_read(fd, count)
        with mock.patch.object(rt.os, "read", side_effect=fail_material):
            payload = rt.collect_health(frozen)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["reason"], "catalog_invalid")
