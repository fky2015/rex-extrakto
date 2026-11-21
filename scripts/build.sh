#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
cargo build --release --locked "$@"
mkdir -p bin
for task_name in rex-extract rex-collect rex-popup; do
    task_binary=$(mktemp "bin/$task_name.XXXXXX")
    trap 'rm -f "$task_binary"' EXIT
    install -m 0755 "target/release/$task_name" "$task_binary"
    mv -f "$task_binary" "bin/$task_name"
    printf 'Built %s/bin/%s\n' "$PWD" "$task_name"
done
