#!/bin/sh
# Cut a release. Usage:  ./scripts/release.sh 1.1.0
#
# Bumps VERSION, commits, tags and pushes. Everything after that is
# .github/workflows/release.yml: it creates the GitHub release, hashes the
# tarball and pushes the updated formula to nicolasdao/homebrew-tap.
#
# There is deliberately no sha256 to copy by hand here - that step is exactly
# the one that silently rots when a release is cut in a hurry.
set -e

V="$1"
[ -n "$V" ] || { echo "usage: $0 X.Y.Z"; exit 1; }
echo "$V" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' || {
  echo "version must be semver X.Y.Z (got '$V')"; exit 1; }

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

[ -z "$(git status --porcelain)" ] || { echo "working tree is dirty. commit first."; exit 1; }

git rev-parse "v$V" >/dev/null 2>&1 && { echo "tag v$V already exists."; exit 1; }

CUR="$(cat VERSION)"
echo "  $CUR -> $V"

python3 tests/test_control.py

echo "$V" > VERSION
git add VERSION
git commit -m "release v$V"
git tag -a "v$V" -m "v$V"
git push origin HEAD
git push origin "v$V"

echo
echo "  pushed v$V. the release workflow will:"
echo "    - publish the GitHub release"
echo "    - update nicolasdao/homebrew-tap to $V"
echo
echo "  watch it:  gh run watch"
echo
