#!/bin/sh
set -eu
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'Python 3.8 or newer is required.' >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { printf '%s\n' 'curl is required.' >&2; exit 1; }
task_repo=${REX_EXTRAKTO_REPOSITORY:-fky2015/rex-extrakto}
task_ref=${REX_EXTRAKTO_REF:-main}
case "$task_repo/$task_ref" in
    *[!a-zA-Z0-9._/-]*) printf '%s\n' 'Invalid repository or ref.' >&2; exit 1 ;;
esac
task_tmp=$(mktemp -d)
trap 'rm -rf "$task_tmp"' EXIT
curl -fsSL --connect-timeout 10 --max-time 60 \
    "https://raw.githubusercontent.com/$task_repo/$task_ref/scripts/install.py" \
    -o "$task_tmp/install.py"
python3 "$task_tmp/install.py" "$@"
