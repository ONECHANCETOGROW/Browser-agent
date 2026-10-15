import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from octg_agent.files.authorization import AuthorizationError, AuthorizedFolders, check_filename


class FilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="octg_files_")).resolve()
        self.root = self.tmp / "allowed"
        self.other = self.tmp / "secret"
        self.root.mkdir()
        self.other.mkdir()
        (self.root / "pic.png").write_bytes(b"\x89PNG-data")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "doc.txt").write_text("hello")
        (self.other / "passwords.txt").write_text("hunter2")
        self.f = AuthorizedFolders(self.tmp / "store.json")
        self.f.allow(str(self.root))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def code(self, fn, *a, **k):
        with self.assertRaises(AuthorizationError) as cm:
            fn(*a, **k)
        return cm.exception.code

    def test_nothing_is_readable_until_authorized(self):
        empty = AuthorizedFolders(self.tmp / "none.json")
        self.assertEqual(self.code(empty.resolve_file, str(self.root / "pic.png")), "FILE_NOT_AUTHORIZED")

    def test_inside_ok_outside_rejected(self):
        self.assertEqual(self.f.resolve_file(str(self.root / "pic.png")).name, "pic.png")
        self.assertEqual(self.f.resolve_file(str(self.root / "sub" / "doc.txt")).name, "doc.txt")
        self.assertEqual(self.code(self.f.resolve_file, str(self.other / "passwords.txt")), "FILE_NOT_AUTHORIZED")

    def test_traversal_and_relative_paths_rejected(self):
        self.assertEqual(self.code(self.f.resolve_file, str(self.root / ".." / "secret" / "passwords.txt")), "FILE_NOT_AUTHORIZED")
        self.assertEqual(self.code(self.f.resolve_file, "pic.png"), "INVALID_PARAMS")
        self.assertEqual(self.code(self.f.resolve_file, ""), "INVALID_PARAMS")

    def test_sibling_with_common_prefix_is_not_inside(self):
        sibling = self.tmp / "allowed-evil"
        sibling.mkdir()
        (sibling / "x.txt").write_text("x")
        self.assertEqual(self.code(self.f.resolve_file, str(sibling / "x.txt")), "FILE_NOT_AUTHORIZED")

    @unittest.skipUnless(os.name == "nt", "NTFS alternate data streams")
    def test_alternate_data_stream_rejected(self):
        self.assertEqual(self.code(self.f.resolve, str(self.root / "pic.png") + ":secret"), "FILE_NOT_AUTHORIZED")

    @unittest.skipUnless(os.name == "nt", "junction test uses mklink /J")
    def test_junction_escape_rejected(self):
        link = self.root / "escape"
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(self.other)], capture_output=True)
        if r.returncode != 0:
            self.skipTest("cannot create a junction here")
        self.assertEqual(self.code(self.f.resolve_file, str(link / "passwords.txt")), "FILE_NOT_AUTHORIZED")

    def test_too_broad_folders_refused(self):
        drive_root = Path(self.tmp.anchor)
        self.assertEqual(self.code(self.f.allow, str(drive_root)), "FILE_NOT_AUTHORIZED")
        self.assertEqual(self.code(self.f.allow, str(Path.home())), "FILE_NOT_AUTHORIZED")
        if os.environ.get("SystemRoot"):
            self.assertEqual(self.code(self.f.allow, os.environ["SystemRoot"]), "FILE_NOT_AUTHORIZED")
        self.assertEqual(self.code(self.f.allow, str(self.tmp / "missing")), "FILE_NOT_FOUND")

    def test_revoke(self):
        self.assertTrue(self.f.revoke(str(self.root)))
        self.assertEqual(self.code(self.f.resolve_file, str(self.root / "pic.png")), "FILE_NOT_AUTHORIZED")
        self.assertFalse(self.f.revoke(str(self.root)))

    def test_allow_is_idempotent(self):
        self.f.allow(str(self.root))
        self.assertEqual(len(self.f.roots()), 1)

    def test_find_stays_inside_roots(self):
        names = [Path(x["path"]).name for x in self.f.find("*.txt")]
        self.assertEqual(names, ["doc.txt"])  # never reports secret/passwords.txt
        self.assertEqual(self.code(self.f.find, "*", str(self.other)), "FILE_NOT_AUTHORIZED")
        self.assertEqual(len(self.f.find("*", max_results=1)), 1)

    def test_read_for_upload_and_size_limit(self):
        info = self.f.read_for_upload(str(self.root / "pic.png"), 1000)
        self.assertEqual((info["name"], info["size"]), ("pic.png", 9))
        self.assertEqual(len(info["sha256"]), 64)
        self.assertEqual(self.code(self.f.read_for_upload, str(self.root / "pic.png"), 3), "FILE_TOO_LARGE")

    def test_save_into_rules(self):
        src = self.other / "dl.bin"
        src.write_bytes(b"abc")
        r = self.f.save_into(str(self.root), "saved.bin", src)
        self.assertEqual(Path(r["saved_path"]).read_bytes(), b"abc")
        self.assertEqual(self.code(self.f.save_into, str(self.root), "saved.bin", src), "FILE_EXISTS")
        self.f.save_into(str(self.root), "saved.bin", src, overwrite=True)
        self.assertEqual(self.code(self.f.save_into, str(self.other), "x.bin", src), "FILE_NOT_AUTHORIZED")
        for bad in ("..\\evil.txt", "../evil.txt", "a/b.txt", "CON", "nul.txt", "bad:name", "trail.", " lead", ""):
            self.assertEqual(self.code(check_filename, bad), "INVALID_PARAMS", bad)
        self.assertFalse((self.tmp / "evil.txt").exists())


if __name__ == "__main__":
    unittest.main()
