#!/usr/bin/env bash
# Move E back next to B (E touches B, C and D) without dragging C and D along.
(
set -euo pipefail
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@x GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@x

rebubble() { uvx --from "git+https://github.com/DSLeonard-coding/git-rebubble.git" git-rebubble "$@"; }
commit() { local m=$1; shift; for kv in "$@"; do echo "${kv#*=}" >> "${kv%%=*}"; done
           git add -A; git commit -q --allow-empty -m "$m"; }

cd "$(mktemp -d)" && git init -q -b main
git commit -q --allow-empty -m 0; BASE=$(git rev-parse HEAD)
commit A A=A
commit B B=B
commit C C=C
commit D D=D
commit E B=E C=E D=E                       # E touches B, C and D

TIP=$(git rev-parse 'HEAD^{tree}')

rebubble "$BASE" --order 1,2,5,3,4 --auto  # order: A B E C D

echo; echo "history:  $(git log --reverse --format=%s "$BASE..HEAD" | tr '\n' ' ')"
echo; echo "E' now touches only B:"; git show --stat --format='  %s' HEAD~2 | sed '/^$/d'
[ "$(git rev-parse 'HEAD^{tree}')" = "$TIP" ] && echo && echo "final snapshot: identical to before"
)