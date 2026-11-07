#!/usr/bin/env python3
"""
Tests for reordering across forks, merges and off-line merges.  Standard library only.

    python test_forks.py
    GIT_BUBBLE_CMD="python3 /path/to/run_tool.py" python test_forks.py

Rules under test
  * the tip snapshot never changes
  * a side branch stays attached to its fork point BY NAME
  * side commits are replayed once each, chained; hunks that don't fit are dropped
    and come back at the (tree-preserving) merge
  * a fork point may not be moved past the merge that joins its branch back
"""
import os
import random
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
DEFAULT_CMD = f"uv run --project {shlex.quote(str(PROJECT))} git-rebubble"
TOOL = shlex.split(os.environ.get("GIT_BUBBLE_CMD", DEFAULT_CMD))

GIT_ENV = dict(
    os.environ,
    GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_NOSYSTEM="1",
    GIT_AUTHOR_NAME="Test Author", GIT_AUTHOR_EMAIL="author@example.com",
    GIT_COMMITTER_NAME="Test Committer", GIT_COMMITTER_EMAIL="committer@example.com",
    GIT_EDITOR=":", GIT_TERMINAL_PROMPT="0", PYTHONUNBUFFERED="1",
)
GIT_ENV.pop("GIT_SEQUENCE_EDITOR", None)


class Repo:
    def __init__(self, path):
        self.path = Path(path)
        self.git("init", "-q")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")

    def git(self, *args, check=True):
        r = subprocess.run(["git", *args], cwd=self.path, env=GIT_ENV,
                           capture_output=True, text=True)
        if check and r.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed:\n{r.stdout}{r.stderr}")
        return r

    def out(self, *args):
        return self.git(*args).stdout.strip()

    def commit(self, msg, files=None):
        for name, text in (files or {}).items():
            (self.path / name).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", msg)
        return self.out("rev-parse", "HEAD")

    def branch(self, name, at="HEAD"):
        self.git("checkout", "-q", "-b", name, at)

    def checkout(self, name):
        self.git("checkout", "-q", name)

    def merge(self, branch, msg, *more):
        self.git("merge", "-q", "--no-ff", "-m", msg, branch, *more)
        return self.out("rev-parse", "HEAD")

    def tree(self, rev="HEAD"):
        return self.out("rev-parse", f"{rev}^{{tree}}")

    def files(self, rev):
        names = self.out("ls-tree", "-r", "--name-only", rev).splitlines()
        return {n: self.git("show", f"{rev}:{n}").stdout for n in names}

    def parents(self, rev):
        return self.out("rev-list", "--parents", "-n1", rev).split()[1:]

    def by_subject(self, base):
        """{subject: sha} for everything in base..HEAD (side branches included)."""
        rows = self.out("log", "--format=%H\t%s", f"{base}..HEAD").splitlines()
        return {s: h for h, s in (r.split("\t", 1) for r in rows)}

    def count(self, base):
        return int(self.out("rev-list", "--count", f"{base}..HEAD"))

    def line_subjects(self, base):
        return self.out("log", "--first-parent", "--reverse", "--format=%s", f"{base}..HEAD").splitlines()

    def run_tool(self, *args):
        return subprocess.run(TOOL + list(args), cwd=self.path, env=GIT_ENV,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180)


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Repo(tmp.name)

    def ok(self, base, before_tree, *args):
        res = self.repo.run_tool(base, *args)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.repo.tree(), before_tree, "tip snapshot changed!")
        self.assertEqual(self.repo.out("status", "--porcelain"), "")
        self.assertEqual(self.repo.git("fsck", "--no-dangling", check=False).returncode, 0)
        return res

    # ---- history builders
    def build_fork(self, extra=None):
        """line: A F E G M H   side (forked at F): S1 (merged by M).  base is returned.
        Positions: A=1 F=2 E=3 G=4 M=5 H=6."""
        r = self.repo
        base = r.commit("0", {"cfg": "x\n"})
        r.commit("A", {"A": "A\n"})
        r.commit("F", {"F": "F\n"})
        r.branch("side")
        r.commit("S1", {"s1": "s1\n"})
        r.checkout("main")
        r.commit("E", {"e": "E\n"})
        r.commit("G", {"g": "G\n"})
        r.merge("side", "M")
        r.commit("H", {"h": "H\n"})
        return base


class TestForkAttach(Case):
    def test_commit_pushed_before_fork_enters_the_side_branch(self):
        r = self.repo
        base = self.build_fork()
        before = r.tree()
        n = r.count(base)
        self.ok(base, before, "--order", "1,3,2,4,5,6", "--auto")      # E before F
        self.assertEqual(r.line_subjects(base), ["A", "E", "F", "G", "M", "H"])
        by = r.by_subject(base)
        self.assertEqual(r.count(base), n, "no commit may be duplicated or lost")
        s1, f = by["S1"], by["F"]
        self.assertEqual(r.parents(s1), [f], "side branch must stay attached to F (by name)")
        self.assertEqual(set(r.files(s1)), {"cfg", "A", "e", "F", "s1"}, "side now contains E")
        self.assertEqual(r.parents(by["M"])[1], s1)

    def test_commit_pushed_behind_fork_leaves_the_side_branch(self):
        r = self.repo
        base = r.commit("0")
        r.commit("A", {"A": "A\n"})
        r.commit("E", {"e": "E\n"})
        r.commit("F", {"F": "F\n"})
        r.branch("side")
        r.commit("S1", {"s1": "s1\n"})
        r.checkout("main")
        r.commit("G", {"g": "G\n"})
        r.merge("side", "M")
        before = r.tree()
        self.ok(base, before, "--order", "1,3,2,4,5", "--auto")        # E after F
        by = r.by_subject(base)
        self.assertEqual(set(r.files(by["S1"])), {"A", "F", "s1"}, "E must be gone from the side")
        self.assertEqual(set(r.files(by["M"])), {"A", "e", "F", "g", "s1"}, "...and back at the merge")

    def test_unchanged_fork_snapshot_means_reparent_only(self):
        """Swapping two commits BEFORE the fork leaves F's snapshot alone."""
        r = self.repo
        base = r.commit("0")
        r.commit("A", {"A": "A\n"})
        r.commit("B", {"B": "B\n"})
        r.commit("F", {"F": "F\n"})
        r.branch("side")
        s1_tree = None
        r.commit("S1", {"s1": "s1\n"})
        s1_tree = r.tree()
        r.checkout("main")
        r.commit("G", {"g": "G\n"})
        r.merge("side", "M")
        before = r.tree()
        res = self.ok(base, before, "--order", "2,1,3,4,5", "--auto")
        by = r.by_subject(base)
        self.assertEqual(r.tree(by["S1"]), s1_tree)
        self.assertEqual(r.parents(by["S1"]), [by["F"]])
        self.assertIn("0 side commit(s) replayed, 1 re-parented", res.stdout)

    def test_side_branch_forked_at_base_is_untouched(self):
        r = self.repo
        base = r.commit("0")
        r.branch("side")
        s1 = r.commit("S1", {"s1": "s1\n"})
        r.checkout("main")
        r.commit("A", {"A": "A\n"})
        r.commit("B", {"B": "B\n"})
        r.merge("side", "M")
        r.commit("H", {"h": "H\n"})
        before = r.tree()
        self.ok(base, before, "--order", "2,1,3,4", "--auto")
        self.assertEqual(r.by_subject(base)["S1"], s1, "side commit must keep its hash")
        self.assertEqual(r.parents(r.by_subject(base)["M"])[1], s1)


class TestMergesOnTheLine(Case):
    def test_move_after_merge_commit_before_it(self):
        r = self.repo
        base = self.build_fork()
        before = r.tree()
        self.ok(base, before, "--order", "1,2,3,4,6,5", "--auto")     # H before M
        self.assertEqual(r.line_subjects(base), ["A", "F", "E", "G", "H", "M"])
        m = r.by_subject(base)["M"]
        self.assertEqual(len(r.parents(m)), 2)
        self.assertEqual(r.parents(m)[1], r.by_subject(base)["S1"])

    def test_move_commit_across_merge_and_fork(self):
        r = self.repo
        base = self.build_fork()
        before = r.tree()
        self.ok(base, before, "--order", "6,1,2,3,4,5", "--auto")     # H first
        by = r.by_subject(base)
        self.assertEqual(r.line_subjects(base), ["H", "A", "F", "E", "G", "M"])
        self.assertIn("h", r.files(by["S1"]), "H is now below the fork, so the side has it")

    def test_merge_commit_moved_earlier_but_not_past_its_fork(self):
        r = self.repo
        base = self.build_fork()
        before = r.tree()
        self.ok(base, before, "--order", "1,2,5,3,4,6", "--auto")    # M before E and G
        self.assertEqual(r.line_subjects(base), ["A", "F", "M", "E", "G", "H"])

    def test_fork_past_its_merge_is_refused_and_nothing_moves(self):
        r = self.repo
        base = self.build_fork()
        head, tree = r.out("rev-parse", "HEAD"), r.tree()
        res = r.run_tool(base, "--order", "1,3,4,5,2,6", "--auto")   # F after M
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("fork point", res.stdout + res.stderr)
        self.assertNotIn("Traceback", res.stderr)
        self.assertEqual(r.out("rev-parse", "HEAD"), head)
        self.assertEqual(r.tree(), tree)
        self.assertEqual(r.out("status", "--porcelain"), "")

    def test_merge_moved_before_its_fork_is_refused(self):
        r = self.repo
        base = self.build_fork()
        head = r.out("rev-parse", "HEAD")
        res = r.run_tool(base, "--order", "1,5,2,3,4,6", "--auto")   # M before F
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("fork point", res.stdout + res.stderr)
        self.assertEqual(r.out("rev-parse", "HEAD"), head)


class TestReplayConflicts(Case):
    def build(self):
        r = self.repo
        base = r.commit("0")
        r.commit("A", {"cfg": "x\n"})
        r.commit("E", {"cfg": "x\nE\n"})
        r.commit("F", {"F": "F\n"})
        r.branch("side")
        r.commit("S1", {"cfg": "x\nE\nS\n"})            # builds on E's line
        r.checkout("main")
        r.commit("G", {"g": "G\n"})
        r.merge("side", "M")
        return base

    def test_auto_drops_what_does_not_fit_and_merge_restores_it(self):
        r = self.repo
        base = self.build()
        before = r.tree()
        self.ok(base, before, "--order", "1,3,2,4,5", "--auto")     # E after F
        by = r.by_subject(base)
        self.assertEqual(r.files(by["S1"])["cfg"], "x\n", "S1's hunk needs E, so it is dropped")
        self.assertEqual(r.files(by["M"])["cfg"], "x\nE\nS\n", "the merge brings it back")

    def test_noninteractive_conflict_aborts_and_restores_everything(self):
        r = self.repo
        base = self.build()
        head, tree = r.out("rev-parse", "HEAD"), r.tree()
        res = r.run_tool(base, "--order", "1,3,2,4,5")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("terminal", (res.stdout + res.stderr).lower())
        self.assertNotIn("Traceback", res.stderr)
        self.assertEqual(r.out("rev-parse", "HEAD"), head)
        self.assertEqual(r.tree(), tree)
        self.assertEqual(r.out("status", "--porcelain"), "")
        self.assertEqual(r.out("symbolic-ref", "--short", "HEAD"), "main")


class TestOffLineMerge(Case):
    """F forks two branches that merge into EACH OTHER before joining the line:
         side:  S1 - S2 --.
         other: T1 -------+- U ---.
       line:  A B F E G ---------- M H"""
    FILES = {"S1": "s1", "S2": "s2", "T1": "t1"}

    def build(self):
        r = self.repo
        base = r.commit("0")
        for x in "AB":
            r.commit(x, {x: f"{x}\n"})
        r.commit("F", {"F": "F\n"})
        r.branch("side")
        r.commit("S1", {"s1": "s1\n"})
        r.commit("S2", {"s2": "s2\n"})
        r.checkout("main")
        r.branch("other")
        r.commit("T1", {"t1": "t1\n"})
        r.checkout("side")
        r.merge("other", "U")
        r.checkout("main")
        r.commit("E", {"e": "E\n"})
        r.commit("G", {"g": "G\n"})
        r.merge("side", "M")
        r.commit("H", {"h": "H\n"})
        return base                       # positions A1 B2 F3 E4 G5 M6 H7

    def check_structure(self, base, n):
        r = self.repo
        by = r.by_subject(base)
        self.assertEqual(r.count(base), n, "every commit exactly once")
        f = by["F"]
        self.assertEqual(r.parents(by["S1"]), [f])
        self.assertEqual(r.parents(by["T1"]), [f])
        self.assertEqual(r.parents(by["S2"]), [by["S1"]])
        self.assertEqual(r.parents(by["U"]), [by["S2"], by["T1"]])
        self.assertEqual(r.parents(by["M"])[1], by["U"])
        want = set(r.files(f)) | {"s1", "s2", "t1"}
        self.assertEqual(set(r.files(by["U"])), want)

    def test_push_E_before_the_fork(self):
        r = self.repo
        base = self.build()
        before, n = r.tree(), r.count(base)
        self.ok(base, before, "--order", "1,2,4,3,5,6,7", "--auto")
        self.check_structure(base, n)
        self.assertIn("e", r.files(r.by_subject(base)["U"]))

    def test_move_everything_around(self):
        r = self.repo
        base = self.build()
        before, n = r.tree(), r.count(base)
        self.ok(base, before, "--order", "7,2,1,3,5,4,6", "--auto")
        self.check_structure(base, n)

    def test_random_orders(self):
        for seed in range(30):
            with self.subTest(seed=seed):
                self.setUp()
                r = self.repo
                base = self.build()
                before, n, head = r.tree(), r.count(base), r.out("rev-parse", "HEAD")
                names = r.line_subjects(base)
                order = list(range(1, len(names) + 1))
                random.Random(seed).shuffle(order)
                res = r.run_tool(base, "--order", ",".join(map(str, order)), "--auto")
                pos = {names[i - 1]: k for k, i in enumerate(order)}
                if pos["F"] >= pos["M"]:
                    self.assertNotEqual(res.returncode, 0, "fork past merge must be refused")
                    self.assertEqual(r.out("rev-parse", "HEAD"), head)
                    continue
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                self.assertEqual(r.tree(), before)
                self.assertEqual(r.line_subjects(base), [names[i - 1] for i in order])
                self.check_structure(base, n)
                self.assertEqual(r.git("fsck", "--no-dangling", check=False).returncode, 0)


class TestRegression(Case):
    """The pre-fork behaviour must not change."""

    def test_linear_reorder_and_tip(self):
        r = self.repo
        base = r.commit("0")
        for x in "ABCD":
            r.commit(x, {x: f"{x}\n"})
        r.commit("E", {x: f"{x}\nE\n" for x in "BCD"})
        before = r.tree()
        self.ok(base, before, "--order", "1,2,5,3,4", "--auto")
        self.assertEqual(r.line_subjects(base), list("ABECD"))
        e = r.by_subject(base)["E"]
        self.assertEqual(r.files(e), {"A": "A\n", "B": "B\nE\n"})

    def test_refuses_dirty_tree_and_bad_order(self):
        r = self.repo
        base = r.commit("0")
        r.commit("A", {"A": "A\n"})
        r.commit("B", {"B": "B\n"})
        res = r.run_tool(base, "--order", "1,1", "--auto")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("permutation", res.stdout + res.stderr)
        (r.path / "A").write_text("dirty\n")
        res = r.run_tool(base, "--order", "2,1", "--auto")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("uncommitted", (res.stdout + res.stderr).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
