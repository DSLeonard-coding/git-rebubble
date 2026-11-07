#!/usr/bin/env python3
"""
Tests for git-rebubble.  Standard library only:

    python test_git_bubble.py            # or: python -m unittest -v test_git_bubble
    python -m pytest test_git_bubble.py  # also works

By default the tool under test is the project's own .venv/bin/git-rebubble if it
exists (fast, and immune to `uv run` re-syncing the environment between calls),
otherwise `uv run --project <root> git-rebubble`.  To test another entry point:

    GIT_BUBBLE_CMD="git-rebubble" python test_git_bubble.py
    GIT_BUBBLE_CMD="uv run git-rebubble" python test_git_bubble.py

Every test builds a throwaway repo in a temp directory, with git's global and
system config switched off so your own settings can't leak in.

The one property that must hold for EVERY reorder, whatever the conflicts:
the tip snapshot (HEAD's tree) is identical before and after.

Rules under test (forks and merges)
  * a side branch stays attached to its fork point BY NAME
  * side commits are replayed once each, chained; hunks that don't fit are dropped
    and come back at the (tree-preserving) merge
  * a fork point may not be moved past the merge that joins its branch back
  * the line is the first-parent path by default; --via routes it through another leg

Rebuilt commits are never compared by sha: finalize() writes new commit objects, and
their committer date depends on the clock.  Compare subjects and parent structure.
"""
import itertools
import os
import pty
import random
import select
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from dsleonard_qx import *
from pathlib import Path

qx.verbosity_thresholds(out_thrsh=Qx.INFO)
HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent          # tests/test.py -> project root
_VENV_BIN = PROJECT / ".venv" / "bin" / "git-rebubble"
if _VENV_BIN.exists():
    DEFAULT_CMD = shlex.quote(str(_VENV_BIN))
else:
    DEFAULT_CMD = f"uv run --project {shlex.quote(str(PROJECT))} git-rebubble"

TOOL = shlex.split(os.environ.get("GIT_BUBBLE_CMD", DEFAULT_CMD))

# Text the tool prints (once per prompt) when it drops you into a shell to
# resolve a conflict by hand: "exit 1 / to abort".
PROMPT_MARK = "to abort"

GIT_ENV = dict(
    os.environ,
    GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_NOSYSTEM="1",
    GIT_AUTHOR_NAME="Test Author", GIT_AUTHOR_EMAIL="author@example.com",
    GIT_COMMITTER_NAME="Test Committer", GIT_COMMITTER_EMAIL="committer@example.com",
    GIT_EDITOR=":", GIT_TERMINAL_PROMPT="0", PYTHONUNBUFFERED="1",
)
GIT_ENV.pop("GIT_SEQUENCE_EDITOR", None)


# ------------------------------------------------------------------ helpers

class Repo:
    def __init__(self, path):
        self.path = Path(path)
        self.git("init", "-q")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")

    def git(self, *args, env=None, check=True, input=None):
        r = qx(["git", *args], cwd=self.path, env={**GIT_ENV, **(env or {})},
                            input=input)
        if check and r.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed:\n{r.stdout}{r.stderr}")
        return r

    def out(self, *args, **kw):
        return self.git(*args, **kw).stdout.strip()

    def commit(self, message, files=None, author=None, date=None):
        for name, text in (files or {}).items():
            p = self.path / name
            if text is None:
                p.unlink()
                msg(f"rm {text}\n",echo_lvl=Qx.DEBUG)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text)
                msg(f"\n echo {text} >> {name}\n",echo_lvl=Qx.DEBUG)
        self.git("add", "-A")
        env = {}
        if author:
            env.update(GIT_AUTHOR_NAME=author[0], GIT_AUTHOR_EMAIL=author[1])
        if date:
            env["GIT_AUTHOR_DATE"] = date
        self.git("commit", "-q", "--allow-empty", "-m", message, env=env)
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

    def parents(self, rev):
        return self.out("rev-list", "--parents", "-n1", rev).split()[1:]

    def subject(self, rev="HEAD"):
        return self.out("log", "-1", "--format=%s", rev)

    def commits(self, base):
        return self.out("rev-list", "--reverse", f"{base}..HEAD").split()

    def subjects(self, base):
        return self.out("log", "--reverse", "--format=%s", f"{base}..HEAD").splitlines()

    def by_subject(self, base):
        """{subject: sha} for everything in base..HEAD (side branches included)."""
        rows = self.out("log", "--format=%H\t%s", f"{base}..HEAD").splitlines()
        return {s: h for h, s in (r.split("\t", 1) for r in rows)}

    def count(self, base):
        return int(self.out("rev-list", "--count", f"{base}..HEAD"))

    def line_subjects(self, base):
        return self.out("log", "--first-parent", "--reverse", "--format=%s", f"{base}..HEAD").splitlines()

    def snapshot(self, rev):
        names = self.out("ls-tree", "-r", "--name-only", rev).splitlines()
        return {n: self.git("show", f"{rev}:{n}").stdout for n in names}

    def run_tool(self, *args, stdin=subprocess.DEVNULL, env=None):
        return qx(TOOL + list(args), cwd=self.path,
                              env={**GIT_ENV, **(env or {})}, stdin=stdin,
                               timeout=180)

def build_letters(repo):
    """The README repro: commits A,B,C,D each create their own file;
    E then appends 'E' to B, C and D.  Returns the base (an empty commit)."""
    base = repo.commit("0")
    for x in "ABCD":
        repo.commit(x, {x: f"{x}\n"})
    repo.commit("E", {x: f"{x}\nE\n" for x in "BCD"})
    return base


PARSER_1 = 'def parse(s):\n    return s.split(",")\n'
PARSER_2 = ('def trace(x):\n    print(x)\n    return x\n\n\n'
            'def parse(s):\n    return trace(s.split(","))\n')
PARSER_5 = PARSER_2.replace('trace(s.split(","))', 'trace([p.strip() for p in s.split(",")])')


def build_app(repo):
    """A small structured history with one REAL text conflict when commit 5
    ('strip') is moved in front of commit 2 ('trace'): both touch the same line.
    Returns the base commit."""
    base = repo.commit("base", {"VERSION": "0.1\n"})
    repo.commit("add parser", {"parser.py": PARSER_1})
    repo.commit("trace parse", {"parser.py": PARSER_2})
    repo.commit("add cli", {"cli.py": 'from parser import parse\n\nprint(parse("a, b"))\n'})
    repo.commit("docs", {"README.md": "usage: python cli.py\n"})
    repo.commit("strip whitespace", {
        "parser.py": PARSER_5,
        "test_parser.py": 'from parser import parse\n\nassert parse("a, b") == ["a", "b"]\n'})
    repo.commit("bump version", {"VERSION": "0.2\n"})
    return base


class RepoCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Repo(tmp.name)

    def parents(self, sha):
        return self.repo.parents(sha)

    def assert_chain(self, base, expected):
        """Compare the FULL snapshot (every file, every byte) of every commit in
        base..HEAD against a list of {path: content} dicts, oldest first."""
        shas = self.repo.commits(base)
        self.assertEqual(len(shas), len(expected), "wrong number of commits")
        for k, (sha, want) in enumerate(zip(shas, expected)):
            self.assertEqual(self.repo.snapshot(sha), want,
                             f"slot {k} ({self.repo.out('log', '-1', '--format=%s', sha)!r})")

    def check_common(self, res, before):
        """Success, tip snapshot unchanged, clean tree, healthy object database."""
        r = self.repo
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(r.tree("HEAD"), before, "tip snapshot changed!")
        self.assertEqual(r.out("status", "--porcelain"), "", "working tree not clean")
        self.assertEqual(r.git("fsck", "--no-dangling", check=False).returncode, 0)

    def assert_reordered(self, base, expected_subjects, tree_before, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.repo.subjects(base), expected_subjects)
        self.check_common(result, tree_before)

    def run_ok(self, base, before_tree, *args):
        """Run the tool, require success and an unchanged tip."""
        res = self.repo.run_tool(base, *args)
        self.check_common(res, before_tree)
        return res

    def assert_refused(self, res, head, needle="fork point"):
        """The tool refused cleanly and nothing moved."""
        self.assertNotEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn(needle, (res.stdout + res.stderr).lower())
        self.assertNotIn("Traceback", res.stderr)
        self.assertEqual(self.repo.out("rev-parse", "HEAD"), head)
        self.assertEqual(self.repo.out("status", "--porcelain"), "")

    def build_fork(self):
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


# -------------------------------------------------------------------- tests


class TestReadmeRepro(RepoCase):
    """The example from the README, where `rebase -i` loses data."""

    def test_move_E_after_B(self):
        base = build_letters(self.repo)
        before = self.repo.tree()
        res = self.repo.run_tool(base, "--order", "1,2,5,3,4", "--auto-squash-right")
        self.assert_reordered(base, list("ABECD"), before, res)

        a, b, e, c, d = (self.repo.snapshot(s) for s in self.repo.commits(base))
        self.assertEqual(a, {"A": "A\n"})
        self.assertEqual(b, {"A": "A\n", "B": "B\n"})
        # The point of the tool: a CLEAN E', touching only B.
        self.assertEqual(e, {"A": "A\n", "B": "B\nE\n"})
        self.assertEqual(c, {"A": "A\n", "B": "B\nE\n", "C": "C\nE\n"})
        self.assertEqual(d, {"A": "A\n", "B": "B\nE\n", "C": "C\nE\n", "D": "D\nE\n"})

    def test_full_reversal(self):
        base = build_letters(self.repo)
        before = self.repo.tree()
        res = self.repo.run_tool(base, "--order", "5,4,3,2,1", "--auto-squash-right")
        self.assert_reordered(base, list("EDCBA"), before, res)

    def test_mixed_orders(self):
        for order in ("1,3,2,5,4", "2,1,5,4,3", "3,1,2,4,5", "1,2,3,5,4"):
            with self.subTest(order=order):
                self.setUp()
                base = build_letters(self.repo)
                before = self.repo.tree()
                res = self.repo.run_tool(base, "--order", order, "--auto-squash-right")
                expected = ["ABCDE"[int(n) - 1] for n in order.split(",")]
                self.assert_reordered(base, expected, before, res)


class TestBlockMove(RepoCase):
    def test_FGHIJ_before_ABCDE_takes_25_swaps(self):
        base = self.repo.commit("0")
        for x in "ABCDEFGHIJ":
            self.repo.commit(x, {x: f"{x}\n"})
        before = self.repo.tree()
        res = self.repo.run_tool(base, "--order", "6,7,8,9,10,1,2,3,4,5", "--auto-squash-right")
        self.assert_reordered(base, list("FGHIJABCDE"), before, res)
        swaps = [l for l in res.stdout.splitlines() if l.startswith("swap:")]
        self.assertEqual(len(swaps), 25, "5 commits past 5 commits = 25 inversions")
        # sweep order: F is pulled back over E,D,C,B,A before G starts
        self.assertIn("'F'", swaps[0])
        self.assertIn("'F'", swaps[4])
        self.assertIn("'G'", swaps[5])


class TestRealConflicts(RepoCase):
    ORDER = "1,5,2,3,4,6"      # move 'strip whitespace' in front of 'trace parse'
    EXPECTED = ["add parser", "strip whitespace", "trace parse", "add cli", "docs", "bump version"]

    def test_abort_after_resolving(self):
        """Resolve + git add, THEN change our mind: --abort, then exit.  Nothing may move."""
        base = build_app(self.repo)
        head, tree = self.repo.out("rev-parse", "HEAD"), self.repo.tree()
        mine = 'def parse(s):\n    return [p.strip() for p in s.split(",")]\n'

        def resolve(_n):
            (self.repo.path / "parser.py").write_text(mine)
            self.repo.git("add", "parser.py")

        abort_cmd = " ".join(shlex.quote(t) for t in TOOL) + " --abort\nexit\n"
        code, out, prompts = self.run_with_prompts([base, "--order", self.ORDER], resolve,
                                                   reply=abort_cmd.encode())
        self.assertNotEqual(code, 0, out)
        self.assertEqual(prompts, 1, out)
        self.assertIn("aborted", out.lower())
        self.assertNotIn("Traceback", out)
        r = self.repo
        self.assertEqual(r.out("rev-parse", "HEAD"), head, "branch must not move")
        self.assertEqual(r.tree(), tree)
        self.assertEqual(r.out("status", "--porcelain"), "")
        self.assertEqual(r.out("symbolic-ref", "--short", "HEAD"), "main")
        self.assertFalse((r.path / ".git" / "CHERRY_PICK_HEAD").exists())
        self.assertFalse((r.path / ".git" / "rebubble-abort").exists(), "flag file must be cleaned up")

    def test_auto_keeps_tip_and_drops_what_does_not_fit(self):
        base = build_app(self.repo)
        before = self.repo.tree()
        res = self.repo.run_tool(base, "--order", self.ORDER, "--auto-squash-right")
        self.assert_reordered(base, self.EXPECTED, before, res)
        shas = self.repo.commits(base)
        moved = self.repo.snapshot(shas[1])           # 'strip whitespace', moved back
        self.assertEqual(moved["parser.py"], PARSER_1, "auto keeps the earlier side")
        self.assertIn("test_parser.py", moved, "clean hunks from the moved commit survive")
        # ...and what was dropped comes back in the commit that follows:
        self.assertEqual(self.repo.snapshot(shas[2])["parser.py"], PARSER_5)

    def test_auto_keeps_clean_hunks_inside_a_conflicted_file(self):
        """One file, two far-apart hunks: line 1 conflicts, line 12 does not.
        --auto-squash-right must drop only the conflicting hunk, not the whole file."""
        lines = [f"line{i}" for i in range(1, 13)]
        def text(top=None, bottom=None):
            ls = list(lines)
            if top: ls[0] = top
            if bottom: ls[11] = bottom
            return "\n".join(ls) + "\n"
        base = self.repo.commit("base")
        self.repo.commit("add config", {"config.txt": text()})
        self.repo.commit("edit top", {"config.txt": text(top="top-edited")})
        self.repo.commit("edit both", {"config.txt": text(top="top-final", bottom="bottom-final")})
        before = self.repo.tree()
        res = self.repo.run_tool(base, "--order", "1,3,2", "--auto-squash-right")
        self.assert_reordered(base, ["add config", "edit both", "edit top"], before, res)
        shas = self.repo.commits(base)
        self.assertEqual(self.repo.snapshot(shas[1])["config.txt"], text(bottom="bottom-final"),
                         "moved commit: conflicting top hunk dropped, clean bottom hunk kept")
        self.assertEqual(self.repo.snapshot(shas[2])["config.txt"],
                         text(top="top-final", bottom="bottom-final"))

    def test_noninteractive_conflict_aborts_and_restores_everything(self):
        base = build_app(self.repo)
        head, tree = self.repo.out("rev-parse", "HEAD"), self.repo.tree()
        res = self.repo.run_tool(base, "--order", self.ORDER)       # no --auto-squash-right, no tty
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("terminal", (res.stdout + res.stderr).lower(), "should explain why it stopped")
        self.assertNotIn("Traceback", res.stderr)
        self.assertEqual(self.repo.out("rev-parse", "HEAD"), head, "branch must not move")
        self.assertEqual(self.repo.tree(), tree)
        self.assertEqual(self.repo.out("status", "--porcelain"), "")
        self.assertEqual(self.repo.out("symbolic-ref", "--short", "HEAD"), "main")

    def test_interactive_resolution_through_a_tty(self):
        """Resolve the conflict by hand at the prompt; the moved commit must
        contain exactly what we chose, and the tip must still be unchanged."""
        base = build_app(self.repo)
        before = self.repo.tree()
        mine = 'def parse(s):\n    return [p.strip() for p in s.split(",")]\n'   # strip, no trace

        def resolve(_n):
            (self.repo.path / "parser.py").write_text(mine)
            self.repo.git("add", "parser.py")

        code, out, prompts = self.run_with_prompts([base, "--order", self.ORDER], resolve)
        self.assertEqual(code, 0, out)
        self.assertEqual(prompts, 1, "only the (trace, strip) swap should conflict:\n" + out)
        self.assert_reordered_state(base, self.EXPECTED, before)
        shas = self.repo.commits(base)
        self.assertEqual(self.repo.snapshot(shas[1])["parser.py"], mine)
        self.assertEqual(self.repo.snapshot(shas[2])["parser.py"], PARSER_5)

    def assert_reordered_state(self, base, expected_subjects, tree_before):
        self.assertEqual(self.repo.subjects(base), expected_subjects)
        self.assertEqual(self.repo.tree("HEAD"), tree_before, "tip snapshot changed!")
        self.assertEqual(self.repo.out("status", "--porcelain"), "")

    def run_with_prompts(self, args, respond, timeout=90, reply=b"exit\n"):
        """Run the tool on a pty.  Each time the prompt appears, call respond(n)
        (resolve the conflict), then type `reply` (default: `exit`, which leaves
        the shell the tool spawned and lets it continue)."""
        master, slave = pty.openpty()
        p = subprocess.Popen(TOOL + list(args), cwd=self.repo.path, env=GIT_ENV, stdin=slave,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True)
        os.close(slave)
        out, prompts, deadline = b"", 0, time.time() + timeout
        try:
            while time.time() < deadline:
                ready, _, _ = select.select([p.stdout], [], [], 0.2)
                if ready:
                    chunk = os.read(p.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    out += chunk
                    if out.decode(errors="replace").count(PROMPT_MARK) > prompts:
                        prompts += 1
                        respond(prompts)
                        os.write(master, reply)
                elif p.poll() is not None:
                    break
            p.wait(timeout=30)
        finally:
            os.close(master)
            if p.poll() is None:
                p.kill()
            p.stdout.close()
        return p.returncode, out.decode(errors="replace"), prompts

    def test_random_orders_always_preserve_the_tip(self):
        """Property test: any permutation, real conflicts included, --auto-squash-right."""
        for seed in range(15):
            with self.subTest(seed=seed):
                self.setUp()
                base = build_app(self.repo)
                before = self.repo.tree()
                names = self.repo.subjects(base)
                order = list(range(1, len(names) + 1))
                random.Random(seed).shuffle(order)
                res = self.repo.run_tool(base, "--order", ",".join(map(str, order)), "--auto-squash-right")
                self.assert_reordered(base, [names[i - 1] for i in order], before, res)


class TestIndependentOracle(RepoCase):
    """The tool's own end-of-run check only compares the TIP.  Here every commit
    touches only its own file, so the correct snapshot at EVERY slot follows from
    the spec alone: the files of the first k commits in the new order.  A tool
    that left contamination from the future in intermediate commits would pass
    the tip check and fail this."""

    def test_every_intermediate_snapshot_matches_the_spec(self):
        names = list("ABCDEFG")
        for seed in range(15):
            with self.subTest(seed=seed):
                self.setUp()
                base = self.repo.commit("0")
                for x in names:
                    self.repo.commit(x, {x: f"{x}\n"})
                before = self.repo.tree()
                order = list(range(1, len(names) + 1))
                random.Random(seed).shuffle(order)
                res = self.repo.run_tool(base, "--order", ",".join(map(str, order)), "--auto-squash-right")
                self.assert_reordered(base, [names[i - 1] for i in order], before, res)
                for k, sha in enumerate(self.repo.commits(base)):
                    want = {names[i - 1]: f"{names[i - 1]}\n" for i in order[:k + 1]}
                    self.assertEqual(self.repo.snapshot(sha), want, f"slot {k}, order {order}")


class TestChainContents(RepoCase):
    """What does EVERY commit in the final chain contain?  Expected values come
    from the spec (see each test), never from the tool's own output."""

    def test_conflict_example_full_chain(self):
        """Hand-derived from the swap rule  P->X->Y  =>  P->Y'->X'  with X'.tree = Y.tree.
        'strip' hops back over docs, cli and finally 'trace', where its parser hunk
        conflicts and --auto-squash-right drops it; the dropped hunk reappears in 'trace parse'."""
        base = build_app(self.repo)
        before = self.repo.tree()
        res = self.repo.run_tool(base, "--order", "1,5,2,3,4,6", "--auto-squash-right")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        cli = 'from parser import parse\n\nprint(parse("a, b"))\n'
        docs = "usage: python cli.py\n"
        tests = 'from parser import parse\n\nassert parse("a, b") == ["a", "b"]\n'
        self.assert_chain(base, [
            {"VERSION": "0.1\n", "parser.py": PARSER_1},                                    # add parser
            {"VERSION": "0.1\n", "parser.py": PARSER_1, "test_parser.py": tests},           # strip: its hunk dropped
            {"VERSION": "0.1\n", "parser.py": PARSER_5, "test_parser.py": tests},           # trace: now carries it
            {"VERSION": "0.1\n", "parser.py": PARSER_5, "test_parser.py": tests, "cli.py": cli},
            {"VERSION": "0.1\n", "parser.py": PARSER_5, "test_parser.py": tests, "cli.py": cli,
             "README.md": docs},
            {"VERSION": "0.2\n", "parser.py": PARSER_5, "test_parser.py": tests, "cli.py": cli,
             "README.md": docs},
        ])
        self.assertEqual(self.repo.tree(), before)

    def test_shared_file_disjoint_hunks_oracle(self):
        """One file, commit i edits only region i (regions far apart, so no conflicts
        are possible).  Slot k must be the file with exactly the regions of the first
        k+1 commits, in the NEW order, edited."""
        n, gap = 5, 10
        lines = [f"line{i}" for i in range(1, n * gap + 1)]
        def text(edited):
            ls = list(lines)
            for i in edited:
                ls[i * gap - 1] = f"line{i * gap} edited by {i}"
            return "\n".join(ls) + "\n"
        base = self.repo.commit("base", {"data.txt": text([])})
        for i in range(1, n + 1):
            self.repo.commit(f"edit {i}", {"data.txt": text(range(1, i + 1))})   # cumulative, like real history
        orig = self.repo.out("rev-parse", "HEAD")
        before = self.repo.tree()
        for seed in range(20):
            with self.subTest(seed=seed):
                self.repo.git("reset", "-q", "--hard", orig)
                order = list(range(1, n + 1))
                random.Random(seed).shuffle(order)
                res = self.repo.run_tool(base, "--order", ",".join(map(str, order)), "--auto-squash-right")
                self.assert_reordered(base, [f"edit {i}" for i in order], before, res)
                self.assert_chain(base, [{"data.txt": text(order[:k + 1])} for k in range(n)])

    def test_dependency_family_orders(self):
        """The README repro.  A,B,C,D create their own file; E appends
        'E' to B, C and D.  Spec for any order: slot k holds the files created by the
        first k+1 commits, and a file that is B, C or D carries its 'E' line iff E is
        among those commits (E's change to a file is delivered by whichever commit
        creates it, or by E itself if the file already exists).

        Runs a seeded random sample of the 120 orders plus hand-picked corners.
        (Taking the first N of itertools.permutations would be a trap: it is
        lexicographic, so the first 24 never move anything in front of commit A.)
        Set GIT_BUBBLE_EXHAUSTIVE=1 to run all 120 (about a minute)."""
        base = build_letters(self.repo)
        orig = self.repo.out("rev-parse", "HEAD")
        before = self.repo.tree()
        names = list("ABCDE")
        everything = list(itertools.permutations(range(1, 6)))
        corners = [(5, 4, 3, 2, 1),    # full reversal
                   (5, 1, 2, 3, 4),    # E first: nothing to modify yet
                   (2, 3, 4, 5, 1),    # A (the oldest) moved to the tip
                   (1, 2, 5, 3, 4),    # the README example
                   (1, 5, 2, 3, 4),    # E right after A
                   (3, 4, 5, 1, 2)]    # a block move
        if os.environ.get("GIT_BUBBLE_EXHAUSTIVE"):
            orders = everything
        else:
            sample = random.Random(0).sample(everything, 14)
            orders = list(dict.fromkeys(corners + sample))
        for order in orders:
            with self.subTest(order=order):
                self.repo.git("reset", "-q", "--hard", orig)
                res = self.repo.run_tool(base, "--order", ",".join(map(str, order)), "--auto-squash-right")
                self.assert_reordered(base, [names[i - 1] for i in order], before, res)
                expected = []
                for k in range(5):
                    window = [names[i - 1] for i in order[:k + 1]]
                    expected.append({f: f"{f}\n" + ("E\n" if "E" in window and f in "BCD" else "")
                                     for f in window if f != "E"})
                self.assert_chain(base, expected)


class TestMetadata(RepoCase):
    def test_authors_dates_and_messages_survive(self):
        base = self.repo.commit("base")
        people = [("Ann", "ann@example.com", "2020-01-01T10:00:00+0000"),
                  ("Bob", "bob@example.com", "2021-02-03T11:30:00+0000"),
                  ("Cy", "cy@example.com", "2022-05-06T12:45:00+0000")]
        for i, (n, e, d) in enumerate(people):
            self.repo.commit(f"commit {n}\n\nbody line {i}\nsecond line", {f"f{i}": f"{i}\n"},
                             author=(n, e), date=d)
        fmt = "%an|%ae|%ad|%B"
        def facts():
            return {s: self.repo.out("log", "-1", "--date=raw", f"--format={fmt}", c)
                    for s, c in zip(self.repo.subjects(base), self.repo.commits(base))}
        want = facts()
        res = self.repo.run_tool(base, "--order", "3,1,2", "--auto-squash-right")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(facts(), want)


class TestEditorFlow(RepoCase):
    def test_reorder_via_sequence_editor(self):
        base = build_letters(self.repo)
        before = self.repo.tree()
        script = Path(self.repo.path.parent) / f"rev_todo_{os.getpid()}.py"
        script.write_text(
            "import sys\n"
            "lines = open(sys.argv[1]).read().splitlines()\n"
            "picks = [l for l in lines if l.startswith('pick ')]\n"
            "open(sys.argv[1], 'w').write('\\n'.join(reversed(picks)) + '\\n')\n")
        self.addCleanup(script.unlink)
        editor = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
        res = self.repo.run_tool(base, "--auto-squash-right", env={"GIT_SEQUENCE_EDITOR": editor})
        self.assert_reordered(base, list("EDCBA"), before, res)


class TestGuardRails(RepoCase):
    def test_identity_order_is_a_noop(self):
        base = build_letters(self.repo)
        head = self.repo.out("rev-parse", "HEAD")
        res = self.repo.run_tool(base, "--order", "1,2,3,4,5", "--auto-squash-right")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("nothing to do", res.stdout)
        self.assertEqual(self.repo.out("rev-parse", "HEAD"), head)

    def test_refuses_a_dirty_working_tree(self):
        base = build_letters(self.repo)
        head = self.repo.out("rev-parse", "HEAD")
        (self.repo.path / "A").write_text("uncommitted\n")
        res = self.repo.run_tool(base, "--order", "5,4,3,2,1", "--auto-squash-right")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("uncommitted", (res.stdout + res.stderr).lower())
        self.assertNotIn("Traceback", res.stderr)
        self.assertEqual(self.repo.out("rev-parse", "HEAD"), head)
        self.assertEqual((self.repo.path / "A").read_text(), "uncommitted\n")

    def test_bad_order_argument(self):
        base = build_letters(self.repo)
        head = self.repo.out("rev-parse", "HEAD")
        for bad in ("1,2,3", "1,1,2,3,4", "0,1,2,3,4", "1,2,3,4,6"):
            with self.subTest(order=bad):
                res = self.repo.run_tool(base, "--order", bad, "--auto-squash-right")
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("permutation", (res.stdout + res.stderr).lower())
                self.assertNotIn("Traceback", res.stderr)
                self.assertEqual(self.repo.out("rev-parse", "HEAD"), head)

    def test_detached_head_stays_detached(self):
        base = build_letters(self.repo)
        self.repo.git("checkout", "-q", "--detach")
        before = self.repo.tree()
        res = self.repo.run_tool(base, "--order", "1,2,5,3,4", "--auto-squash-right")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.repo.tree("HEAD"), before)
        self.assertEqual(self.repo.git("symbolic-ref", "-q", "HEAD", check=False).returncode, 1)
        self.assertEqual(self.repo.subjects(base), list("ABECD"))


class TestMerges(RepoCase):
    def build_side_merge(self):
        """0 -- base -- A -- M(merge side) -- B -- C ;  side: 0 -- S (forks BEFORE base)."""
        r = self.repo
        r.commit("0")
        r.git("checkout", "-q", "-b", "side")
        side = r.commit("S", {"S": "S\n"})
        r.git("checkout", "-q", "main")
        base = r.commit("base")
        r.commit("A", {"A": "A\n"})
        r.git("merge", "-q", "--no-ff", "-m", "merge side", "side")
        r.commit("B", {"B": "B\n"})
        r.commit("C", {"C": "C\n"})
        return base, side

    def build_second_parent_merge(self):
        r"""
             0 -- base -- A  (feat)          M = merge(S, A): line runs through parent 2
              \                 \
               S (main) ---------M -- B -- C
        """
        r = self.repo
        r.commit("0")
        r.git("checkout", "-q", "-b", "feat")
        base = r.commit("base")
        r.commit("A", {"A": "A\n"})
        r.git("checkout", "-q", "main")
        side = r.commit("S", {"S": "S\n"})
        r.git("merge", "-q", "--no-ff", "-m", "merge feat", "feat")
        r.commit("B", {"B": "B\n"})
        r.commit("C", {"C": "C\n"})
        return base, side

    def build_diamond(self):
        r"""
        0 -- A ------- R1 ----- M -- C        M = merge(R1, L1)
              \              /
               `---- L1 ----'
        Default line: A, R1, M, C (first parents); L1 is a side commit forked at A.
        With --via L1 the line is A, L1, M, C and R1 is the side commit.
        Returns (base, l1, before_tree, head)."""
        r = self.repo
        base = r.commit("0")
        r.commit("A", {"A": "A\n"})
        r.git("checkout", "-q", "-b", "side")
        l1 = r.commit("L1", {"L1": "L\n"})
        r.git("checkout", "-q", "main")
        r.commit("R1", {"R1": "R\n"})
        r.git("merge", "-q", "--no-ff", "-m", "merge side", "side")
        r.commit("C", {"C": "C\n"})
        return base, l1, r.tree(), r.out("rev-parse", "HEAD")

    def line(self, base):
        """The base->tip line, oldest first.  NOT --first-parent: after the swap a
        merge's first parent may be the side branch, not the line."""
        r = self.repo
        shas = r.out("rev-list", "--ancestry-path", "--topo-order", "--reverse",
                     f"{base}..HEAD").split()
        return shas, [r.out("log", "-1", "--format=%s", s) for s in shas]

    def test_move_merge_back_Y_is_merge(self):
        r = self.repo
        base, side = self.build_side_merge()
        before = r.tree()
        res = r.run_tool(base, "--order", "2,1,3,4", "--auto-squash-right")
        self.check_common(res, before)
        shas, subs = self.line(base)
        self.assertEqual(subs, ["merge side", "A", "B", "C"])
        # Y' keeps its side parent, in the same slot, with P (base) in the line slot.
        self.assertEqual(self.parents(shas[0]), [base, side])
        self.assertEqual(r.snapshot(shas[0]), {"S": "S\n"})            # merge brought in S only
        self.assertEqual(r.snapshot(shas[1]), {"A": "A\n", "S": "S\n"})  # X' = old M snapshot

    def test_move_merge_forward_X_is_merge(self):
        r = self.repo
        base, side = self.build_side_merge()
        before = r.tree()
        res = r.run_tool(base, "--order", "1,3,2,4", "--auto-squash-right")        # B jumps in front of the merge
        self.check_common(res, before)
        shas, subs = self.line(base)
        self.assertEqual(subs, ["A", "B", "merge side", "C"])
        # X' (the merge) now sits on Y' (B') in the line slot and still has `side`.
        self.assertEqual(self.parents(shas[2]), [shas[1], side])
        self.assertEqual(r.snapshot(shas[1]), {"A": "A\n", "B": "B\n"})              # B' has no S yet
        self.assertEqual(r.snapshot(shas[2]), {"A": "A\n", "B": "B\n", "S": "S\n"})  # merge delivers S

    def test_second_parent_move_merge_back(self):
        r = self.repo
        base, side = self.build_second_parent_merge()
        before = r.tree()
        res = r.run_tool(base, "--order", "2,1,3,4", "--auto-squash-right")
        self.check_common(res, before)
        shas, subs = self.line(base)
        self.assertEqual(subs, ["merge feat", "A", "B", "C"])
        # Y' keeps side in slot 1 and puts P (base) in slot 2: order preserved.
        self.assertEqual(self.parents(shas[0]), [side, base])
        # -m 2 diffs against A, so the merge contributes only S.  With -m 1 it would add A.
        self.assertEqual(r.snapshot(shas[0]), {"S": "S\n"})
        self.assertEqual(r.snapshot(shas[1]), {"A": "A\n", "S": "S\n"})
        self.assertEqual(self.parents(shas[1]), [shas[0]])

    def test_second_parent_move_merge_forward(self):
        r = self.repo
        base, side = self.build_second_parent_merge()
        before = r.tree()
        res = r.run_tool(base, "--order", "1,3,2,4", "--auto-squash-right")  # B jumps in front of the merge
        self.check_common(res, before)
        shas, subs = self.line(base)
        self.assertEqual(subs, ["A", "B", "merge feat", "C"])
        # X' keeps side in slot 1; only slot 2 (the line) is replaced by B'.
        self.assertEqual(self.parents(shas[2]), [side, shas[1]])
        self.assertEqual(r.snapshot(shas[1]), {"A": "A\n", "B": "B\n"})
        self.assertEqual(r.snapshot(shas[2]), {"A": "A\n", "B": "B\n", "S": "S\n"})

    # ---- diamonds: a side branch forked AFTER base and merged back

    def test_diamond_default_line_swap_merge_and_C(self):
        """No --via: line is A, R1, M, C.  L1 is a side commit forked at A."""
        r = self.repo
        base, l1, before, head = self.build_diamond()
        res = r.run_tool(base, "--order", "1,2,4,3", "--auto-squash-right")         # C in front of M
        self.check_common(res, before)
        self.assertEqual(r.subject("HEAD"), "merge side")
        c2, side = self.parents("HEAD")                                # chain slot first, side second
        self.assertEqual(r.subject(c2), "C")
        self.assertEqual(r.subject(side), "L1")
        r1 = self.parents(c2)[0]
        self.assertEqual(r.subject(r1), "R1")
        self.assertEqual(self.parents(side), self.parents(r1), "L1 and R1 share the rebuilt fork point A")
        self.assertEqual(r.snapshot(c2), {"A": "A\n", "R1": "R\n", "C": "C\n"})        # no L1 yet
        self.assertEqual(r.snapshot("HEAD"), {"A": "A\n", "L1": "L\n", "R1": "R\n", "C": "C\n"})

    def test_diamond_default_line_refuses_fork_past_merge(self):
        r = self.repo
        base, l1, before, head = self.build_diamond()
        res = r.run_tool(base, "--order", "2,3,1,4", "--auto-squash-right")          # A after M
        self.assert_refused(res, head)

    def test_diamond_via_swap_merge_and_C(self):
        """--via L1: line is A, L1, M, C and R1 is the side commit (in slot 0 of M)."""
        r = self.repo
        base, l1, before, head = self.build_diamond()
        res = r.run_tool(base, "--via", l1, "--order", "1,2,4,3", "--auto-squash-right")
        self.check_common(res, before)
        self.assertEqual(r.subject("HEAD"), "merge side")
        side, c2 = self.parents("HEAD")                                # side slot first, chain slot second
        self.assertEqual(r.subject(side), "R1")
        self.assertEqual(r.subject(c2), "C")
        l1_new = self.parents(c2)[0]
        self.assertEqual(r.subject(l1_new), "L1")
        self.assertEqual(self.parents(side), self.parents(l1_new), "R1 and L1 share the rebuilt fork point A")
        self.assertEqual(r.snapshot(c2), {"A": "A\n", "L1": "L\n", "C": "C\n"})        # no R1 yet
        self.assertEqual(r.snapshot("HEAD"), {"A": "A\n", "L1": "L\n", "R1": "R\n", "C": "C\n"})

    def test_diamond_via_move_L1_before_A_replays_the_other_leg(self):
        """A' now sits on L1', so its snapshot changed and R1 has to be replayed onto it."""
        r = self.repo
        base, l1, before, head = self.build_diamond()
        res = r.run_tool(base, "--via", l1, "--order", "2,1,3,4", "--auto-squash-right")
        self.check_common(res, before)
        self.assertIn("1 side commit(s) replayed", res.stdout)
        merge = self.parents("HEAD")[0]
        side, a_new = self.parents(merge)
        self.assertEqual(r.subject(side), "R1")
        self.assertEqual(r.subject(a_new), "A")
        self.assertEqual(self.parents(side), [a_new], "R1' sits on the rebuilt A'")
        self.assertEqual(r.snapshot(side), {"A": "A\n", "L1": "L\n", "R1": "R\n"})

    def test_diamond_via_refuses_fork_past_merge(self):
        r = self.repo
        base, l1, before, head = self.build_diamond()
        res = r.run_tool(base, "--via", l1, "--order", "2,3,1,4", "--auto-squash-right")   # A after M
        self.assert_refused(res, head)

    # ---- empty sides: an extra parent that is already on the line

    def build_no_ff_ahead(self):
        r"""
        0 -- A ------- M -- C        M = merge(A, B), a --no-ff merge of a branch strictly ahead of A
              \       /
               `-- B -'
        Default line: A, M, C with B as a side commit.  With --via B the line is A, B, M, C
        and A is ALSO M's first parent: an empty side (a branch that merges back with
        no commits between the fork point and the merge).
        Returns (base, b, before_tree, head)."""
        r = self.repo
        base = r.commit("0")
        r.commit("A", {"A": "A\n"})
        r.git("checkout", "-q", "-b", "feat")
        b = r.commit("B", {"B": "B\n"})
        r.git("checkout", "-q", "main")
        r.git("merge", "-q", "--no-ff", "-m", "merge feat", "feat")
        r.commit("C", {"C": "C\n"})
        return base, b, r.tree(), r.out("rev-parse", "HEAD")

    def test_no_ff_via_swap_merge_and_C(self):
        r = self.repo
        base, b, before, head = self.build_no_ff_ahead()
        res = r.run_tool(base, "--via", b, "--order", "1,2,4,3", "--auto-squash-right")
        self.check_common(res, before)
        self.assertEqual(r.subject("HEAD"), "merge feat")
        a_new, c2 = self.parents("HEAD")                               # the empty side first, chain slot second
        self.assertEqual(r.subject(a_new), "A")
        self.assertEqual(r.subject(c2), "C")
        b_new = self.parents(c2)[0]
        self.assertEqual(r.subject(b_new), "B")
        self.assertEqual(self.parents(b_new), [a_new], "the empty side is the REBUILT A, same as B's parent")
        self.assertEqual(r.snapshot(c2), {"A": "A\n", "B": "B\n", "C": "C\n"})

    def test_no_ff_via_refuses_the_empty_sides_fork_point_past_the_merge(self):
        r = self.repo
        base, b, before, head = self.build_no_ff_ahead()
        res = r.run_tool(base, "--via", b, "--order", "2,3,1,4", "--auto-squash-right")     # A after M
        self.assert_refused(res, head)

    def test_no_ff_default_line_treats_the_branch_as_a_side(self):
        """Without --via the same history has line A, M, C and B is a side commit."""
        r = self.repo
        base, b, before, head = self.build_no_ff_ahead()
        res = r.run_tool(base, "--order", "1,3,2", "--auto-squash-right")           # C in front of M
        self.check_common(res, before)
        c2, side = self.parents("HEAD")
        self.assertEqual((r.subject(c2), r.subject(side)), ("C", "B"))

    def test_merge_of_a_branch_that_never_forked(self):
        """M's extra parent A is an ancestor of its line parent B: git merge refuses to make
        this, but commit-tree can.  It's an empty side from the start."""
        r = self.repo
        base = r.commit("0")
        a = r.commit("A", {"A": "A\n"})
        b = r.commit("B", {"B": "B\n"})
        m = r.out("commit-tree", r.tree(), "-p", b, "-p", a, "-m", "odd merge")
        r.git("reset", "-q", "--hard", m)
        r.commit("C", {"C": "C\n"})
        before, head = r.tree(), r.out("rev-parse", "HEAD")

        # C in front of M: A is still before the merge, so this is fine.
        res = r.run_tool(base, "--order", "1,2,4,3", "--auto-squash-right")
        self.check_common(res, before)
        self.assertEqual(r.subject("HEAD"), "odd merge")
        c2, a_new = self.parents("HEAD")
        self.assertEqual((r.subject(c2), r.subject(a_new)), ("C", "A"))
        b_new = self.parents(c2)[0]
        self.assertEqual(self.parents(b_new), [a_new], "the extra parent is the REBUILT A")

        # A after the merge that names it as a parent: refused, nothing moves.
        r.git("reset", "-q", "--hard", head)
        res = r.run_tool(base, "--order", "2,3,1,4", "--auto-squash-right")
        self.assert_refused(res, head)


class TestForkAttach(RepoCase):
    """A side branch stays attached to the line commit it forked from, BY NAME."""

    def test_commit_pushed_before_fork_enters_the_side_branch(self):
        r = self.repo
        base = self.build_fork()
        before = r.tree()
        n = r.count(base)
        self.run_ok(base, before, "--order", "1,3,2,4,5,6", "--auto-squash-right")      # E before F
        self.assertEqual(r.line_subjects(base), ["A", "E", "F", "G", "M", "H"])
        by = r.by_subject(base)
        self.assertEqual(r.count(base), n, "no commit may be duplicated or lost")
        s1, f = by["S1"], by["F"]
        self.assertEqual(r.parents(s1), [f], "side branch must stay attached to F (by name)")
        self.assertEqual(set(r.snapshot(s1)), {"cfg", "A", "e", "F", "s1"}, "side now contains E")
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
        self.run_ok(base, before, "--order", "1,3,2,4,5", "--auto-squash-right")        # E after F
        by = r.by_subject(base)
        self.assertEqual(set(r.snapshot(by["S1"])), {"A", "F", "s1"}, "E must be gone from the side")
        self.assertEqual(set(r.snapshot(by["M"])), {"A", "e", "F", "g", "s1"}, "...and back at the merge")

    def test_unchanged_fork_snapshot_means_reparent_only(self):
        """Swapping two commits BEFORE the fork leaves F's snapshot alone."""
        r = self.repo
        base = r.commit("0")
        r.commit("A", {"A": "A\n"})
        r.commit("B", {"B": "B\n"})
        r.commit("F", {"F": "F\n"})
        r.branch("side")
        r.commit("S1", {"s1": "s1\n"})
        s1_tree = r.tree()
        r.checkout("main")
        r.commit("G", {"g": "G\n"})
        r.merge("side", "M")
        before = r.tree()
        res = self.run_ok(base, before, "--order", "2,1,3,4,5", "--auto-squash-right")
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
        self.run_ok(base, before, "--order", "2,1,3,4", "--auto-squash-right")
        self.assertEqual(r.by_subject(base)["S1"], s1, "side commit must keep its hash")
        self.assertEqual(r.parents(r.by_subject(base)["M"])[1], s1)


class TestMergesOnTheLine(RepoCase):
    def test_move_after_merge_commit_before_it(self):
        r = self.repo
        base = self.build_fork()
        before = r.tree()
        self.run_ok(base, before, "--order", "1,2,3,4,6,5", "--auto-squash-right")     # H before M
        self.assertEqual(r.line_subjects(base), ["A", "F", "E", "G", "H", "M"])
        m = r.by_subject(base)["M"]
        self.assertEqual(len(r.parents(m)), 2)
        self.assertEqual(r.parents(m)[1], r.by_subject(base)["S1"])

    def test_move_commit_across_merge_and_fork(self):
        r = self.repo
        base = self.build_fork()
        before = r.tree()
        self.run_ok(base, before, "--order", "6,1,2,3,4,5", "--auto-squash-right")     # H first
        by = r.by_subject(base)
        self.assertEqual(r.line_subjects(base), ["H", "A", "F", "E", "G", "M"])
        self.assertIn("h", r.snapshot(by["S1"]), "H is now below the fork, so the side has it")

    def test_merge_commit_moved_earlier_but_not_past_its_fork(self):
        r = self.repo
        base = self.build_fork()
        before = r.tree()
        self.run_ok(base, before, "--order", "1,2,5,3,4,6", "--auto-squash-right")    # M before E and G
        self.assertEqual(r.line_subjects(base), ["A", "F", "M", "E", "G", "H"])

    def test_fork_past_its_merge_is_refused_and_nothing_moves(self):
        r = self.repo
        base = self.build_fork()
        head, tree = r.out("rev-parse", "HEAD"), r.tree()
        res = r.run_tool(base, "--order", "1,3,4,5,2,6", "--auto-squash-right")   # F after M
        self.assert_refused(res, head)
        self.assertEqual(r.tree(), tree)

    def test_merge_moved_before_its_fork_is_refused(self):
        r = self.repo
        base = self.build_fork()
        head = r.out("rev-parse", "HEAD")
        res = r.run_tool(base, "--order", "1,5,2,3,4,6", "--auto-squash-right")   # M before F
        self.assert_refused(res, head)


class TestReplayConflicts(RepoCase):
    """A side commit that doesn't fit on its new fork point."""

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
        self.run_ok(base, before, "--order", "1,3,2,4,5", "--auto")     # E after F
        by = r.by_subject(base)
        self.assertEqual(r.snapshot(by["S1"])["cfg"], "x\n", "S1's hunk needs E, so it is dropped")
        self.assertEqual(r.snapshot(by["M"])["cfg"], "x\nE\nS\n", "the merge brings it back")

    def test_noninteractive_conflict_aborts_and_restores_everything(self):
        r = self.repo
        base = self.build()
        head, tree = r.out("rev-parse", "HEAD"), r.tree()
        res = r.run_tool(base, "--order", "1,3,2,4,5")
        self.assert_refused(res, head, needle="terminal")
        self.assertEqual(r.tree(), tree)
        self.assertEqual(r.out("symbolic-ref", "--short", "HEAD"), "main")


class TestOffLineMerge(RepoCase):
    """F forks two branches that merge into EACH OTHER before joining the line:
         side:  S1 - S2 --.
         other: T1 -------+- U ---.
       line:  A B F E G ---------- M H"""

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
        want = set(r.snapshot(f)) | {"s1", "s2", "t1"}
        self.assertEqual(set(r.snapshot(by["U"])), want)

    def test_push_E_before_the_fork(self):
        r = self.repo
        base = self.build()
        before, n = r.tree(), r.count(base)
        self.run_ok(base, before, "--order", "1,2,4,3,5,6,7", "--auto-squash-right")
        self.check_structure(base, n)
        self.assertIn("e", r.snapshot(r.by_subject(base)["U"]))

    def test_move_everything_around(self):
        r = self.repo
        base = self.build()
        before, n = r.tree(), r.count(base)
        self.run_ok(base, before, "--order", "7,2,1,3,5,4,6", "--auto-squash-right")
        self.check_structure(base, n)

    def disabled_test_random_orders(self):
        for seed in range(7):
            with self.subTest(seed=seed):
                self.setUp()
                r = self.repo
                base = self.build()
                before, n, head = r.tree(), r.count(base), r.out("rev-parse", "HEAD")
                names = r.line_subjects(base)
                order = list(range(1, len(names) + 1))
                random.Random(seed).shuffle(order)
                res = r.run_tool(base, "--order", ",".join(map(str, order)), "--auto-squash-right")
                pos = {names[i - 1]: k for k, i in enumerate(order)}
                if pos["F"] >= pos["M"]:
                    self.assert_refused(res, head)
                    continue
                self.check_common(res, before)
                self.assertEqual(r.line_subjects(base), [names[i - 1] for i in order])
                self.check_structure(base, n)


if __name__ == "__main__":
    unittest.main(verbosity=2)