#!/usr/bin/env python3
"""Unit tests for the pure helpers of filelist.py.

Run: python3 -m unittest -v test_filelist.py
(or: python3 test_filelist.py)

These cover the rendering / layout / classification logic that has no herdr or
PTY dependencies. They exist so refactors of `main()` and the input layer stay
safe.
"""
import os
import sys
import tempfile
import unittest

import filelist as F

DEFAULT_OPT = dict(F._OPT)


class TextHelpers(unittest.TestCase):
    def test_ellipsize(self):
        self.assertEqual(F.ellipsize("abcdef", 6), "abcdef")
        self.assertEqual(F.ellipsize("abcdef", 4), "abc…")
        self.assertEqual(F.ellipsize("ab", 4), "ab")
        self.assertEqual(F.ellipsize("abcdef", 1), "…")
        self.assertEqual(F.ellipsize("abcdef", 0), "")
        self.assertEqual(F.ellipsize("", 4), "")

    def test_ellipsize_left_keeps_tail(self):
        self.assertEqual(F.ellipsize_left("abcdef", 4), "…def")
        self.assertEqual(F.ellipsize_left("ab", 4), "ab")
        self.assertEqual(F.ellipsize_left("abcdef", 1), "…")

    def test_shorten_path_home_and_truncate(self):
        home = os.path.expanduser("~")
        self.assertEqual(F.shorten_path(home, 40), "~")
        self.assertEqual(F.shorten_path(home + "/code/x", 40), "~/code/x")
        # left-truncated when too long
        s = F.shorten_path(home + "/a/b/c", 6)
        self.assertTrue(s.startswith("…"))
        self.assertLessEqual(len(s), 6)

    def test_split_indicator(self):
        self.assertEqual(F._split_indicator("a/"), ("a", "/"))
        self.assertEqual(F._split_indicator("a@"), ("a", "@"))
        self.assertEqual(F._split_indicator("a*"), ("a", "*"))
        self.assertEqual(F._split_indicator("a"), ("a", ""))

    def test_fit_entry_preserves_indicator(self):
        self.assertEqual(F.fit_entry("ab/", 8), "ab/")
        long_dir = F.fit_entry("verylongdirname/", 10)
        self.assertTrue(long_dir.endswith("/"))
        self.assertLessEqual(len(long_dir), 10)
        # exec star is preserved too
        long_exec = F.fit_entry("verylongscript*", 10)
        self.assertTrue(long_exec.endswith("*"))


class Coloring(unittest.TestCase):
    def _plain(self, s):
        return F._ANSI_RE.sub("", s)

    def test_style_name_by_type(self):
        self.assertTrue(self._plain(F.style_name("x/")).endswith("/"))
        self.assertEqual(self._plain(F.style_name("x@")), "x@")
        # exec star is stripped in display
        self.assertEqual(self._plain(F.style_name("x*")), "x")
        self.assertEqual(self._plain(F.style_name("x")), "x")

    def test_git_gutter_is_two_cells(self):
        for code in ("M", "A", "D", "R", "?", "U"):
            self.assertEqual(len(self._plain(F.git_gutter(code))), 2,
                             f"gutter for {code} not 2 cells")
        self.assertEqual(F.git_gutter(None), "  ")
        self.assertEqual(F.git_gutter("X"), "  ")


class SortAndEntries(unittest.TestCase):
    def setUp(self):
        F._OPT = dict(DEFAULT_OPT)

    def tearDown(self):
        F._OPT = dict(DEFAULT_OPT)

    def test_dirs_first(self):
        F._OPT["dirs"] = True
        raw = "zfile\nAdir/\nbfile\n"
        names = [e["name"] for e in F.render_entries(raw, {}, 40)]
        self.assertEqual(names, ["Adir", "bfile", "zfile"])

    def test_dirs_first_off_is_alpha(self):
        F._OPT["dirs"] = False
        raw = "zfile\nAdir/\nbfile\n"
        names = [e["name"] for e in F.render_entries(raw, {}, 40)]
        self.assertEqual(names, ["Adir", "bfile", "zfile"])  # case-insensitive alpha

    def test_mtime_passthrough_preserves_order(self):
        F._OPT["mtime"] = True
        raw = "old.txt\nz/\nmid.md\n"
        names = [e["name"] for e in F.render_entries(raw, {}, 40)]
        self.assertEqual(names, ["old.txt", "z", "mid.md"])  # not re-sorted

    def test_symlink_to_dir_is_dir(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "realdir"))
            open(os.path.join(d, "realfile"), "w").close()
            os.symlink("realdir", os.path.join(d, "dirlink"))
            os.symlink("realfile", os.path.join(d, "filelink"))
            os.symlink("/nowhere", os.path.join(d, "broken"))
            by = {e["name"]: e["dir"] for e in
                  F.render_entries("dirlink@\nfilelink@\nbroken@\nrealdir/\n", {}, 40, d)}
            self.assertTrue(by["dirlink"])    # symlink -> dir
            self.assertFalse(by["filelink"])  # symlink -> file
            self.assertFalse(by["broken"])    # broken symlink
            self.assertTrue(by["realdir"])

    def test_git_gutter_mapping(self):
        gmap = {"file.py": "M", "new.py": "A", "gone.py": "D"}
        recs = {e["name"]: e for e in F.render_entries("file.py\nnew.py\ngone.py\nclean.py\n", gmap, 40)}
        self.assertIn("M", recs["file.py"]["line"])
        self.assertIn("A", recs["new.py"]["line"])
        self.assertIn("D", recs["gone.py"]["line"])
        # clean entry has an empty 2-cell gutter (no symbol)
        self.assertEqual(F._ANSI_RE.sub("", recs["clean.py"]["line"])[:2], "  ")

    def test_empty_msg(self):
        recs = F._msg("(empty)")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["name"], "")


class Layout(unittest.TestCase):
    def setUp(self):
        F._OPT = dict(DEFAULT_OPT)

    def test_entry_at_row(self):
        entries = F.render_entries("a/\nb\nc\n", {}, 40)
        self.assertIsNone(F.entry_at_row(entries, 24, 1))   # title
        self.assertEqual(F.entry_at_row(entries, 24, 2), "a")
        self.assertEqual(F.entry_at_row(entries, 24, 3), "b")
        self.assertIsNone(F.entry_at_row(entries, 24, 99))

    def test_menu_action_at_row(self):
        # k=2 actions occupy rows rows-2 .. rows-1
        rows = 10
        self.assertEqual(F.menu_action_at_row(rows, 2, rows - 2), 0)
        self.assertEqual(F.menu_action_at_row(rows, 2, rows - 1), 1)
        self.assertIsNone(F.menu_action_at_row(rows, 2, rows - 3))
        self.assertIsNone(F.menu_action_at_row(rows, 2, rows))

    def test_compose_shape(self):
        entries = F.render_entries("a/\nb\nc\nd\n", {}, 40)
        screen = F.compose("/x", entries, "main", 40, 10)
        lines = screen.split("\n")
        self.assertEqual(len(lines), 10)               # exactly `rows` lines
        self.assertTrue(lines[0].startswith("\x1b[1m"))  # title rule bold
        self.assertIn("main", lines[-1])               # footer has branch
        self.assertLessEqual(max(len(F._ANSI_RE.sub("", l)) for l in lines), 40)

    def test_compose_fewer_entries_than_rows(self):
        # regression guard: must not IndexError
        entries = F.render_entries("only/\n", {}, 40)
        screen = F.compose("/x", entries, None, 40, 30)
        self.assertEqual(len(screen.split("\n")), 30)

    def test_compose_more_hint(self):
        entries = F.render_entries("a/\nb\nc\nd\n", {}, 40)
        screen = F.compose("/x", entries, None, 40, 4)   # only 2 entry rows
        self.assertIn("not shown", screen)

    def test_compose_selected_bar(self):
        entries = F.render_entries("a/\nb\n", {}, 40)
        screen = F.compose("/x", entries, None, 40, 6, selected_name="b", hint=True)
        # the selected row is a full-width reverse bar containing "..."
        self.assertIn("\x1b[7m", screen)
        self.assertIn("...", screen)

    def test_compose_preview_offset_and_binary(self):
        lines = [f"line{i}" for i in range(100)]
        s0 = F.compose_preview("f", lines, 0, 40, 8)
        self.assertIn("f", s0.split("\n")[0])           # title = filename
        self.assertIn("line0", s0)
        self.assertIn("1-6/100", s0)                    # footer position
        s40 = F.compose_preview("f", lines, 40, 40, 8)
        self.assertIn("line40", s40)
        # binary
        sb = F.compose_preview("bin", None, 0, 40, 6)
        self.assertIn("binary", sb)


class Actions(unittest.TestCase):
    def test_build_actions_dir_and_file(self):
        d_acts = F.build_actions("/p/d", True, "P1", True)
        ops = {a["op"] for a in d_acts}
        self.assertIn("open_pane", ops)
        self.assertIn("cd_into", ops)   # followed is a shell
        self.assertIn("copy", ops)
        f_acts = F.build_actions("/p/f.py", False, "P1", False)
        self.assertFalse(any(a["op"] == "cd_into" for a in f_acts))  # not a shell
        self.assertTrue(any(a["op"] == "open_pane" for a in f_acts))

    def test_settings_actions_keys(self):
        keys = {a["key"] for a in F.settings_actions()}
        self.assertEqual(keys, {"hidden", "git", "dirs", "mtime"})
        for a in F.settings_actions():
            self.assertTrue(a["label"].startswith(("✓", "•")))

    def test_extract_pane_id(self):
        self.assertEqual(F._extract_pane_id({"result": {"pane": {"pane_id": "X"}}}), "X")
        self.assertEqual(F._extract_pane_id({"pane_id": "Y", "extra": {"pane_id": "Z"}}), "Y")
        self.assertIsNone(F._extract_pane_id({}))

    def test_is_shell(self):
        self.assertTrue(F.is_shell("bash"))
        self.assertTrue(F.is_shell("zsh"))
        self.assertFalse(F.is_shell("node"))
        self.assertFalse(F.is_shell("python3.13"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
