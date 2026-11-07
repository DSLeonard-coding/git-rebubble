# lib.sh
set -euo pipefail
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@x GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@x

init_repo() { cd "$(mktemp -d)"; git init -q -b main; git commit -q --allow-empty -m 0; BASE=$(git rev-parse HEAD); }

# commit MSG FILE=TEXT ...     (appends TEXT as a line to FILE)
commit() { local msg=$1; shift
  for kv in "$@"; do echo "${kv#*=}" >> "${kv%%=*}"; done
  git add -A; git commit -q --allow-empty -m "$msg"; }

tree()      { git rev-parse "$1^{tree}"; }
show_log()  { git log --reverse --format='  %s' "$BASE..HEAD"; }

# expect REV FILE CONTENT   (content with \n for newlines)
expect() { local got; got=$(git show "$1:$2" 2>/dev/null || echo "<missing>")
  [ "$got" = "$(printf "$3")" ] || { echo "FAIL: $1:$2 was [$got], wanted [$3]"; exit 1; }; }

expect_tree_unchanged() { [ "$(tree HEAD)" = "$1" ] || { echo "FAIL: tip tree changed"; exit 1; }; }