#!/bin/sh
set -eu

workdir=$(mktemp -d "${TMPDIR:-/tmp}/codex-web-gpt-patcher.XXXXXX")
trap 'rm -rf "$workdir"' 0
trap 'exit 129' 1
trap 'exit 130' 2
trap 'exit 143' 15

archive_url=https://raw.githubusercontent.com/sdaniger/oh-my-codex-chatgpt-web/main/CodexWebGPT-Patcher-6.1.14-macos-arm64.zip
curl -fsSL --retry 3 "$archive_url" -o "$workdir/patcher.zip"
/usr/bin/ditto -x -k "$workdir/patcher.zip" "$workdir"
/bin/sh "$workdir/CodexWebGPT-Patcher/apply.sh" "$@"
