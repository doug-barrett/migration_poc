#!/bin/bash
set -e

SOURCE_DIR=~/work/POC/Omnia/migration_poc/nodes
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
NODES_DIR="$REPO_DIR/nodes"

if [ ! -d "$SOURCE_DIR" ]; then
  echo "Error: Source directory not found: $SOURCE_DIR"
  exit 1
fi

rm -rf "$NODES_DIR"
mkdir -p "$NODES_DIR"
cp -r "$SOURCE_DIR"/* "$NODES_DIR"/

cd "$REPO_DIR"
git add .
COMMIT_MSG="Update nodes - $(date '+%Y-%m-%d %H:%M:%S')"
git commit -m "$COMMIT_MSG"
git push

echo "Done: $COMMIT_MSG"
