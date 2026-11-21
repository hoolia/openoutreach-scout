#!/bin/sh
# Download the pinned OpenOutreach commit, verify its checksum and unpack only the
# linkedin_cli package and the licence into ./upstream (or the directory given).
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
. "$here/upstream.env"
dest=${1:-$here/upstream}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

url="$UPSTREAM_REPO/archive/$UPSTREAM_COMMIT.tar.gz"
if command -v curl >/dev/null 2>&1; then
  curl -fsSL -o "$tmp/src.tgz" "$url"
else
  python3 -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' "$url" "$tmp/src.tgz"
fi
echo "$UPSTREAM_SHA256  $tmp/src.tgz" | sha256sum -c -
tar -xzf "$tmp/src.tgz" -C "$tmp"
src="$tmp/OpenOutreach-$UPSTREAM_COMMIT"

rm -rf "$dest/linkedin_cli"
mkdir -p "$dest"
cp -r "$src/linkedin_cli" "$dest/linkedin_cli"
cp "$src/LICENCE.md" "$dest/LICENCE.md"
echo "$UPSTREAM_COMMIT" > "$dest/COMMIT"
echo "linkedin_cli at $UPSTREAM_COMMIT in $dest"
