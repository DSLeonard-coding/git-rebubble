#!/usr/bin/env bash
# Same history, same reorder (A B E C D), but with plain `git rebase -i`.
set -euo pipefail
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@x GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@x

commit() { local m=$1; shift; for kv in "$@"; do echo "${kv#*=}" >> "${kv%%=*}"; done
           git add -A; git commit -q --allow-empty -m "$m"; }

cd "$(mktemp -d)" && git init -q -b main
git commit -q --allow-empty -m 0; BASE=$(git rev-parse HEAD)
commit A A=A
commit B B=B
commit C C=C
commit D D=D
commit E B=E C=E D=E                       # E touches B, C and D

ORIG=$(git rev-parse HEAD)
TIP=$(git rev-parse 'HEAD^{tree}')

# Build the todo list ourselves so no editor opens:  A B E C D
TODO=$(mktemp)
for n in A B E C D; do echo "pick $(git log -1 --format=%h --grep="^$n\$") $n"; done > "$TODO"

echo; echo "== git rebase -i: move E up next to B =="
GIT_SEQUENCE_EDITOR="cp $TODO" git rebase -i "$BASE" || true   # stops: E edits C and D, which don't exist yet

echo; echo "== conflict: E modified C and D, but they don't exist at this point =="
git status --short
echo "== the only sensible resolution is to delete them, so do that =="
git rm -q C D
GIT_EDITOR=true git rebase --continue >/dev/null               # C and D are then re-added as plain C and D

echo; echo "history:  $(git log --reverse --format=%s "$BASE..HEAD" | tr '\n' ' ')"
echo; echo "what changed between the original tip and the rebased tip:"

git diff --stat "$ORIG" HEAD
echo; echo "C is now: $(git show HEAD:C | tr '\n' ' ')   (it was: $(git show "$ORIG:C" | tr '\n' ' '))"
echo "D is now: $(git show HEAD:D | tr '\n' ' ')   (it was: $(git show "$ORIG:D" | tr '\n' ' '))"

if [ "$(git rev-parse 'HEAD^{tree}')" = "$TIP" ]; then
  echo; echo "final snapshot: identical (rebase got lucky)"
else
  echo; echo "final snapshot: DIFFERENT. E's changes to C and D are gone."
fi