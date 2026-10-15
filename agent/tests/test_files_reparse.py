"""Windows junction / reparse-point hardening of the authorized-folder model. Deterministic: every case builds its own folders and links (mklink /J needs no privileges)."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from octg_agent.files.authorization import AuthorizationError, AuthorizedFolders, is_reparse_point, safe_walk


def junction(link: Path, target: Path) -> bool:
    """Create a directory junction; False if this platform/user cannot."""
    if os.name != "nt":
        try:
            os.symlink(target, link, target_is_directory=True)
            return True
        except OSError:
            return False
    return subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True).returncode == 0


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name).resolve()
        self.auth = self.root / "auth"
        self.evil = self.root / "auth_evil"      # a SIBLING whose name starts with the authorized folder's name
        self.outside = self.root / "outside"
        for d in (self.auth, self.evil, self.outside, self.auth / "docs", self.auth / "a"):
            d.mkdir()
        (self.auth / "ok.txt").write_text("ok", encoding="utf-8")
        (self.auth / "docs" / "inner.txt").write_text("inner", encoding="utf-8")
        (self.outside / "secret.txt").write_text("secret", encoding="utf-8")
        (self.evil / "x.txt").write_text("x", encoding="utf-8")
        self.f = AuthorizedFolders(self.root / "folders.json")
        self.f.allow(str(self.auth))

    def tearDown(self):
        self.tmp.cleanup()

    def link(self, name_path: Path, target: Path):
        if not junction(name_path, target):
            self.skipTest("cannot create a junction or symlink here")


class DetectionTests(Base):
    def test_junctions_are_reparse_points_and_ordinary_folders_are_not(self):
        self.link(self.auth / "j", self.outside)
        self.assertTrue(is_reparse_point(self.auth / "j"))
        self.assertFalse(is_reparse_point(self.auth / "docs"))
        self.assertFalse(is_reparse_point(self.auth / "ok.txt"))
        self.assertFalse(is_reparse_point(self.auth / "does_not_exist"))

    def test_symlinks_count_too_when_they_can_be_made(self):
        try:
            os.symlink(self.outside / "secret.txt", self.auth / "s.txt")
        except OSError:
            self.skipTest("file symlinks need privileges here")
        self.assertTrue(is_reparse_point(self.auth / "s.txt"))


class ResolveTests(Base):
    def refused(self, raw):
        with self.assertRaises(AuthorizationError) as cm:
            self.f.resolve(raw)
        self.assertEqual(cm.exception.code, "FILE_NOT_AUTHORIZED", raw)

    def test_normal_paths_inside_the_folder_still_work_including_case_changes(self):
        self.assertEqual(self.f.resolve_file(str(self.auth / "ok.txt")), (self.auth / "ok.txt").resolve())
        self.assertEqual(self.f.resolve_file(str(self.auth / "docs" / "inner.txt")), (self.auth / "docs" / "inner.txt").resolve())
        self.assertEqual(self.f.resolve_dir(str(self.auth / "docs")), (self.auth / "docs").resolve())
        if os.name == "nt":
            self.assertTrue(self.f.resolve_file(str(self.auth / "OK.TXT")))

    def test_a_junction_to_outside_is_refused(self):
        self.link(self.auth / "j", self.outside)
        self.refused(str(self.auth / "j" / "secret.txt"))
        self.refused(str(self.auth / "j"))

    def test_a_nested_junction_escape_is_refused(self):
        self.link(self.auth / "a" / "b", self.outside)
        self.refused(str(self.auth / "a" / "b" / "secret.txt"))

    def test_a_junction_chain_that_leads_outside_is_refused(self):
        mid = self.auth / "mid"
        self.link(mid, self.outside)
        self.link(self.auth / "docs" / "again", mid)
        self.refused(str(self.auth / "docs" / "again" / "secret.txt"))

    def test_a_junction_whose_target_stays_inside_resolves_to_the_real_inside_path(self):
        self.link(self.auth / "alias", self.auth / "docs")
        self.assertEqual(self.f.resolve_file(str(self.auth / "alias" / "inner.txt")), (self.auth / "docs" / "inner.txt").resolve())

    def test_sibling_prefix_and_traversal_tricks_are_refused(self):
        self.refused(str(self.evil / "x.txt"))
        self.refused(str(self.auth) + "_evil\\x.txt" if os.name == "nt" else str(self.auth) + "_evil/x.txt")
        self.refused(str(self.auth / ".." / "auth_evil" / "x.txt"))
        self.refused(str(self.auth / ".." / "outside" / "secret.txt"))
        self.refused(str(self.auth / "docs" / ".." / ".." / "outside" / "secret.txt"))

    def test_relative_nul_and_stream_paths_are_still_refused(self):
        with self.assertRaises(AuthorizationError):
            self.f.resolve("ok.txt")
        with self.assertRaises(AuthorizationError):
            self.f.resolve(str(self.auth / "ok.txt") + "\x00.png")
        if os.name == "nt":
            with self.assertRaises(AuthorizationError):
                self.f.resolve(str(self.auth / "ok.txt") + ":hidden")

    def test_allowing_a_junction_records_its_real_target_and_drive_roots_stay_refused(self):
        self.link(self.root / "alias_to_outside", self.outside)
        g = AuthorizedFolders(self.root / "other.json")
        e = g.allow(str(self.root / "alias_to_outside"))
        self.assertEqual(Path(e["path"]).resolve(), self.outside.resolve())
        with self.assertRaises(AuthorizationError):
            g.allow(Path(self.root.anchor))


class WalkTests(Base):
    def test_find_never_lists_through_a_junction_to_outside(self):
        self.link(self.auth / "j", self.outside)
        names = [Path(r["path"]).name for r in self.f.find("*")]
        self.assertIn("ok.txt", names)
        self.assertIn("inner.txt", names)
        self.assertNotIn("secret.txt", names, "a name that lives outside the authorized folder must not leak through a link")

    def test_find_does_not_follow_an_inside_junction_either_so_there_are_no_duplicates_or_loops(self):
        self.link(self.auth / "alias", self.auth / "docs")
        self.link(self.auth / "docs" / "up", self.auth)          # a loop back to the root
        names = sorted(Path(r["path"]).name for r in self.f.find("*.txt"))
        self.assertEqual(names, ["inner.txt", "ok.txt"])

    def test_find_with_a_root_that_is_an_outside_junction_is_refused(self):
        self.link(self.auth / "j", self.outside)
        with self.assertRaises(AuthorizationError):
            self.f.find("*", root=str(self.auth / "j"))

    def test_safe_walk_reports_every_link_it_does_not_follow(self):
        self.link(self.auth / "j", self.outside)
        skipped = []
        seen = [fn for _, _, files in safe_walk(self.auth, on_skip=lambda p, why: skipped.append(Path(p).name)) for fn in files]
        self.assertEqual(sorted(seen), ["inner.txt", "ok.txt"])
        self.assertEqual(skipped, ["j"])

    def test_the_depth_limit_still_applies(self):
        deep = self.auth
        for i in range(10):
            deep = deep / f"d{i}"
            deep.mkdir()
        (deep / "deep.txt").write_text("d", encoding="utf-8")
        self.assertEqual([r for r in self.f.find("deep.txt")], [])
        self.assertTrue(self.f.find("deep.txt", max_depth=20))


class UploadAndSaveTests(Base):
    def test_read_for_upload_refuses_through_a_junction_and_works_normally(self):
        self.link(self.auth / "j", self.outside)
        with self.assertRaises(AuthorizationError):
            self.f.read_for_upload(str(self.auth / "j" / "secret.txt"), 1000)
        info = self.f.read_for_upload(str(self.auth / "ok.txt"), 1000)
        self.assertEqual((info["name"], info["data"]), ("ok.txt", b"ok"))

    def test_a_path_that_is_re_pointed_while_the_file_is_read_is_refused(self):
        calls = {"n": 0}
        real = self.f.resolve_file

        def flip(raw):
            calls["n"] += 1
            return real(raw) if calls["n"] == 1 else real(str(self.auth / "docs" / "inner.txt"))   # the second look finds a different real file

        self.f.resolve_file = flip
        with self.assertRaises(AuthorizationError) as cm:
            self.f.read_for_upload(str(self.auth / "ok.txt"), 1000)
        self.assertIn("changed while", cm.exception.message)

    def test_save_into_refuses_a_destination_that_is_an_outside_junction(self):
        self.link(self.auth / "j", self.outside)
        src = self.root / "download.bin"
        src.write_bytes(b"data")
        with self.assertRaises(AuthorizationError):
            self.f.save_into(str(self.auth / "j"), "x.bin", src)
        self.assertEqual(list(self.outside.glob("x.bin")), [])
        self.assertTrue(self.f.save_into(str(self.auth / "docs"), "x.bin", src)["saved_path"].endswith("x.bin"))


if __name__ == "__main__":
    unittest.main()
