#!/usr/bin/env python3
r"""
git-rebubble: reorder commits with "invariant swaps" instead of rebase replay.

# Copyright D.S. Leonard
# License: MIT, attribution required
#
# This is my original work.
#
# A git re-ordering rebase that preserves the tip snapshot
# And generally makes conflict resolution more transparent and intuitive.

A swap of CHAANGES (patches) turns   P -> X -> Y   into   P -> Y' -> X'   where

    X'.tree == Y.tree          the snapshot after the pair never changes
    Y'.tree == P + patch(Y)    Y applied onto P; parts that need X don't apply

    This is NOT actually an assymetric operation, although it reads that way.  We are replacing X with Y'

           Local(ours)
           P
         /  \
  Base  X    Y'
         \  /
          Y
         Remote (theirs)

    Neither the contents of the first nor last commit actually change, only what's in the middle,
    does and thus the ORDER of the CHANGES to get from P to Y.  We describe commit messages by what
    changes, not by content.

Because the endpoint is fixed, once the diff Y'-P  is determined, the diff
X'-Y' also is.   A general reorder is a plain bubble sort using only that
swap operation, sweeping from the tip back toward the tail, so each commit
that must move bubbles back through its predecessors one swap at a time.

Squashing
---------
Lines marked 'squash' or 'fixup' in the todo fold into the line commit above them,
after  reordering.  You cannot squash onto a fork.

Forks, merges and diamonds
--------------------------
The "line" is one path of commits from BASE to HEAD (first parents by default;
--via COMMIT routes it through COMMIT).  Every other commit in BASE..HEAD that
descends from BASE is a "side" commit.  Phase 1 bubble-sorts the line, which
never looks at side commits.  Phase 2 rebuilds the line commits in their new
order, and whenever a line merge needs its side parents, replays the side
commits it consumes:

  * By default a side branch stays attached to the line commit it forked from, BY NAME.
  * Side commits are replayed with an ordinary cherry-pick onto their remade
    parent.   Hunks that don't fit are dropped (--auto) or resolved by you,
    exactly like a swap.
  * After the bubble sort and during the side branch replays, line merges
    keep their tree(contents).  The results of a merge should only depend on what features
    cam before (vs after) it, not what branch the feature is on.  This might shift   perspective on how
    content arivedd, from one branch, from both, or as a reconciliation, butt it doesn't change the result.
  * A fork point may not be moved after the merge that joins its branch back (loops are impossible in git).

  for usage syntax type:
  git rebubble -h

The branch ref only moves at the very end, so aborting is always safe.

All information needed for conflict resolution is available at each step.
"""
import argparse
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import cache, cached_property
from itertools import pairwise
from typing import NamedTuple, Any

from dsleonard_git_tools import *

# Setup useful verbosity controls for messaging and shell command execution via qx:
# see https://github.com/DSLeonard-coding/qx
from dsleonard_qx import *  # noqa so linters won't move this # type: ignore

# Names available
# from  'git_rebubble'.core import *
__all__ = [
    'git_rebubble',
]
# Set __export__all__ to True to elevate names in __all__ to package level,  ie now
# from 'git_rebubble' import *
# For single-class modules, this prevents my_class.MyClass() redundancy or manual __init__.py maintenance.
__export_all__ = True


# From bash, use export git_rebubble_DEBUG="True" to enable debug output


# Main entry point for project.
# Edit project.scripts in pyproject.toml if you rename it, or to change the cli name


class Fatal(Exception):
    pass


# ------------------------------------------------------------------ helpers

def git_str(cmd):
    """git() raises RuntimeError on failure (check=True); return stdout without trailing whitespace."""
    return git(cmd).rstripped


def short(sha):
    return sha[:8]


def subject_of(sha):
    return git(["log", "-1", "--format=%s", sha]).lines[0]


@cache
def tree_of(sha):
    return git_str(["rev-parse", f"{sha}^{{tree}}"])


def unmerged(pathspecs=None):
    """{path: {stage: oid}} for every conflicted path, optionally only those matching pathspecs."""
    found = defaultdict(dict)
    cmd = ["ls-files", "-u", "-z"]
    if pathspecs:
        cmd += ["--", *pathspecs]
    for rec in git(cmd).stdout.split("\0"):
        if rec:
            info, path = rec.split("\t", 1)
            _mode, oid, stage = info.split()
            found[path][int(stage)] = oid
    return dict(found)


def paths_differing_from_tree(tree, pathspecs):
    """Paths matching pathspecs whose staged content differs from `tree`, plus conflicted ones."""
    listed = git(["diff", "--cached", "--name-only", "-z", "--no-renames",
                  tree, "--", *pathspecs]).stdout.split("\0")
    return {path for path in listed if path} | set(unmerged(pathspecs))


def restore_paths_from_tree(tree, paths):
    """Make each path in the index and working tree identical to its version in `tree`
    (removed if `tree` lacks it).  This also resolves any conflict on the path."""
    for path in sorted(paths):
        if git(["cat-file", "-e", f"{tree}:{path}"], check=False):
            git(["checkout", tree, "--", path])
        else:
            git(["rm", "-q", "-f", "--", path])


def merge_hunks(path, stages, policy):
    """Resolve content conflicts in `path` hunk by hunk (like -X ours/theirs), keeping
    non-conflicting merges.  Returns False if it can't (missing stage, binary, error)."""
    if not {1, 2, 3} <= stages.keys():
        return False
    with tempfile.TemporaryDirectory() as tmp_dir:
        temp_paths = {}
        for stage, name in ((2, "ours"), (1, "base"), (3, "theirs")):
            temp_paths[name] = os.path.join(tmp_dir, name)
            blob = git(["cat-file", "blob", stages[stage]], check=False)
            if blob.returncode:
                return False
            with open(temp_paths[name], "wb") as f:
                f.write(blob.stdout)
        # merge-file rewrites its first file; --ours/--theirs settles each conflicting hunk
        merged = git(["merge-file", f"--{policy}",
                                 temp_paths["ours"], temp_paths["base"], temp_paths["theirs"]],
                                check=False)
        if merged.returncode != 0:
            return False
        with open(temp_paths["ours"], "rb") as src, open(path, "wb") as dst:
            dst.write(src.read())
    git(["add", "--", path])
    return True

def abort_flag():
    return os.path.join(git_str("rev-parse --absolute-git-dir"), "rebubble-abort")


def run_shell(flag):
    """Run $SHELL until it exits, or until `git-rebubble --abort` drops the flag,
    in which case hang the shell up ourselves."""
    env = dict(os.environ, REBUBBLE_ACTIVE=git_str("rev-parse --absolute-git-dir"))
    # make sure `git rebubble --abort` resolves to THIS install, even from a venv
    env["PATH"] = os.path.dirname(os.path.abspath(sys.argv[0])) + os.pathsep + env.get("PATH", "")
    p = subprocess.Popen(shlex.split(os.environ.get("SHELL", "bash")), env=env)
    try:
        while p.poll() is None:
            if os.path.exists(flag):
                p.send_signal(signal.SIGHUP)
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
                break
            time.sleep(0.1)
    except BaseException:
        p.kill()
        p.wait()
        raise

def cherry_pick_tree(onto_sha, commit, x_option, settle):
    """cherry-pick -n `commit` onto `onto_sha`, then call settle() so the caller can
    adjust or resolve the staged result.  Returns the resulting tree.
    A merge is replayed relative to its chain parent.

    :param onto_sha: sha to check out and apply the commit's patch onto.
    :param commit: Commit whose current_sha is picked.
    :param x_option: "ours", "theirs" or None; passed to git as -X.
    :param settle:  callable, always called after the pick. On return, no conflicts may remain.
    :returns: sha of the tree that results.
    """
    git(["checkout", "-q", "--detach", onto_sha])
    try:
        flags = ["-X", x_option] if x_option else []
        if commit.is_merge:
            flags += ["-m", str(commit.chain_parent_idx + 1)]
        picked = git(["cherry-pick", "-n", *flags, commit.current_sha], check=False)
        if not picked and not unmerged():
            raise Fatal(f"cherry-pick of {short(commit.original_sha)} failed:\n{picked.stderr.strip()}")
        settle()
        return git_str("write-tree")
    finally:
        git("cherry-pick --quit", check=False)
        git("reset -q --hard", check=False)

# ------------------------------------------------------------------ commits

class Commit:
    """A commit in the BASE..HEAD graph, which we can also rewrite.

    Identity and history are fixed at construction:
      original_sha            the original sha, the commit's name for its whole life
      original_parent_shas    its original parents, never changed
      chain_parent_idx        index (in the parents) of the chain parent: the previous commit
                              on the line.  Side commits use 0, so their first parent is
                              their chain parent.

    The rewritten state is what changes as we reorder:
      current_sha     sha of the current rewritten version (== original_sha until rewritten)
      parent_shas     current parents (chain slot rewired, side parents kept in place)
      tree            current tree (== original_tree until a rewrite supplies a new one)

    Metadata (original_tree, author, subject, message) is read from git on first use,
    so building the whole graph up front stays cheap."""

    def __init__(self, original_sha, parent_shas, chain_parent_idx=0):
        self.original_sha = original_sha
        self.original_parent_shas = tuple(parent_shas)
        self.chain_parent_idx = chain_parent_idx
        self.current_sha = original_sha
        self.parent_shas = list(parent_shas)
        self.rewritten_tree = None

    # --- what we know about the original commit
    @property
    def is_merge(self):
        return len(self.original_parent_shas) > 1

    @property
    def original_parent_sha(self):
        """The original chain parent."""
        return self.original_parent_shas[self.chain_parent_idx]

    @property
    def side_parent_shas(self):
        """Original parents other than the chain parent, in their original order."""
        return [p for k, p in enumerate(self.original_parent_shas) if k != self.chain_parent_idx]

    @property
    def parent_sha(self):
        """The current chain parent."""
        return self.parent_shas[self.chain_parent_idx]

    @cached_property
    def original_tree(self):
        """Tree of the commit as it was before any rewriting."""
        return tree_of(self.original_sha)

    @property
    def tree(self):
        """Tree of the current rewritten version."""
        return self.original_tree if self.rewritten_tree is None else self.rewritten_tree

    @cached_property
    def subject(self):
        return subject_of(self.original_sha)

    @property
    def label(self):
        return f"{short(self.original_sha)} {self.subject!r}"

    @cached_property
    def message(self):
        return git_str(["log", "-1", "--format=%B", self.original_sha]) + "\n"

    @cached_property
    def author_env(self):
        name, email, date = git(["log", "-1", "--date=raw", "--format=%an%n%ae%n%ad", self.original_sha]).lines[:3]
        return {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": date}

    # --- rewriting
    def remake(self, parents, tree=None):
        """New commit object with the given parents (and tree), same author and message."""
        self.parent_shas = list(parents)
        if tree:
            self.rewritten_tree = tree
        parent_args = [a for p in self.parent_shas for a in ("-p", p)]
        self.current_sha = git(["commit-tree", self.tree, *parent_args],
                               input=self.message, env=dict(os.environ, **self.author_env)).stdout.strip()

    def reparent(self, parent, tree=None):
        """Rewire only the chain slot; side parents and their order are kept."""
        parents = list(self.parent_shas)
        parents[self.chain_parent_idx] = parent
        self.remake(parents, tree)


# ------------------------------------------------------------------ structured types

class Edge(NamedTuple):
    """One parent link of a side commit."""
    child_sha: str
    parent_idx: int


# Where each side-commit parent link attaches: edge -> sha of the line commit it forks from.
ForkEdges = dict[Edge, str]


@dataclass(frozen=True)
class Graph:
    """The history BASE..TIP, split into one line and everything else.

    line_commits         [Commit, ...] oldest first
    side_commit_store    {sha: Commit} for every other commit that descends from BASE,
                         parents before children"""
    line_commits: list
    side_commit_store: dict

    @cached_property
    def line_shas(self):
        return {commit.original_sha for commit in self.line_commits}


class Order(NamedTuple):
    """What the user asked for: the new line order, where each fork attaches, and which
    line commits are squashed into the one before them (after reordering).

    squash_modes: {sha: "squash" | "fixup"} for each line commit folded into its predecessor.
    "fixup" drops that commit's message, "squash" appends it."""
    ordered_line_shas: list
    fork_edges: ForkEdges
    squash_modes: dict[str, str] | None = None

    @property
    def rank_by_sha(self):
        return {sha: n for n, sha in enumerate(self.ordered_line_shas)}

    @property
    def squash_groups(self):
        """[[sha, ...], ...] in the new order: each group is one pick plus the squashes folded into it."""
        modes = self.squash_modes or {}
        groups = []
        for sha in self.ordered_line_shas:
            if sha in modes:
                groups[-1].append(sha)
            else:
                groups.append([sha])
        return groups


@dataclass(frozen=True)
class Options:
    """swap_conflict_policy / branch_conflict_policy: "ours", "theirs", or None (= resolve by hand).
    auto_files: pathspecs; if set, the two policies only apply to matching conflicted files.
    force_left_files / force_right_files: pathspecs forced during swaps (see Resolver.force_swap_files)."""
    swap_conflict_policy: str | None = None
    branch_conflict_policy: str | None = None
    mergetool: bool = False
    tool: str | None = None
    auto_files: tuple[str, ...] | None = None
    force_left_files: tuple[str, ...] | None = None
    force_right_files: tuple[str, ...] | None = None


@dataclass
class Stats:
    swaps: int = 0
    replayed: int = 0  # side commits rebuilt by cherry-pick
    reparented: int = 0  # side commits that only needed new parents
    squashed: int = 0  # line commits folded into a predecessor


# class AncestryStore(dict[str, list[str]]):
#     pass

# ------------------------------------------------------------------ graph

def ancestry_sha_store(a, b):
    """
    :param a: base sha
    :param b: tip sha
    :returns: AncestryStore  for the commits in a..b that descend from a
    (git's --ancestry-path), parents before children.  `a` itself is not included."""
    parents: dict[str, list[str]] = {}
    for rec in git(["rev-list", "--parents", "--ancestry-path", "--topo-order",
                    "--reverse", f"{a}..{b}"]).lines:
        sha, *parent_shas = rec.split()
        parents[sha] = parent_shas
    return parents


def build_graph(base_sha, tip_sha, vias=()):
    """
    Split the history BASE..TIP into a line and side commits.


    Git does the work: --ancestry-path_commits keeps exactly the commits in BASE..TIP
    that descend from BASE.  Parents outside that set (history below BASE, side
    branches that forked before BASE) are never touched; they ride along as
    inert extra parents.

    The line follows first parents (among those descending from BASE).  With
    vias, the line is built in segments BASE..via1..via2..TIP, which is how you
    pick a route through a diamond.

    :param base_sha:  Sha of Commit before the first one(s) that can be reordered
    :param tip_sha: Sha of the present tip of the commit graph
    :param vias: Shas of way-points, directs line through side branches.
    :return:  Graph.  line_commits have chain_parent_idx set to the index (in their parents)
        of the previous commit on the line (BASE for the first).  side_commit_store is
        {sha: Commit} for every other commit in BASE..TIP that descends from BASE (side
        branches forked after BASE, merged back later), parents before children.
    """
    marks = [base_sha, *vias, tip_sha]
    # validate the line definition:
    for a, b in pairwise(marks):
        if a != b and not git(["merge-base", "--is-ancestor", a, b], check=False):
            raise Fatal(f"{short(a)} is not an ancestor of {short(b)}; "
                        f"--via commits must lie between BASE and HEAD, oldest first")

    all_shas_store = ancestry_sha_store(base_sha, tip_sha)

    # Build the main line we're reordering along, one segment per via
    line_commits = []
    for a, b in pairwise(marks):
        if a == b:
            continue
        sha_store = ancestry_sha_store(a, b)  # could contain multiple re-merged branches
        # start at the tip of the segment (b) and follow the chain parent back to a
        current_sha, path_commits = b, []
        while current_sha != a:
            parent_shas = sha_store[current_sha]
            # the chain parent is the first parent that is inside this segment
            # next here just gets the index of the FIRST match meeting the condition.
            chain_parent_idx = next(i for i, p in enumerate(parent_shas) if p == a or p in sha_store)
            path_commits.append(Commit(current_sha, parent_shas, chain_parent_idx))
            current_sha = parent_shas[chain_parent_idx]
        line_commits.extend(reversed(path_commits))
    line_shas = {c.original_sha for c in line_commits}
    side_commit_store = {sha: Commit(sha, all_shas_store[sha]) for sha in all_shas_store if sha not in line_shas}
    return Graph(line_commits, side_commit_store)


def side_ancestor_commits(side_commit_store, ref_shas):
    """
    Every side commit ancestor of `ref_shas` through side commits, parents before children.
    Result includes ref_shas

    :param side_commit_store: {sha: Commit} list of commits, nominally all re-merged side commits in a graph.
    :param ref_shas: list of sha to find the ancestors of
    :returns: list of commits of ancestors.
    """
    need = set()  # empty unordered collection
    stack = [r for r in ref_shas if r in side_commit_store]
    while stack:
        sha = stack.pop()
        if sha not in need:
            need.add(sha)
            # Find any original parent shas of sha that are in the side_commit_store
            # Add them to the stack to process.
            stack += [p for p in side_commit_store[sha].original_parent_shas if p in side_commit_store]
    return [s for sha, s in side_commit_store.items() if sha in need]


def default_fork_edges(graph):
    """Every side-commit parent link that lands on the line, attached where it originally was.

    :param graph: Graph
    :return: ForkEdges"""
    return {Edge(side_commit.original_sha, parent_idx): parent_sha
            for side_commit in graph.side_commit_store.values()
            for parent_idx, parent_sha in enumerate(side_commit.original_parent_shas)
            if parent_sha in graph.line_shas}


def fork_point_shas(commit, line_shas, side_commit_store, fork_edges):
    """
    Finds fork_points that ONLY descend to commit through side commits.  Does NOT find all fork points in history.

    :param commit: a commit to find fork-point ancestors of
    :param line_shas: The line we're trying to find forks from.
    :param side_commit_store: {sha: Commit}, nominally for all non-line commits
    :param fork_edges: ForkEdges, where each side-commit parent link attaches to the line.
    :return: sha of fork point
    """
    for side_parent_sha in commit.side_parent_shas:
        if side_parent_sha in line_shas:
            yield side_parent_sha
        for side_commit in side_ancestor_commits(side_commit_store, [side_parent_sha]):
            for parent_idx in range(len(side_commit.original_parent_shas)):
                fork_sha = fork_edges.get(Edge(side_commit.original_sha, parent_idx))
                if fork_sha is not None:
                    yield fork_sha


def validate_order(graph, order):
    """Every line commit that a remerged-side commit hangs off must come before the consuming merge.

    :param graph: Graph, as returned by build_graph.
    :param order: Order, the requested line order and fork attachments.

    Raises:
        Fatal: if a fork point would be moved past its merge.
    """
    rank_by_sha = order.rank_by_sha
    for commit in graph.line_commits:
        for fork in fork_point_shas(commit, graph.line_shas, graph.side_commit_store, order.fork_edges):
            if rank_by_sha[fork] >= rank_by_sha[commit.original_sha]:
                raise Fatal(f"fork point {short(fork)} {subject_of(fork)!r} would end up after the merge "
                            f"{commit.label} that joins its branch back in; "
                            f"a fork point can't be moved past its merge")


# --------------------------------------------------------------- conflict resolution

SWAP_GUIDE = """
   Swapping P->X->Y  to P->Y'->X'

   Conflicts building 
           Y: {y_label} 
        on top of 
           P: {p_short} {p_subject}
        without 
           X: {x_label}
        underneath it.
{merge_note}
 ############################################################################################
   YOUR JOB:  Build a new version of Y,  Y',  as it should look without   X   
   Do not worry too much about losing changes. Any parts you remove here will carry to X
 ############################################################################################

   Guide to git status:  base, ours, and theirs are permuted as in cherry-pick 
       Y, the thing we're rebuilding, is THEM.   
       P, the real base is US
       X, what we're actually trying to remove, is the reference/base
       Added by us     ->  Deleted by X.  But there's a conflict, so Y renamed or made a directory.
                           Probably keep (REMOTE PANE) THEIR (Y's) version (SQUASH-LEFT) or just ADD.
       Deleted by us   ->  Added by X, modified by Y.  Check if anything should be kept that Y needs without X!
                           You want to: git show :3:filename > filename &&  git difftool :1:filename filename
                           And then remove everything on the right that Y didn't add and doesn't need.
                           On a mergetool modify/delete choice, maybe ABORT.
       Added by them   ->  Added by Y (the REAL US) ADD, ie keep this stuff except bits that relate to X
       Deleted by them ->  Deleted by Y (the REAL US) We usually want to DELETE it. (SQUASH-LEFT)
       Both added      ->  X deleted and Y added.  You probably want to keep (ADD) this. (SQUASH-LEFT)
       Both deleted    ->  X added the file and Y deleted it.  You probably want to DELETE it. (SQUASH-LEFT)
       Both modified   ->  Modified by Y  and X.  Git will already remove things from X back to P where it can.
                           Prioritize the REMOTE PANE in mergetool or difftool.
    Generally we want to keep OUR changes, but PRIORITIZE THEIR changes
"""

SWAP_MERGE_NOTE = """   (Y is a merge commit: its changes are taken relative to its parent on the line,
    parent {y_chain_parent_number}, i.e. what the merge brought in.)
"""

REPLAY_GUIDE = """
   Replaying side-branch commit
           S: {s_label}
        onto
           P': {p_short} {p_subject!r}
        which replaces its old parent
           P:  {old_parent_short} {old_parent_subject!r}
        because the main line was reordered.

 ############################################################################################
   YOUR JOB:  Make S fit on P'.  Keep what makes sense, drop what doesn't.
   Anything you drop comes back at the merge that joins this branch to the line,
   because that merge's contents never change.
 ############################################################################################

   This is an ordinary cherry-pick:  US = P' (the new parent),  THEM = S,  base = P.

"""


class Resolver:
    """Everything that happens after a cherry-pick: forced files, automatic policy, or by hand."""

    def __init__(self, options):
        self.options = options

    def x_option_for(self, policy):
        """The -X value to hand to git.  git's -X can't be limited by path, so when
        --auto-files is set we apply the policy ourselves, per file, in auto_resolve."""
        return None if self.options.auto_files else policy

    @staticmethod
    def y_only_lines(x_bytes, y_bytes):
        """Lines of Y that are inserted or changed relative to X (Y's own contribution)."""
        x_lines = x_bytes.splitlines(keepends=True)
        y_lines = y_bytes.splitlines(keepends=True)
        kept = []
        for tag, _i1, _i2, j1, j2 in SequenceMatcher(None, x_lines, y_lines, autojunk=False).get_opcodes():
            if tag in ("insert", "replace"):
                kept += y_lines[j1:j2]
        return b"".join(kept)

    @staticmethod
    def _tree_file_bytes(treeish, path: str) -> bytes | None:
        res = subprocess.run(
            ["git", "cat-file", "-p", f"{treeish}:{path}"],
            capture_output=True,
        )
        return res.stdout if res.returncode == 0 else None


    def settle_swap(self, P, X, Y):
        """Building Y' (Y applied onto P, witheout X)."""
        self.force_swap_files(P, Y)
        if unmerged() and self.options.swap_conflict_policy:
            self.auto_resolve(self.options.swap_conflict_policy)
        if unmerged():
            merge_note = ""
            if Y.is_merge:
                merge_note = SWAP_MERGE_NOTE.format(y_chain_parent_number=Y.chain_parent_idx + 1)
            print(SWAP_GUIDE.format(y_label=Y.label, p_short=short(P), p_subject=subject_of(P),
                                    x_label=X.label, merge_note=merge_note))
            self.resolve_by_hand()

    def settle_replay(self, P, S):
        """Replaying a side-branch commit onto a rebuilt parent."""
        if unmerged() and self.options.branch_conflict_policy:
            self.auto_resolve(self.options.branch_conflict_policy)
        if unmerged():
            old_parent = S.original_parent_sha
            print(REPLAY_GUIDE.format(s_label=S.label, p_short=short(P), p_subject=subject_of(P),
                                      old_parent_short=short(old_parent),
                                      old_parent_subject=subject_of(old_parent)))
            self.resolve_by_hand()

    def force_swap_files(self, P, Y):
        """Swap only, runs whether or not git reported a conflict.

        left:  Y' takes Y  version matching files.
        right: Y' takes P  version of matching files.
        A file matching both lists ends up right-forced."""
        if self.options.force_left_files:
            left_paths = paths_differing_from_tree(Y.tree, self.options.force_left_files)
            restore_paths_from_tree(Y.tree, left_paths)
            if left_paths:
                print(f"   force-left: {len(left_paths)} file(s) taken from {Y.label}")
        if self.options.force_right_files:
            right_paths = paths_differing_from_tree(tree_of(P), self.options.force_right_files)
            restore_paths_from_tree(tree_of(P), right_paths)
            if right_paths:
                print(f"   force-right: {len(right_paths)} file(s) kept as in {short(P)}")

    def resolve_by_hand(self):
        for path in sorted(unmerged()):
            print("     U", path)

        if self.options.mergetool or self.options.tool:
            cmd = ["git", "mergetool"]
            if self.options.tool:
                cmd.append(f"--tool={self.options.tool}")
            subprocess.run(cmd)

        if not sys.stdin.isatty():
            raise Fatal("conflicts need resolving but stdin is not a terminal (use --auto)")
        flag = abort_flag()
        while unmerged():
            if os.path.exists(flag):
                os.unlink(flag)  # stale
            print(
                f"   1)  Resolve conflicts\n"
                f"   2)  git add   -> stage all changes.  Committing will have no additional effect.\n"
                f"   3)  Type  exit  to continue,  or  git rebubble --abort  to abort\n"
            )
            run_shell(flag)  # exit status deliberately ignored

            if os.path.exists(flag):
                os.unlink(flag)
                git("cherry-pick --abort", check=False)
                raise Fatal("aborted by user")

            if unmerged():
                ans = input("\n    [!] Conflicts are still unmerged. [r]etry or [a]bort? [r]: ").strip().lower()
                if ans.startswith("a"):
                    git("cherry-pick --abort", check=False)
                    raise Fatal("aborted by user")

    def auto_resolve(self, conflict_policy):
        """Settle conflicts by taking one side, only in --auto-files if given.
        Hunk-level where possible; otherwise the whole side, and a path that side lacks is removed."""
        keep_stage = 3 if conflict_policy == "theirs" else 2
        targets = unmerged(self.options.auto_files)
        for path, stages in targets.items():
            if merge_hunks(path, stages, conflict_policy):
                continue
            if keep_stage in stages:
                git(["checkout", f"--{conflict_policy}", "--", path])
                git(["add", "--", path])
            else:
                git(["rm", "-q", "-f", "--", path])
        left_for_user = len(unmerged())
        print(f'   {len(targets)} conflicted file(s) auto-resolved (kept the '
              f'{"earlier" if conflict_policy == "ours" else "later"} side, dropped what didn\'t fit)'
              + (f"; {left_for_user} left for you" if left_for_user else ""))


# --------------------------------------------------------------- the swap

class Bubble:
    def __init__(self, base, graph, order, resolver):
        """
        :param base: sha the first line commit is rebuilt on.
        :param graph: Graph from build_graph.
        :param order: Order, the requested line order and fork attachments.
        :param resolver: Resolver, consulted whenever a cherry-pick conflicts.
        """
        self.base = base
        self.graph = graph
        self.order = order
        self.resolver = resolver
        self.chain_commits = list(graph.line_commits)  # line commits, in their current order
        self.stats = Stats()
        self.new = {}  # orig sha -> final sha, for line and side commits as they are rebuilt
        self.merged_away = defaultdict(list)  # surviving sha -> shas squashed into it

    def run(self):
        """Sort, squash, then build the real commits.  Returns the sha of the new tip."""
        self.sort_back(self.order.rank_by_sha)
        self.squash_line()
        self.finalize()
        return self.chain_commits[-1].current_sha

    def parent_of(self, i):
        """The sha that slot i of the chain currently sits on."""
        return self.base if i == 0 else self.chain_commits[i - 1].current_sha

    def link(self, upto):
        """Make chain[0..upto] real commits that point at each other."""
        for j in range(upto + 1):
            c = self.chain_commits[j]
            if c.parent_sha != self.parent_of(j):
                c.reparent(self.parent_of(j))

    def sort_back(self, rank):
        """Bubble sort, sweeping tip -> tail.  After pass k, slots 0..k are final."""
        n = len(self.chain_commits)
        for first in range(n - 1):
            swapped = False
            for i in range(n - 2, first - 1, -1):
                if rank[self.chain_commits[i].original_sha] > rank[self.chain_commits[i + 1].original_sha]:
                    self.invariant_swap(i)
                    swapped = True
            if not swapped:
                break

    def invariant_swap(self, i):
        """
        Swap  X and Y CHANGES (not content) in P->X->Y  to P->Y'->X'
        The invariant is that X'.tree = Y.tree, ie the end point snapshot is unchanged.

        Merges: if Y is a merge it is replayed relative to its on-line parent
        (cherry-pick -m), and Y' keeps its side parents.  If X is a merge, X'
        keeps its side parents and just gets the new tree.  Trees are forced,
        never recomputed from the parents, so side parents are inert here.
        (They are rewired later, in finalize().)
        """
        self.link(i + 1)
        X, Y = self.chain_commits[i], self.chain_commits[i + 1]
        P = self.parent_of(i)
        print(f"swap: {X.label}  <->  {Y.label}")

        # Y' : apply Y's patch (base X, ours P, theirs Y) onto P without committing.
        # Explanation of inverted base in swaps/cherry/etc.
        # let Pt mean the tree, or snapshot at P (not the patch) (Ie P.tree in above notation)
        # By "pretending" the base is X, like this:
        #
        #  Xt -> Pt -> Yt'
        #     ↘   ↗
        #       Yt     # pretend it branched from X
        #
        # Imagine the snapshots all just have N chunks (parts)
        # if some chunk i of Pt, Pt_i, differs from Xt_i, but Yt_i is unchanged from Xt_i
        # a normal merge says to keep all forward-going changes on all branches.
        # So we keep the Pt_i patch which drops Yt_i, ie the parts of Yt that are still like Xt and not like Pt.
        # So, we get Pt with all Yt-like changes that are not Xt-like. ie Pt ->  Yt'  where Yt' = Yt-Xt
        # The only things we keep from the base Xt are things that look like Pt, which in the real order,
        # means they are in Xt but were not put there by the patch Xp
        #
        y_tree = cherry_pick_tree(P, Y, self.resolver.x_option_for(self.resolver.options.swap_conflict_policy),
                                  lambda: self.resolver.settle_swap(P, X, Y))

        # Y' goes where X was; X' is the old Y snapshot on top of it.
        x_tree_after = Y.tree
        Y.reparent(P, tree=y_tree)
        X.reparent(Y.current_sha, tree=x_tree_after)
        self.chain_commits[i], self.chain_commits[i + 1] = Y, X
        self.stats.swaps += 1

    # ------------------------------------------------- between the phases: squash
    def squash_line(self):
        """Collapse each squash group into its first commit.

        The line is already in its final order and  a group's tree is just its last member's tree.
        The survivor keeps its identity (author, original_sha); finalize() remakes it with the right parent.
        Groups never contain merges (validate_order), so there are no side parents to lose."""
        modes = self.order.squash_modes or {}
        if not modes:
            return
        by_sha = {c.original_sha: c for c in self.chain_commits}
        collapsed = []
        for group in self.order.squash_groups:
            target = by_sha[group[0]]  # target = tip commit of the group: survivor
            # add messages from rest of group if enabled:
            for sha in group[1:]:
                member = by_sha[sha]
                if modes[sha] == "squash":
                    target.message = target.message.rstrip("\n") + "\n\n" + member.message
                target.rewritten_tree = member.tree
                self.merged_away[target.original_sha].append(sha)
                self.stats.squashed += 1
            #keep the survivor:
            collapsed.append(target)
        self.chain_commits = collapsed

    # ------------------------------------------------- phase 2: final build
    def rebuilt(self, sha):
        """Final version of a parent: whatever we rebuilt it as, else untouched."""
        if sha in self.new:
            return self.new[sha]
        if sha in self.graph.line_shas or sha in self.graph.side_commit_store:
            raise Fatal(f"internal error: {short(sha)} is needed before it is rebuilt")
        return sha  # BASE or older: inert

    def finalize(self):
        """
            Phase 2: build the real commits.  Phase 1 left only throwaway intermediates
            (every swap writes new objects, with stale side parents).

            Each line commit is made once here, in the new order, with its chain parent set
            to the rebuilt predecessor, so everything from the first change to the tip is
            new.  Trees (snapshots) are already right, so nothing is replayed for the line.  Side
            commits are different: a merge's side parents sit on line commits whose shas
            just changed, so they are rebuilt first, parents before children (re-parented regardless
            if the tree underneath is unchanged), once each, inside
            this loop.  We replay all routes to a merge and then reparent the merge.
        """
        prev = self.base
        for chain_commit in self.chain_commits:
            # these trace back to all_shas which are parent to child ordered
            for side_commit in side_ancestor_commits(self.graph.side_commit_store, chain_commit.side_parent_shas):
                if side_commit.original_sha not in self.new:
                    self.replay_side_commit(side_commit)
            parents = list(chain_commit.original_parent_shas)
            for idx, parent in enumerate(parents):
                parents[idx] = prev if idx == chain_commit.chain_parent_idx else self.rebuilt(parent)
            chain_commit.remake(parents)
            self.new[chain_commit.original_sha] = chain_commit.current_sha
            for gone in self.merged_away.get(chain_commit.original_sha, ()):
                self.new[gone] = chain_commit.current_sha  # forks attached to a squashed member
            prev = chain_commit.current_sha

    def replay_side_commit(self, S):
        """Rebuild side commit S on its remade parents (name-attach: a parent that is a
        line commit becomes that commit's final version)."""
        # Finalize order guarantees our original parents are already rebuilt.
        new_parents = [
            # defaults to original parent (...,p) if no transplant defined in fork_edges.
            self.rebuilt(self.order.fork_edges.get(Edge(S.original_sha, parent_idx), parent_sha))
            for parent_idx, parent_sha in enumerate(S.original_parent_shas)
        ]
        if new_parents == list(S.original_parent_shas):
            self.new[S.original_sha] = S.original_sha  # nothing above it changed
            return
        if tree_of(new_parents[0]) == tree_of(S.original_parent_sha):
            tree = S.original_tree  # same snapshot underneath: no replay needed
            self.stats.reparented += 1
        else:
            P = new_parents[0]
            print(f"replay: {S.label}  onto  {short(P)}")
            tree = cherry_pick_tree(P, S, self.resolver.x_option_for(self.resolver.options.branch_conflict_policy),
                                    lambda: self.resolver.settle_replay(P, S))
            self.stats.replayed += 1
        S.remake(new_parents, tree)
        self.new[S.original_sha] = S.current_sha


# ------------------------------------------------------------------ order
def editor_order(graph):
    """Let the user reorder `line`, squash commits, and move fork markers, in an editor.

    :param graph: Graph, the line and side commits as they are today.
    :return: Order
    """
    original_fork_edges = default_fork_edges(graph)

    todo = ["# Reorder 'pick' lines, oldest first. Do not add or remove lines.",
            "# Change 'pick' to 'squash' (or 'fixup', which drops the message) to fold a",
            "# commit into the line above it, after reordering.  Merges cannot be squashed ",
            "# (but can be squashed itno)",
            "# A 'fork' line attaches a side branch to the first non-fork above it.",
            "# Move it to move the fork point.  It must stay below some pick.",
            "# You can't squash onto a fork.",
            "# 'fork abc123:2' has the prior non-fork commit as its side parent",
            "# The final snapshot is guaranteed identical to today's tip.", ""]
    for commit in graph.line_commits:
        todo += [f"pick {short(commit.original_sha)} {commit.subject}"]
        # get forks:
        for edge, fork_sha in original_fork_edges.items():
            if fork_sha == commit.original_sha:
                tag = short(edge.child_sha) + (f":{edge.parent_idx + 1}" if edge.parent_idx else "")
                todo += [f"fork {tag} {graph.side_commit_store[edge.child_sha].subject}"]

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("\n".join(todo) + "\n")
    try:
        editor = os.environ.get("GIT_SEQUENCE_EDITOR") or git_str("var GIT_EDITOR")
        subprocess.run(shlex.split(editor) + [f.name], check=True)
        with open(f.name) as edited:
            lines = edited.read().splitlines()
    finally:
        os.unlink(f.name)

    picked_shas = []
    squash_modes = {}
    moved_fork_edges = {}
    fork_since_member = False  # a fork line sits between the last pick/squash line and here
    line_by_sha = {c.original_sha: c for c in graph.line_commits}
    for text in lines:
        if not text.strip() or text.strip().startswith("#"):
            continue
        verb, *rest = text.split()
        if not rest:
            raise Fatal(f"incomplete line: {text!r}")
        ref = rest[0]
        if verb in ("pick", "squash", "s", "fixup", "f"):
            hit = [c.original_sha for c in graph.line_commits if c.original_sha.startswith(ref)]
            if len(hit) != 1:
                raise Fatal(f"cannot identify commit in line: {text!r}")
            if verb != "pick":
                if line_by_sha[hit[0]].is_merge:
                    raise Fatal(f"can't squash a merge into the commit above it "
                                f"(its side parents would be lost): {text!r}")
                if not picked_shas:
                    raise Fatal(f"nothing to squash into: {text!r}")
                if fork_since_member:
                    raise Fatal(f"can't squash onto a fork: {text!r}\n"
                                f"  move the fork line below the last squash of the group")
                squash_modes[hit[0]] = "fixup" if verb in ("fixup", "f") else "squash"
            picked_shas.append(hit[0])
            fork_since_member = False
        elif verb == "fork":
            if not picked_shas:
                raise Fatal(f"fork line must come after a pick line: {text!r}")
            ref, _, parent_number = ref.partition(":")
            try:
                parent_idx = int(parent_number) - 1 if parent_number else 0
            except ValueError:
                raise Fatal(f"bad parent number in: {text!r}")
            hit = sorted({edge.child_sha for edge in original_fork_edges if edge.child_sha.startswith(ref)})
            if len(hit) != 1 or Edge(hit[0], parent_idx) not in original_fork_edges:
                raise Fatal(f"cannot identify side branch fork: {text!r}")
            edge = Edge(hit[0], parent_idx)
            if edge in moved_fork_edges:
                raise Fatal(f"duplicate fork line: {text!r}")
            moved_fork_edges[edge] = picked_shas[-1]
            fork_since_member = True
        else:
            raise Fatal(f"unknown todo line: {text!r}")

    if len(moved_fork_edges) != len(original_fork_edges):
        raise Fatal("every fork line must be kept; none may be added or removed")
    return Order(picked_shas, moved_fork_edges, squash_modes)


def load_order(args, graph):
    """The requested Order, from --order if given, else from the editor.

    :param args: parsed argparse namespace.
    :param graph: Graph
    :return: Order
    """
    original_line_shas = [commit.original_sha for commit in graph.line_commits]
    if args.order:
        try:
            positions = [int(n) for n in args.order.split(",")]
        except ValueError:
            positions = []
        if sorted(positions) != list(range(1, len(original_line_shas) + 1)):
            raise Fatal(f"--order must be a permutation of 1..{len(original_line_shas)}")
        return Order([original_line_shas[n - 1] for n in positions], default_fork_edges(graph))
    else:
        order = editor_order(graph)
        if sorted(order.ordered_line_shas) != sorted(original_line_shas):
            raise Fatal("the set of commits changed; only reordering is allowed")
    return order


# ------------------------------------------------------------------- main

def build_parser():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base", nargs="?", help="The last non-reorderable commit to rebuild on, like in rebase.")
    ap.add_argument("--via", action="append", default=[], metavar="COMMIT",
                    help="For a diamond, route the line through COMMIT (repeatable, oldest first)")
    ap.add_argument("--mergetool", action="store_true", help="use mergetool")
    ap.add_argument("-t", "--tool", help="Specify git mergetool name (e.g., meld, p4merge)")
    ap.add_argument("--abort", action="store_true",
                    help="from inside the conflict shell: abort the whole reorder")
    ap.add_argument("--order", help="1-based positions, oldest=1, e.g. 1,5,2,3,4, only considering AFTER base")
    ap.add_argument("--auto", action="store_true", help="Auto resolve conflicts with default policies:\n"
                                                        "Equivalent to --auto-squash-left --branchX=ours.  Explicit options override the default.")
    squash_direction = ap.add_mutually_exclusive_group()
    squash_direction.add_argument("--auto-squash-left", dest="swap_conflict_policy",
                                  action="store_const", const="theirs",
                                  help="for conflicting swap: squash left and duplicate. This is generally the preferred auto-mode,\n"
                                       "since moving changes back in time while preserving the end result means things in between become less relevant. \n"
                                       "But exceptions are abundant, including even the original motivating README example!")
    squash_direction.add_argument("--auto-squash-right", dest="swap_conflict_policy",
                                  action="store_const", const="ours",
                                  help="for conflicting swap, duplicate prior commit and squash right. \n"
                                       "Might be sensible if moving a commit toward tip, if you want to wipe out intermediate states along the way.\n"
                                       "or if a feature you're moving back modified now-future features that it itself doesn't need (the README example).")
    ap.add_argument("--branchX", dest="branch_conflict_policy", choices=["ours", "theirs"],
                    help="Merge strategy for conflict during side-branch replay; works like -X for rebase, but makes replay fully automatic. \n"
                         "This only matters if reordering commits across a branch point.")
    ap.add_argument("--auto-files",nargs="+", action="extend",default=[],
                    help="If set, --auto* and --branchX will only apply to files listed here.")
    ap.add_argument("--force-left-files", nargs="+", action="extend", default=[], metavar="PATHSPEC",
                    help="In P->X->Y to P->Y'->X' swaps, Y' always gets matching files from Y:\n"
                         "so all changes occur in P to Y', squashed left.  Takes precedence over --auto-files.")
    ap.add_argument("--force-right-files", nargs="+", action="extend", default=[], metavar="PATHSPEC",
                    help="In P->X->Y to P->Y'->X' swaps, Y' always gets matching files from from 'P':\n"
                         "so all changes occur in Y' to X', squashed right. Takes precedence over --auto-files.")
    return ap


def parse_options(args):
    """Turn parsed arguments into Options, applying the --auto defaults."""
    swap_conflict_policy = args.swap_conflict_policy
    branch_conflict_policy = args.branch_conflict_policy
    if args.auto:
        swap_conflict_policy = swap_conflict_policy or "theirs"
        branch_conflict_policy = branch_conflict_policy or "ours"
    auto_files = tuple(args.auto_files) or None
    if auto_files and not (swap_conflict_policy or branch_conflict_policy):
        raise Fatal("--auto-files needs --auto, --auto-squash-left/right or --branchX")
    return Options(swap_conflict_policy=swap_conflict_policy,
                   branch_conflict_policy=branch_conflict_policy,
                   mergetool=args.mergetool,
                   tool=args.tool,
                   auto_files=auto_files,
                   force_left_files=tuple(args.force_left_files) or None,
                   force_right_files=tuple(args.force_right_files) or None)

@contextmanager
def restore_on_failure(restore):
    """If the body fails for any reason, put the working tree and HEAD back before re-raising."""
    try:
        yield
    except BaseException:
        git("reset -q --hard", check=False)
        git(["checkout", "-q", "-f", restore], check=False)
        raise


def update_branch_ref(head_ref, restore, old_head, new_head):
    """The one place the branch ref moves."""
    if head_ref:
        git_str(["update-ref", "-m", "git-rebubble: reorder", head_ref, new_head, old_head])
        git(["checkout", "-q", "-f", restore])
    else:
        git(["checkout", "-q", "--detach", new_head])


def git_rebubble_core():
    args = build_parser().parse_args()
    if args.abort:
        open(abort_flag(), "w").close()
        print("rebubble abort requested;")
        return
    if not args.base:
        build_parser().error("the following arguments are required: base")
    options = parse_options(args)

    vias = [git_str(["rev-parse", "--verify", f"{v}^{{commit}}"]) for v in args.via]

    os.chdir(git_str("rev-parse --show-toplevel"))
    if git_str(["status", "--porcelain", "-uno"]):
        raise Fatal("working tree has uncommitted changes")

    base = git_str(["rev-parse", "--verify", f"{args.base}^{{commit}}"])
    old_head = git_str("rev-parse HEAD")
    graph = build_graph(base, old_head, vias)
    original_line_shas = [commit.original_sha for commit in graph.line_commits]
    if len(original_line_shas) < 2:
        raise Fatal("need at least two commits in range (is BASE an ancestor of HEAD?)")

    order = load_order(args, graph)
    if order.ordered_line_shas == original_line_shas and not order.squash_modes:
        print("nothing to do")
        return
    validate_order(graph, order)

    head_ref = git("symbolic-ref -q HEAD", check=False).stdout.strip()
    old_tree = git_str(["rev-parse", "HEAD^{tree}"])
    restore = head_ref.replace("refs/heads/", "", 1) if head_ref else old_head

    bubble = Bubble(base, graph, order, Resolver(options))
    with restore_on_failure(restore):
        new_head = bubble.run()
        if tree_of(new_head) != old_tree:
            raise Fatal("internal error: final tree differs from original")

    update_branch_ref(head_ref, restore, old_head, new_head)
    squash_note = f", {bubble.stats.squashed} squashed" if bubble.stats.squashed else ""
    side_note = ""
    if graph.side_commit_store:
        side_note = (f", {bubble.stats.replayed} side commit(s) replayed, "
                     f"{bubble.stats.reparented} re-parented only")
    print(f"\ndone: {bubble.stats.swaps} swap(s){squash_note}{side_note}. Tip tree content unchanged; old tip was {short(old_head)}.")
    print(f"  (Tip: If something looks off, run 'git reset --hard {short(old_head)}' to roll back)\n")


def git_rebubble():
    try:
        git_rebubble_core()
    except (Fatal, RuntimeError) as e:
        sys.exit(f"git-rebubble: {e}")
    except KeyboardInterrupt:
        sys.exit("git-rebubble: interrupted; branch left untouched")


if __name__ == "__main__":
    git_rebubble()