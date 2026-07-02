cd .#!/bin/bash

set -e

if [ -z "$1" ]; then
    echo "Usage: $0 <source_folder>"
    exit 1
fi

SOURCE_FOLDER="$1"

if [ ! -d "$SOURCE_FOLDER/nodes" ]; then
    echo "Error: $SOURCE_FOLDER/nodes does not exist"
    exit 1
fi

rm -rf nodes
cp -r "$SOURCE_FOLDER/nodes" nodes

git add .
git commit -m "Sync nodes from $SOURCE_FOLDER - $(date '+%Y-%m-%d %H:%M:%S')"
git push origin
