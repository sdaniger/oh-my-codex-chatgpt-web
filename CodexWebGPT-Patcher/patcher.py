#!/usr/bin/env python3
"""Create a hardened copy of a verified vanilla Codex Web GPT application."""
import argparse
import ctypes
import hashlib
import json
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
INFO = json.loads((HERE / "payload.json").read_text())
VERSION = INFO["version"]


def rename_exclusive(source, target):
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renamex_np
    rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(os.fsencode(source), os.fsencode(target), 0x00000004) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))


def run(command, env):
    result = subprocess.run(command, env=env, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[-4000:])
    return result.stdout


def validate_payload(root):
    for name, expected in INFO["payloadFiles"].items():
        path = root / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"パッチデータの整合性を確認できません: {name}")


def main():
    parser = argparse.ArgumentParser(description="バニラ版からパッチ済みアプリのコピーを作成します。")
    parser.add_argument("--app", default="/Applications/Codex Web GPT.app", help="元アプリのパス")
    parser.add_argument("--output", default="/Applications/Codex Web GPT Patched.app", help="新規作成するアプリのパス")
    args = parser.parse_args()
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise RuntimeError("このパッチはmacOS Apple Silicon版専用です。")
    source = pathlib.Path(args.app).expanduser().resolve(strict=True)
    output_arg = pathlib.Path(args.output).expanduser().absolute()
    output = output_arg.parent.resolve() / output_arg.name
    if not source.is_dir() or source.suffix != ".app" or output.suffix != ".app":
        raise RuntimeError("入力と出力には.appフォルダーを指定してください。")
    if output == source or source in output.parents:
        raise RuntimeError("出力先は入力アプリの外側に指定してください。")
    if output.exists() or output.is_symlink():
        raise RuntimeError(f"出力先が既に存在します: {output}")
    if not output.parent.is_dir():
        raise RuntimeError(f"出力先の親フォルダーがありません: {output.parent}")
    import plistlib
    with (source / "Contents/Info.plist").open("rb") as handle:
        plist = plistlib.load(handle)
    if plist.get("CFBundleShortVersionString") != VERSION or plist.get("CFBundleIdentifier") != "dev.codexwebgpt.launcher":
        raise RuntimeError(f"対応するバニラ版は Codex Web GPT {VERSION} のみです。")
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(source)], os.environ.copy())

    payload = HERE / "payload"
    validate_payload(payload)
    state = pathlib.Path.home() / ".codex-chatgpt-web" / INFO["stateDirectory"]
    state.parent.mkdir(parents=True, exist_ok=True)
    if not state.exists():
        with tempfile.TemporaryDirectory(prefix=".patcher-", dir=state.parent) as temp:
            staged = pathlib.Path(temp) / "payload"
            shutil.copytree(payload, staged, symlinks=True)
            try:
                rename_exclusive(staged, state)
            except FileExistsError:
                pass
    validate_payload(state)
    env = os.environ.copy()
    env.pop("ELECTRON_RUN_AS_NODE", None)
    env["CODEX_WEB_GPT_HARDENING_ROOT"] = str(state)
    env["PYTHONNOUSERSITE"] = "1"
    bridge = state / "bin/update-bridge.py"
    verified = json.loads(run(["/usr/bin/python3", str(bridge), "guard", "--version", VERSION], env))
    if verified.get("bundleId") != INFO["bundleId"]:
        raise RuntimeError("パッチ済みランタイムの識別子が一致しません。")
    for name, expected in INFO["vanillaUpdaterFiles"].items():
        contents = run([str(state / "versions" / VERSION / "runtime/runtime/bun"),
            str(state / "bin/asar-tool.cjs"), "read", str(source / "Contents/Resources/app.asar"), name], env)
        if hashlib.sha256(contents.encode()).hexdigest() != expected:
            raise RuntimeError(f"対応するバニラ版と一致しません: {name}")
    with tempfile.TemporaryDirectory(prefix=".codex-web-patcher-", dir=output.parent) as temp:
        staged_app = pathlib.Path(temp) / "Codex Web GPT.app"
        run(["/usr/bin/ditto", str(source), str(staged_app)], env)
        run(["/usr/bin/python3", str(bridge), "stage", "--app", str(staged_app), "--version", VERSION], env)
        if output.exists() or output.is_symlink():
            raise RuntimeError(f"出力先が既に存在します: {output}")
        rename_exclusive(staged_app, output)
    print(f"パッチ済みアプリを作成しました: {output}")
    print("元のアプリと既存のチャット・ログイン情報は変更していません。")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"パッチを適用できませんでした: {error}", file=sys.stderr)
        sys.exit(1)
