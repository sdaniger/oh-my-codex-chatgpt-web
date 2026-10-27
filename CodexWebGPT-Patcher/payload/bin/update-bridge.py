#!/usr/bin/env python3
"""Keep the Codex Web GPT multipart hardening across launcher updates.

The launcher updater normally replaces the whole .app with the official build.
This bridge gives every update a strict preflight and a staging step:

  * `prepare` builds a hardened runtime for a release tag from the canonical patch.
  * `guard` proves that the target version has a complete, verified artifact.
  * `stage` injects the artifact and the hardened update worker into the staged app,
    then re-signs it. The official update is refused unless this succeeds.
  * `install-bridge` installs the update worker into the current app (app closed).
  * `apply-patch` applies the canonical patch to a source checkout without building.
  * `watch` proactively prepares artifacts for new releases.

The hardened worker replaces only electron/update-worker.cjs inside app.asar, so
future updates keep using this bridge instead of the official whole-app swap.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import plistlib
import posixpath
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path(
    os.environ.get("CODEX_WEB_GPT_HARDENING_ROOT", str(pathlib.Path.home() / ".codex-chatgpt-web" / "codex-web-patcher-7a2674e73e1833ac"))
).resolve()
APP = pathlib.Path("/Applications/Codex Web GPT.app")
APP_RUNTIME = APP / "Contents/Resources/runtime"
APP_ASAR = APP / "Contents/Resources/app.asar"
CORE_HOME = pathlib.Path.home() / ".codex-chatgpt-web"
VERSIONS = ROOT / "versions"
PATCH = ROOT / "patch" / "hermes-codexweb-hardening.patch"
WORKER_TEMPLATE = ROOT / "worker" / "update-worker-hardened.cjs"
ASAR_TOOL = ROOT / "bin" / "asar-tool.cjs"
LAUNCHER_ROOT = ROOT / "launcher"
ENTITLEMENTS = ROOT / "launcher-entitlements.plist"
LOGS = ROOT / "logs"
BACKUPS = ROOT / "backups"
STATE = ROOT / "state.json"
LOG = LOGS / "update-bridge.log"

REPOSITORY = "miuuyy/codex-chatgpt-web"
RELEASE_API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
BUNDLE_ID = "dev.codexwebgpt.launcher"
HEALTH_URL = "http://127.0.0.1:17841/healthz"
HERMES_DB = pathlib.Path.home() / ".hermes" / "state.db"
WORKER_MARKER = "CODEX_WEB_GPT_HARDENING_WORKER_V1"
GUARD_MARKER = "CODEX_WEB_GPT_HARDENING_GUARD_V1"
LAUNCHER_MARKER = "CODEX_WEB_GPT_LAUNCHER_SESSION_V1"
PATCH_REQUIRED_FILES = {
    "src/adapters/chatgpt-web/browser-worker.ts",
    "tests/browser-turn-binding.test.ts",
}
TARGET_TESTS = [
    "tests/browser-turn-binding.test.ts",
    "tests/browser-recovery.test.ts",
    "tests/environment.test.ts",
    "tests/goal-continuation.test.ts",
    "tests/prompt-contract.test.ts",
    "tests/server-compaction.test.ts",
    "tests/server-lifecycle.test.ts",
    "tests/native-recovery.test.ts",
    "tests/native-sse-recovery.test.ts",
]


class BridgeError(RuntimeError):
    pass


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def log(message: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    line = f"{utc_now()} {message}"
    print(line, file=sys.stderr)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def record(status: str, **details) -> None:
    payload = {"status": status, "at": utc_now(), "pid": os.getpid(), **details}
    temporary = STATE.with_name(f"{STATE.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(STATE)


def run(
    argv: list[str],
    *,
    cwd: pathlib.Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 1_800,
    check: bool = True,
) -> subprocess.CompletedProcess:
    display = " ".join(str(part) for part in argv)
    log(f"run: {display[:500]}")
    process_env = dict(os.environ)
    process_env.pop("ELECTRON_RUN_AS_NODE", None)
    if env:
        process_env.update(env)
    result = subprocess.run(
        [str(part) for part in argv],
        cwd=str(cwd) if cwd else None,
        env=process_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    combined = f"{result.stdout or ''}{result.stderr or ''}".strip()
    if combined:
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(combined[-8000:] + "\n")
    if check and result.returncode != 0:
        raise BridgeError(
            f"command failed ({result.returncode}): {display}\n{combined[-6000:]}"
        )
    return result


def read_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def patch_for(version: str | None = None) -> pathlib.Path:
    specific = PATCH.with_name(f"hermes-codexweb-hardening-{version}.patch") if version else PATCH
    return specific if specific.is_file() else PATCH


def patch_sha(version: str | None = None) -> str:
    patch = patch_for(version)
    if not patch.exists():
        raise BridgeError(f"canonical patch is missing: {patch}")
    return sha256_file(patch)


def app_version(app: pathlib.Path = APP) -> str:
    plist = app / "Contents/Info.plist"
    result = subprocess.run(
        ["/usr/libexec/PlistBuddy", "-c", "Print :CFBundleShortVersionString", str(plist)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise BridgeError(f"could not read the app version from {plist}: {result.stderr[-500:]}")
    return result.stdout.strip()


def version_tuple(value: str) -> tuple[int, int, int]:
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$", value.strip())
    if not match:
        raise BridgeError(f"invalid version: {value!r}")
    prerelease = match.group(4)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3))) + ((0,) if prerelease else (1,))


def _bundle_file_paths(root: pathlib.Path) -> list[str]:
    paths: list[str] = []
    for entry in sorted(root.rglob("*")):
        relative = entry.relative_to(root).as_posix()
        if relative == "manifest.json":
            continue
        if entry.is_symlink():
            paths.append(relative)
        elif entry.is_file():
            paths.append(relative)
    return sorted(paths)


def verify_runtime(
    path: pathlib.Path,
    *,
    version: str | None = None,
    bundle: str | None = None,
    full: bool = True,
) -> dict:
    """Validate a runtime bundle exactly like the launcher does."""
    if not path.is_dir():
        raise BridgeError(f"runtime directory is missing: {path}")
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file():
        raise BridgeError(f"runtime manifest is missing: {manifest_path}")
    identity = read_json(manifest_path)
    if identity.get("schemaVersion") != 2:
        raise BridgeError(f"{path} has unsupported runtime schema {identity.get('schemaVersion')}")
    if version is not None and identity.get("appVersion") != version:
        raise BridgeError(f"{path} belongs to app version {identity.get('appVersion')}, expected {version}")
    if identity.get("platform") != "darwin" or identity.get("arch") != "arm64":
        raise BridgeError(f"{path} has the wrong platform identity: {identity.get('platform')}/{identity.get('arch')}")
    if identity.get("launcher") != "bin/codex-chatgpt-web" or identity.get("entrypoint") != "app/cli.js":
        raise BridgeError(f"{path} has an unexpected launcher or entrypoint")
    expected_bundle = identity.get("bundleId")
    if not isinstance(expected_bundle, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_bundle):
        raise BridgeError(f"{path} has an invalid bundleId")
    if bundle is not None and expected_bundle != bundle:
        raise BridgeError(f"{path} has bundleId {expected_bundle}, expected {bundle}")
    records = identity.get("files")
    if not isinstance(records, list) or not records:
        raise BridgeError(f"{path} has no runtime file records")

    digest = hashlib.sha256()
    expected_paths: list[str] = []
    previous: str | None = None
    for index, item in enumerate(records):
        if not isinstance(item, dict):
            raise BridgeError(f"{path} has an invalid file record at index {index}")
        relative = item.get("path")
        size = item.get("size")
        checksum = item.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or relative == "manifest.json"
            or "\\" in relative
            or posixpath.isabs(relative)
            or posixpath.normpath(relative) != relative
            or any(segment in ("", ".", "..") for segment in relative.split("/"))
        ):
            raise BridgeError(f"{path} has an unsafe runtime path: {relative!r}")
        if not isinstance(size, int) or size < 0 or not isinstance(checksum, str) or len(checksum) != 64:
            raise BridgeError(f"{path} has an invalid file record for {relative}")
        if previous is not None and previous >= relative:
            raise BridgeError(f"{path} has unsorted or duplicate runtime paths: {relative}")
        previous = relative
        expected_paths.append(relative)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(checksum.encode("ascii"))
        digest.update(b"\0")
    if digest.hexdigest() != expected_bundle:
        raise BridgeError(f"{path} manifest bundleId does not match its file records")

    expected = set(expected_paths)
    actual_paths = _bundle_file_paths(path)
    actual = set(actual_paths)
    missing = [relative for relative in expected_paths if relative not in actual]
    unexpected = [relative for relative in actual_paths if relative not in expected]
    if missing:
        raise BridgeError(f"{path} is missing runtime file {missing[0]}")
    if unexpected:
        raise BridgeError(f"{path} contains an unmanifested file {unexpected[0]}")
    if len(actual_paths) != len(expected_paths):
        raise BridgeError(f"{path} file count mismatch: expected {len(expected_paths)}, found {len(actual_paths)}")
    for required in ("app/cli.js", "app/browser-helper.cjs", "bin/codex-chatgpt-web", "runtime/bun"):
        if required not in expected:
            raise BridgeError(f"{path} does not declare required runtime file {required}")

    canonical_root = path.resolve()
    if full:
        for item in records:
            absolute = path.joinpath(*item["path"].split("/"))
            if not absolute.exists():
                raise BridgeError(f"{path} is missing runtime file {item['path']}")
            if absolute.is_symlink():
                try:
                    absolute.resolve().relative_to(canonical_root)
                except ValueError:
                    raise BridgeError(f"{path} has a symlink escaping the bundle: {item['path']}") from None
            stat = absolute.stat()
            if not absolute.is_file() or stat.st_size != item["size"]:
                raise BridgeError(f"{path} has a size or type mismatch for {item['path']}")
            if sha256_file(absolute) != item["sha256"]:
                raise BridgeError(f"{path} has a checksum mismatch for {item['path']}")
    if os.stat(path / "runtime/bun").st_mode & 0o111 == 0:
        raise BridgeError(f"{path} has a non-executable Bun runtime")
    return identity


def artifact(version: str) -> tuple[pathlib.Path, dict]:
    directory = VERSIONS / version
    runtime = directory / "runtime"
    metadata_path = directory / "metadata.json"
    if not runtime.is_dir() or not metadata_path.is_file():
        raise BridgeError(
            f"no hardened runtime artifact for {version}; run: {sys.argv[0]} prepare --version {version}"
        )
    metadata = read_json(metadata_path)
    expected_patch = patch_sha(version)
    if metadata.get("patchSha256") != expected_patch:
        raise BridgeError(
            f"artifact {version} was built from patch {metadata.get('patchSha256')}, "
            f"but the canonical patch is {expected_patch}; rebuild it"
        )
    return runtime, metadata


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_archive_path(relative: object) -> bool:
    return (
        isinstance(relative, str)
        and bool(relative)
        and "\\" not in relative
        and not relative.startswith("/")
        and posixpath.normpath(relative) == relative
        and all(segment not in ("", ".", "..") for segment in relative.split("/"))
    )


def launcher_artifact(version: str) -> tuple[pathlib.Path, dict]:
    """Validate the launcher-only patch for an exact app version.

    The patch replaces whole launcher files inside app.asar. It is separate from
    the runtime patch so a launcher bug can be fixed without rebuilding Bun.
    """
    directory = LAUNCHER_ROOT / version
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise BridgeError(f"no launcher patch for {version}; expected {manifest_path}")
    manifest = read_json(manifest_path)
    if manifest.get("schemaVersion") != 1:
        raise BridgeError(f"{manifest_path} has unsupported schema {manifest.get('schemaVersion')}")
    if manifest.get("appVersion") != version:
        raise BridgeError(
            f"{manifest_path} targets app version {manifest.get('appVersion')}, expected {version}"
        )
    if manifest.get("marker") != LAUNCHER_MARKER:
        raise BridgeError(f"{manifest_path} is missing the launcher marker {LAUNCHER_MARKER}")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise BridgeError(f"{manifest_path} has no launcher file records")
    seen: set[str] = set()
    for index, file_record in enumerate(files):
        if not isinstance(file_record, dict):
            raise BridgeError(f"{manifest_path} has an invalid file record at index {index}")
        relative = file_record.get("path")
        if not _safe_archive_path(relative):
            raise BridgeError(f"{manifest_path} has an unsafe archive path: {relative!r}")
        if relative in seen:
            raise BridgeError(f"{manifest_path} has a duplicate archive path: {relative}")
        seen.add(relative)
        checksum = file_record.get("sha256")
        base_checksum = file_record.get("baseSha256")
        if not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum):
            raise BridgeError(f"{manifest_path} has an invalid patched hash for {relative}")
        if not isinstance(base_checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", base_checksum):
            raise BridgeError(f"{manifest_path} has an invalid base hash for {relative}")
        source = directory / "files" / relative
        if not source.is_file():
            raise BridgeError(f"launcher patch file is missing: {source}")
        if sha256_file(source) != checksum:
            raise BridgeError(f"launcher patch file does not match its manifest hash: {source}")
        if LAUNCHER_MARKER not in source.read_text(encoding="utf-8"):
            raise BridgeError(f"launcher patch file is missing {LAUNCHER_MARKER}: {source}")
    return directory, manifest


def launcher_status(
    app: pathlib.Path,
    manifest: dict | None = None,
) -> dict:
    version = app_version(app)
    directory: pathlib.Path | None = None
    if manifest is None:
        directory, manifest = launcher_artifact(version)
    results = []
    for file_record in manifest["files"]:
        current = asar_read(app, file_record["path"])
        digest = _sha256_text(current)
        results.append(
            {
                "path": file_record["path"],
                "patched": digest == file_record["sha256"],
                "base": digest == file_record["baseSha256"],
                "marker": LAUNCHER_MARKER in current,
            }
        )
    return {
        "version": version,
        "installed": all(item["patched"] and item["marker"] for item in results),
        "base": all(item["base"] for item in results),
        "files": results,
        "manifest": str(directory / "manifest.json") if directory else None,
    }


def existing_launcher_marker(app: pathlib.Path) -> dict | None:
    try:
        value = read_json(app / "Contents/Resources/hardening.json").get("launcher")
    except Exception:
        return None
    return value if isinstance(value, dict) else None


NODE_CANDIDATES = (
    pathlib.Path.home() / ".local" / "bin" / "node",
    pathlib.Path("/opt/homebrew/bin/node"),
    pathlib.Path("/usr/local/bin/node"),
)


def _usable_javascript_runtime(candidate: pathlib.Path) -> bool:
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        return False
    try:
        result = subprocess.run(
            [str(candidate), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return False
    return result.returncode == 0


def runner() -> str:
    """Return a JavaScript runtime for the bundled asar tool.

    LaunchAgents run with a minimal PATH, so `node` may be installed in
    ~/.local/bin without being discoverable by shutil.which. Try the common
    locations explicitly, then fall back to the hardened runtime's Bun. Never
    leave this to an undefined helper: a failure here aborts the install after
    the app has already quit.
    """
    candidates: list[pathlib.Path | str | None] = [
        shutil.which("node"),
        *NODE_CANDIDATES,
        ROOT / "versions" / "6.1.14" / "runtime" / "runtime" / "bun",
        APP_RUNTIME / "runtime" / "bun",
        ROOT / "tools" / "bun" / "1.4.0" / "bun",
        shutil.which("bun"),
    ]
    for value in candidates:
        if not value:
            continue
        candidate = pathlib.Path(value)
        if _usable_javascript_runtime(candidate):
            return str(candidate)
    raise BridgeError(
        "no usable Node.js or Bun runtime was found for app.asar operations; "
        "install Node.js or run `update-bridge.py prepare` to cache Bun"
    )


def asar_replace(app: pathlib.Path, relative: str, source: pathlib.Path) -> None:
    if not ASAR_TOOL.is_file():
        raise BridgeError(f"asar tool is missing: {ASAR_TOOL}")
    archive = app / "Contents/Resources/app.asar"
    if not archive.is_file():
        raise BridgeError(f"app.asar is missing: {archive}")
    run([runner(), str(ASAR_TOOL), "replace", str(archive), relative, str(source)], timeout=300)


def asar_read(app: pathlib.Path, relative: str) -> str:
    archive = app / "Contents/Resources/app.asar"
    result = run([runner(), str(ASAR_TOOL), "read", str(archive), relative], timeout=300)
    return result.stdout


def write_temp(content: str, suffix: str = ".cjs") -> pathlib.Path:
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=suffix, delete=False)
    try:
        handle.write(content)
        handle.flush()
        return pathlib.Path(handle.name)
    finally:
        handle.close()


def inject_guard(text: str) -> tuple[str, bool]:
    original = text
    early_tag = "// hardening preflight: before download"
    late_tag = "// hardening preflight: before worker"
    has_helper = GUARD_MARKER in text
    # Match the calls themselves so an app injected by an older bridge (which
    # had no tag comment) is upgraded without duplicating the early guard.
    has_early = "hardeningGuard(candidate.version)" in text
    has_late = "hardeningGuard(available.version)" in text
    if has_helper and has_early and has_late:
        return text, True
    if not has_helper:
        helper_anchor = "const UPDATE_CHECK_RETRY_DELAYS_MS = [60_000, 5 * 60_000, 30 * 60_000];\n"
        helper = f"""// {GUARD_MARKER}
function hardeningGuard(version) {{
  const script = path.join(os.homedir(), ".codex-chatgpt-web", "codex-web-patcher-7a2674e73e1833ac", "bin", "update-bridge.py");
  if (!fs.existsSync(script)) return {{ ok: false, message: `hardening bridge is missing: ${{script}}` }};
  const result = spawnSync("/usr/bin/python3", [script, "guard", "--version", version, "--require-idle"], {{
    encoding: "utf8",
    timeout: 60_000,
    env: {{ ...process.env, PYTHONNOUSERSITE: "1" }},
  }});
  if (result.error) return {{ ok: false, message: result.error.message }};
  if (result.status !== 0) {{
    const detail = `${{result.stderr || ""}}${{result.stdout || ""}}`.trim();
    return {{ ok: false, message: detail.slice(-2000) || `hardening guard exited with ${{result.status}}` }};
  }}
  return {{ ok: true }};
}}
"""
        if helper_anchor not in text:
            helper_anchor = "const MAX_REDIRECTS = 5;\n"
            if helper_anchor not in text:
                return original, False
        text = text.replace(helper_anchor, helper_anchor + helper, 1)

    if not has_early:
        install_anchor = "    const available = candidate;\n"
        early_guard = f"""    {early_tag}
    const hardening = hardeningGuard(candidate.version);
    if (!hardening.ok) {{
      throw new Error(`Update preflight failed for v${{candidate.version}}: ${{hardening.message}}`);
    }}
"""
        if install_anchor not in text:
            return original, False
        text = text.replace(install_anchor, early_guard + install_anchor, 1)

    if not has_late:
        spawn_anchor = "        const child = deps.spawnWorker(runtimeExecutable, workerPath, jobPath);\n"
        late_guard = f"""        {late_tag}
        const hardening = hardeningGuard(available.version);
        if (!hardening.ok) {{
          throw new Error(`Update preflight failed for v${{available.version}}: ${{hardening.message}}`);
        }}
"""
        if spawn_anchor not in text:
            return original, False
        text = text.replace(spawn_anchor, late_guard + spawn_anchor, 1)

    return text, True


def write_hardening_marker(
    app: pathlib.Path,
    version: str,
    metadata: dict,
    worker: pathlib.Path,
    guard: bool,
    launcher: dict | None = None,
) -> pathlib.Path:
    marker = app / "Contents/Resources/hardening.json"
    payload = {
        "schemaVersion": 1,
        "version": version,
        "bundleId": metadata["bundleId"],
        "patchSha256": metadata["patchSha256"],
        "workerSha256": sha256_file(worker),
        "guard": guard,
        "installedAt": utc_now(),
    }
    if launcher is not None:
        payload["launcher"] = launcher
    marker.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    return marker


def save_marker(app: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path | None]:
    marker = app / "Contents/Resources/hardening.json"
    # Keep the backup outside the bundle. A file inside Contents/Resources
    # would be sealed by codesign and then deleting it would invalidate the
    # freshly created signature.
    saved = BACKUPS / f"hardening.json-{stamp()}-{os.getpid()}.bak"
    BACKUPS.mkdir(parents=True, exist_ok=True)
    saved.unlink(missing_ok=True)
    if marker.exists():
        shutil.copy2(marker, saved)
        return marker, saved
    return marker, None


def restore_marker(marker: pathlib.Path, saved: pathlib.Path | None) -> None:
    marker.unlink(missing_ok=True)
    if saved is not None and saved.exists():
        shutil.copy2(saved, marker)
        saved.unlink(missing_ok=True)


def sign_app(app: pathlib.Path) -> None:
    extraction = subprocess.run(
        ["codesign", "-d", "--entitlements", ":-", str(app)],
        capture_output=True,
        text=True,
    )
    entitlements_text: str | None = None
    if extraction.returncode == 0 and "<plist" in extraction.stdout:
        entitlements_text = extraction.stdout
    elif ENTITLEMENTS.is_file() and ENTITLEMENTS.stat().st_size > 0:
        # A patched app.asar can invalidate the signature before the first
        # re-sign. Fall back to the last known-good launcher entitlements.
        entitlements_text = ENTITLEMENTS.read_text(encoding="utf-8")
    else:
        raise BridgeError(f"could not read entitlements from {app}: {extraction.stderr[-500:]}")

    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".plist", delete=False)
    try:
        handle.write(entitlements_text)
        handle.flush()
        handle.close()
        result = subprocess.run(
            [
                "codesign",
                "--force",
                "--sign",
                "-",
                "--options",
                "runtime",
                "--identifier",
                BUNDLE_ID,
                "--entitlements",
                handle.name,
                str(app),
            ],
            capture_output=True,
            text=True,
        )
    finally:
        handle.close()
        pathlib.Path(handle.name).unlink(missing_ok=True)
    if result.returncode != 0:
        raise BridgeError(f"codesign failed for {app}: {result.stderr[-2000:]}")
    verify = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)],
        capture_output=True,
        text=True,
    )
    if verify.returncode != 0:
        raise BridgeError(f"codesign verification failed for {app}: {verify.stderr[-2000:]}")
    subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(app)], check=False)


def replace_runtime(
    target: pathlib.Path,
    source: pathlib.Path,
    *,
    label: str,
    keep_backup: bool = True,
) -> pathlib.Path | None:
    verify_runtime(source, full=True)
    moment = stamp()
    temporary = target.with_name(f"{target.name}.hardening-{moment}-{os.getpid()}")
    backup = BACKUPS / f"{label}-{moment}-{os.getpid()}"
    if temporary.exists():
        shutil.rmtree(temporary, ignore_errors=True)
    try:
        shutil.copytree(source, temporary, symlinks=True)
        verify_runtime(temporary, full=True)
        previous: pathlib.Path | None = None
        if target.exists():
            if keep_backup:
                BACKUPS.mkdir(parents=True, exist_ok=True)
                if backup.exists():
                    shutil.rmtree(backup, ignore_errors=True)
                shutil.move(str(target), str(backup))
                previous = backup
            else:
                shutil.rmtree(target)
        try:
            temporary.rename(target)
            verify_runtime(target, full=True)
        except BaseException:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            if previous is not None and previous.exists():
                shutil.move(str(previous), str(target))
            raise
        if previous is not None and not keep_backup:
            shutil.rmtree(previous, ignore_errors=True)
        return previous
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def backup_asar(app: pathlib.Path, label: str) -> pathlib.Path:
    BACKUPS.mkdir(parents=True, exist_ok=True)
    destination = BACKUPS / f"{label}-{stamp()}-{os.getpid()}"
    destination.mkdir(mode=0o700)
    archive = app / "Contents/Resources/app.asar"
    if not archive.is_file():
        raise BridgeError(f"app.asar is missing: {archive}")
    shutil.copy2(archive, destination / "app.asar")
    unpacked = app / "Contents/Resources/app.asar.unpacked"
    if unpacked.is_dir():
        shutil.copytree(unpacked, destination / "app.asar.unpacked", symlinks=True)
    log(f"backed up app.asar to {destination}")
    return destination


def restore_asar(app: pathlib.Path, backup: pathlib.Path) -> None:
    archive = app / "Contents/Resources/app.asar"
    shutil.copy2(backup / "app.asar", archive)
    unpacked = app / "Contents/Resources/app.asar.unpacked"
    saved = backup / "app.asar.unpacked"
    if saved.is_dir():
        shutil.rmtree(unpacked, ignore_errors=True)
        shutil.copytree(saved, unpacked, symlinks=True)
    log(f"restored app.asar from {backup}")


def asar_header_hash(archive: pathlib.Path) -> str:
    """Return the value Electron expects in ElectronAsarIntegrity.

    app-builder-lib hashes only the JSON header string, not the pickle or the
    whole archive. The file layout is:

      8-byte size pickle (payload size 4, then header-pickle size)
      4-byte header-pickle payload size
      4-byte JSON string length
      JSON header (padded to a 4-byte boundary)

    Hashing the wrong slice (for example including the length prefix) produces
    a value that Electron rejects at launch.
    """
    data = archive.read_bytes()
    if len(data) < 16:
        raise BridgeError(f"app.asar is too short to read a header: {archive}")
    header_size = struct.unpack("<I", data[4:8])[0]
    if header_size < 8 or 8 + header_size > len(data):
        raise BridgeError(f"app.asar has an invalid header size ({header_size}): {archive}")
    header_pickle = data[8 : 8 + header_size]
    payload_size = struct.unpack("<I", header_pickle[0:4])[0]
    string_size = struct.unpack("<I", header_pickle[4:8])[0]
    if payload_size < 4 or string_size > payload_size - 4:
        raise BridgeError(f"app.asar has an invalid header payload: {archive}")
    if 8 + string_size > len(header_pickle):
        raise BridgeError(f"app.asar header string is truncated: {archive}")
    return hashlib.sha256(header_pickle[8 : 8 + string_size]).hexdigest()


def asar_integrity_entry(app: pathlib.Path) -> tuple[pathlib.Path, dict, dict | None]:
    plist_path = app / "Contents/Info.plist"
    try:
        plist = plistlib.loads(plist_path.read_bytes())
    except Exception as error:
        raise BridgeError(f"could not read {plist_path}: {error}") from error
    integrity = plist.get("ElectronAsarIntegrity")
    if integrity is None:
        return plist_path, plist, None
    if not isinstance(integrity, dict):
        raise BridgeError(f"ElectronAsarIntegrity in {plist_path} is not a dictionary")
    entry = integrity.get("Resources/app.asar")
    if entry is None:
        return plist_path, plist, None
    if not isinstance(entry, dict):
        raise BridgeError(f"ElectronAsarIntegrity for app.asar in {plist_path} is not a dictionary")
    return plist_path, plist, entry


def update_asar_integrity(app: pathlib.Path) -> dict:
    """Keep Electron's ASAR header hash in sync after patching app.asar."""
    plist_path, plist, entry = asar_integrity_entry(app)
    if entry is None:
        return {"present": False, "updated": False}
    algorithm = entry.get("algorithm", "SHA256")
    if algorithm != "SHA256":
        raise BridgeError(f"unsupported ElectronAsarIntegrity algorithm: {algorithm!r}")
    digest = asar_header_hash(app / "Contents/Resources/app.asar")
    if entry.get("hash") == digest:
        return {"present": True, "updated": False, "hash": digest}
    entry["algorithm"] = "SHA256"
    entry["hash"] = digest
    try:
        plist_path.write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_XML))
    except Exception as error:
        raise BridgeError(f"could not update {plist_path}: {error}") from error
    return {"present": True, "updated": True, "hash": digest}


def verify_asar_integrity(app: pathlib.Path) -> dict:
    _, _, entry = asar_integrity_entry(app)
    if entry is None:
        return {"present": False, "ok": True}
    expected = asar_header_hash(app / "Contents/Resources/app.asar")
    actual = entry.get("hash")
    return {
        "present": True,
        "ok": entry.get("algorithm", "SHA256") == "SHA256" and actual == expected,
        "algorithm": entry.get("algorithm", "SHA256"),
        "expected": expected,
        "actual": actual,
    }


def patch_asar(app: pathlib.Path, worker: pathlib.Path) -> dict:
    if not worker.is_file():
        raise BridgeError(f"hardened worker is missing: {worker}")
    asar_replace(app, "electron/update-worker.cjs", worker)
    installed_worker = asar_read(app, "electron/update-worker.cjs")
    if WORKER_MARKER not in installed_worker:
        raise BridgeError("installed update worker is missing the hardening marker")

    guard_ok = False
    guard_note = ""
    try:
        text = asar_read(app, "electron/update.cjs")
        injected, guard_ok = inject_guard(text)
        if guard_ok and injected != text:
            temporary = write_temp(injected, ".cjs")
            try:
                asar_replace(app, "electron/update.cjs", temporary)
            finally:
                temporary.unlink(missing_ok=True)
        if guard_ok:
            check = asar_read(app, "electron/update.cjs")
            guard_ok = GUARD_MARKER in check
        if not guard_ok:
            guard_note = "update.cjs preflight anchor was not found; the worker gate still protects the update"
    except BridgeError as error:
        guard_note = f"update.cjs preflight could not be installed: {error}"
    return {"worker": True, "guard": guard_ok, "guard_note": guard_note}


def patch_launcher(app: pathlib.Path, version: str) -> dict:
    """Replace launcher JS inside app.asar without rebuilding the runtime.

    This is deliberately narrower than `apply`: it never touches the runtime,
    the update worker, or user data. It refuses to apply to a build whose
    browser-host.cjs is not the exact base the patch was reviewed against.
    """
    if not app.is_dir():
        raise BridgeError(f"application is missing: {app}")
    found_version = app_version(app)
    if found_version != version:
        raise BridgeError(f"{app} is version {found_version}, expected {version}")
    directory, manifest = launcher_artifact(version)
    status = launcher_status(app, manifest)
    if status["installed"]:
        return {"version": version, "status": "already-installed", **status, "app": str(app)}
    if not status["base"]:
        raise BridgeError(
            "the installed launcher files do not match the supported base for this patch; "
            "refusing to overwrite a different build"
        )
    worker = asar_read(app, "electron/update-worker.cjs")
    if WORKER_MARKER not in worker:
        raise BridgeError("install the hardened update bridge before the launcher patch")
    guard = GUARD_MARKER in asar_read(app, "electron/update.cjs")
    metadata = artifact(version)[1]

    backup = backup_asar(app, "app-asar-before-launcher")
    info_plist = app / "Contents/Info.plist"
    info_plist_before = info_plist.read_bytes()
    marker, saved_marker = save_marker(app)
    try:
        for file_record in manifest["files"]:
            source = directory / "files" / file_record["path"]
            asar_replace(app, file_record["path"], source)
            installed = asar_read(app, file_record["path"])
            if _sha256_text(installed) != file_record["sha256"]:
                raise BridgeError(f"launcher patch verification failed for {file_record['path']}")
            if LAUNCHER_MARKER not in installed:
                raise BridgeError(f"launcher marker is missing after patching {file_record['path']}")
        integrity = update_asar_integrity(app)
        launcher_meta = {
            "version": version,
            "manifestSha256": sha256_file(directory / "manifest.json"),
            "files": [
                {"path": file_record["path"], "sha256": file_record["sha256"]}
                for file_record in manifest["files"]
            ],
            "installedAt": utc_now(),
        }
        write_hardening_marker(
            app, version, metadata, WORKER_TEMPLATE, guard, launcher=launcher_meta
        )
        sign_app(app)
        verified = launcher_status(app, manifest)
        if not verified["installed"]:
            raise BridgeError(f"launcher patch is missing after signing: {verified}")
        verified_integrity = verify_asar_integrity(app)
        if not verified_integrity["ok"]:
            raise BridgeError(
                f"launcher-patched app has a stale ElectronAsarIntegrity: {verified_integrity}"
            )
    except BaseException:
        restore_asar(app, backup)
        restore_marker(marker, saved_marker)
        try:
            info_plist.write_bytes(info_plist_before)
            sign_app(app)
        finally:
            record("launcher-install-failed", version=version, backup=str(backup))
        raise
    if saved_marker is not None:
        saved_marker.unlink(missing_ok=True)
    record(
        "launcher-installed",
        version=version,
        backup=str(backup),
        integrity=verified_integrity,
        integrityUpdated=integrity["updated"],
    )
    log(f"installed launcher session patch {version} into {app}")
    return {
        "version": version,
        "status": "installed",
        **verified,
        "integrity": verified_integrity,
        "integrityUpdated": integrity["updated"],
        "app": str(app),
        "backup": str(backup),
        "launcher": launcher_meta,
    }


def patch_app_bundle(
    app: pathlib.Path,
    version: str,
    worker: pathlib.Path,
    *,
    runtime_source: pathlib.Path | None = None,
    keep_backup: bool = True,
) -> dict:
    if not app.is_dir():
        raise BridgeError(f"application is missing: {app}")
    found_version = app_version(app)
    if found_version != version:
        raise BridgeError(f"{app} is version {found_version}, expected {version}")
    source = runtime_source or artifact(version)[0]
    verify_runtime(source, version=version, full=True)

    # A whole-app update replaces the launcher too; restore the reviewed session fix.
    launcher_directory, launcher_manifest = launcher_artifact(version)
    launcher_state = launcher_status(app, launcher_manifest)
    if not launcher_state["base"] and not launcher_state["installed"]:
        raise BridgeError(f"launcher {version} does not match the supported patch base")

    replace_runtime(app / "Contents/Resources/runtime", source, label=f"{version}-app-runtime", keep_backup=keep_backup)
    asar = patch_asar(app, worker)
    if launcher_state["base"]:
        for item in launcher_manifest["files"]:
            asar_replace(app, item["path"], launcher_directory / "files" / item["path"])
    launcher_state = launcher_status(app, launcher_manifest)
    if not launcher_state["installed"]:
        raise BridgeError(f"launcher session patch failed verification: {launcher_state}")
    integrity = update_asar_integrity(app)
    metadata = artifact(version)[1]

    launcher_meta = {
        "version": version,
        "manifestSha256": sha256_file(launcher_directory / "manifest.json"),
        "files": [{"path": item["path"], "sha256": item["sha256"]} for item in launcher_manifest["files"]],
        "installedAt": utc_now(),
    }
    marker = write_hardening_marker(
        app, version, metadata, worker, asar["guard"], launcher=launcher_meta
    )
    sign_app(app)
    verify_runtime(app / "Contents/Resources/runtime", version=version, bundle=metadata["bundleId"], full=True)
    if WORKER_MARKER not in asar_read(app, "electron/update-worker.cjs"):
        raise BridgeError("staged worker marker disappeared after signing")
    verified_integrity = verify_asar_integrity(app)
    if not verified_integrity["ok"]:
        raise BridgeError(f"staged app has a stale ElectronAsarIntegrity: {verified_integrity}")
    if not launcher_status(app, launcher_manifest)["installed"]:
        raise BridgeError("staged launcher session patch disappeared after signing")
    log(f"staged hardened app {app} for {version}")
    return {
        "version": version,
        "bundleId": metadata["bundleId"],
        "worker": True,
        "guard": asar["guard"],
        "guard_note": asar["guard_note"],
        "integrity": verified_integrity,
        "integrityUpdated": integrity["updated"],
        "marker": str(marker),
    }


def alive_commands() -> list[str]:
    patterns = [
        str(APP / "Contents/MacOS/Codex Web GPT"),
        "app/cli.js serve",
        "browser-helper.cjs",
    ]
    found: list[str] = []
    for pattern in patterns:
        result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
        for value in result.stdout.split():
            if value.isdigit() and int(value) != os.getpid():
                found.append(f"{pattern} -> {value}")
    return sorted(set(found))


def launch_environment() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    return env


def health() -> dict | None:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def busy(health_payload: dict | None) -> bool:
    if not health_payload:
        return False
    return bool(health_payload.get("active_http_turns") or health_payload.get("active_browser_turns"))


def hermes_turn_leases() -> tuple[bool, str]:
    """Detect a logical Hermes turn that is between provider requests.

    `/healthz` only sees in-flight requests. During tool execution the counts
    drop to zero, so a quiet window alone could interrupt an active turn. The
    Hermes gateway records one expiring lease per logical turn; require all of
    them to be gone as well.
    """
    if not HERMES_DB.is_file():
        return False, "hermes state.db is absent"
    try:
        connection = sqlite3.connect(f"file:{HERMES_DB}?mode=ro", uri=True, timeout=2)
        try:
            rows = connection.execute(
                "SELECT conversation_id, expires_at FROM session_turn_leases WHERE expires_at > ?",
                (time.time(),),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error).lower():
            return False, "hermes does not expose turn leases"
        # A locked or unreadable database means we cannot prove idle. Wait
        # rather than risk cancelling a turn.
        return True, f"could not read hermes turn leases: {error}"
    except Exception as error:
        return True, f"could not read hermes turn leases: {error}"
    if not rows:
        return False, "no unexpired hermes turn lease"
    remaining = ", ".join(
        f"{str(row[0])[:24]} ({max(0, int(row[1] - time.time()))}s)" for row in rows
    )
    return True, f"active hermes turn lease: {remaining}"


def control_token() -> str | None:
    config = CORE_HOME / "config.json"
    try:
        value = read_json(config).get("controlToken")
        return value if isinstance(value, str) and value else None
    except Exception:
        return None


def post_admin(path: str) -> dict:
    token = control_token()
    if not token:
        raise BridgeError("control token is unavailable")
    request = urllib.request.Request(
        f"http://127.0.0.1:17841{path}",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_until_idle(timeout_seconds: int, quiet_seconds: int = 20, drain: bool = True) -> dict:
    deadline = time.time() + timeout_seconds
    quiet_since: float | None = None
    while time.time() < deadline:
        payload = health()
        hermes_active, hermes_note = hermes_turn_leases()
        if payload and not busy(payload) and not hermes_active:
            quiet_since = quiet_since or time.time()
            if time.time() - quiet_since >= quiet_seconds:
                if not drain:
                    return payload
                try:
                    drained = post_admin("/admin/drain")
                    log(f"drained: {json.dumps(drained)}")
                except Exception as error:
                    # Without a successful drain, /admin/shutdown will refuse
                    # and a late turn could be interrupted. Keep waiting.
                    log(f"drain failed: {error}")
                    quiet_since = None
                    time.sleep(5)
                    continue
                # Close the race between the idle observation and drain.
                time.sleep(3)
                confirmed = health()
                confirmed_hermes, confirmed_note = hermes_turn_leases()
                if confirmed and not busy(confirmed) and not confirmed_hermes:
                    return confirmed
                # A turn started during the drain window. Accept it and wait
                # for another quiet period instead of interrupting it.
                try:
                    resumed = post_admin("/admin/resume")
                    log(
                        "resumed after a late turn appeared: "
                        f"{json.dumps(resumed)}; {confirmed_note or hermes_note}"
                    )
                except Exception as error:
                    log(f"resume failed: {error}")
                    raise BridgeError(
                        "a turn started during the drain window and the daemon could not be resumed"
                    ) from error
                quiet_since = None
        else:
            quiet_since = None
        time.sleep(5)
    raise BridgeError(f"app did not become idle within {timeout_seconds}s")


def main_app_pids() -> list[int]:
    pattern = f"^{re.escape(str(APP / 'Contents/MacOS/Codex Web GPT'))}$"
    result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    pids = [int(value) for value in result.stdout.split() if value.isdigit()]
    if pids:
        return pids
    # The main process writes its PID when it owns the browser host. Verify the
    # command line before trusting a stale PID.
    try:
        pid = int(read_json(CORE_HOME / "runtime" / "launcher-browser.json").get("pid"))
        command = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True
        ).stdout.strip()
        if command == str(APP / "Contents/MacOS/Codex Web GPT"):
            return [pid]
    except Exception:
        pass
    return []


def quit_app(timeout_seconds: int = 90) -> None:
    if not alive_commands():
        return
    # The caller has already drained the daemon. Stop it cleanly so the app
    # supervisor does not race the bundle replacement, then quit the UI.
    try:
        post_admin("/admin/shutdown")
    except urllib.error.HTTPError as error:
        if error.code == 409:
            try:
                post_admin("/admin/resume")
            except Exception:
                pass
            raise BridgeError(
                "a turn became active before shutdown; aborted without interrupting it"
            ) from error
        log(f"daemon shutdown request failed (continuing with app quit): {error}")
    except Exception as error:
        log(f"daemon shutdown request failed (continuing with app quit): {error}")

    # SIGTERM is handled by main.cjs as a graceful requestQuit: it stops the
    # supervisor, persists the browser session, and quits. Avoid AppleScript so
    # a LaunchAgent does not depend on an Automation (TCC) prompt.
    pids = main_app_pids()
    for pid in pids:
        log(f"sending SIGTERM to launcher PID {pid}")
        subprocess.run(["kill", "-TERM", str(pid)], check=False)
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not alive_commands():
            return
        time.sleep(1)
    for pattern in (str(APP / "Contents/MacOS/Codex Web GPT"), "app/cli.js serve", "browser-helper.cjs"):
        result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
        for value in result.stdout.split():
            if value.isdigit():
                subprocess.run(["kill", "-TERM", value], check=False)
    deadline = time.time() + 30
    while time.time() < deadline:
        if not alive_commands():
            return
        time.sleep(1)
    try:
        post_admin("/admin/resume")
        log("resumed the daemon after the app failed to quit")
    except Exception as error:
        log(f"could not resume the daemon after the app failed to quit: {error}")
    raise BridgeError("app did not exit")


def launch_app() -> None:
    result = subprocess.run(["open", "-a", str(APP)], env=launch_environment(), capture_output=True, text=True)
    if result.returncode != 0:
        raise BridgeError(f"could not launch the app: {result.stderr[-1000:]}")
    deadline = time.time() + 90
    while time.time() < deadline:
        if health():
            return
        time.sleep(1)
    raise BridgeError("app did not become healthy after launch")


def latest_release() -> dict:
    request = urllib.request.Request(
        RELEASE_API_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "codex-web-gpt-hardening-bridge"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.loads(response.read().decode("utf-8"))
    tag = str(release.get("tag_name") or "")
    if not tag:
        raise BridgeError("latest release has no tag")
    return release


def ensure_bun(required: str | None) -> str:
    if required:
        cached = ROOT / "tools" / "bun" / required / "bun"
        if cached.is_file() and os.access(cached, os.X_OK):
            return str(cached)
    candidates: list[pathlib.Path] = []
    for directory in sorted((CORE_HOME / "versions").glob("*/runtime/bun")):
        candidates.append(directory)
    system = shutil.which("bun")
    if system:
        candidates.append(pathlib.Path(system))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        result = subprocess.run([str(candidate), "--version"], capture_output=True, text=True)
        if result.returncode == 0 and (required is None or result.stdout.strip() == required):
            return str(candidate)
    if not required:
        raise BridgeError("no Bun runtime is available")
    destination = ROOT / "tools" / "bun" / required
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / "bun.zip"
    arch = "aarch64" if os.uname().machine == "arm64" else "x64"
    url = f"https://github.com/oven-sh/bun/releases/download/bun-v{required}/bun-darwin-{arch}.zip"
    log(f"downloading Bun {required}: {url}")
    urllib.request.urlretrieve(url, archive)
    extract = destination / "extract"
    shutil.rmtree(extract, ignore_errors=True)
    extract.mkdir(parents=True)
    subprocess.run(["/usr/bin/ditto", "-x", "-k", str(archive), str(extract)], check=True)
    found = next(extract.glob("bun-*/bun"), None)
    if not found:
        raise BridgeError(f"downloaded Bun archive did not contain bun: {archive}")
    shutil.copy2(found, destination / "bun")
    (destination / "bun").chmod(0o755)
    result = subprocess.run([str(destination / "bun"), "--version"], capture_output=True, text=True)
    if result.returncode != 0 or result.stdout.strip() != required:
        raise BridgeError(f"downloaded Bun reported {result.stdout.strip()!r}, expected {required}")
    return str(destination / "bun")


def source_checkout(version: str, source_dir: pathlib.Path | None) -> pathlib.Path:
    checkout = source_dir or (ROOT / "source" / f"v{version}")
    if source_dir is not None and (source_dir / ".git").exists():
        # A caller-owned checkout may contain unfinished changes. Never discard
        # them during an artifact rebuild, even if the runtime patch is stale.
        changes = subprocess.run(
            ["git", "-C", str(source_dir), "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True, text=True, check=True,
        )
        if changes.stdout.strip():
            raise BridgeError(
                f"source checkout has local changes: {source_dir}; "
                "prepare without --source-dir to build in a separate checkout"
            )
        run(["git", "-C", source_dir, "fetch", "--tags", "--force", "origin"], timeout=900)
        run(["git", "-C", source_dir, "checkout", "--force", f"v{version}"], timeout=300)
        run(["git", "-C", source_dir, "reset", "--hard", f"v{version}"], timeout=300)
        # Preserve ignored personal files in explicitly supplied repositories.
        run(["git", "-C", source_dir, "clean", "-fd"], timeout=300)
    elif source_dir is None:
        if checkout.exists():
            shutil.rmtree(checkout, ignore_errors=True)
        checkout.parent.mkdir(parents=True, exist_ok=True)
        run(
            [
                "git",
                "clone",
                "--no-checkout",
                f"https://github.com/{REPOSITORY}.git",
                str(checkout),
            ],
            timeout=1_800,
        )
        run(["git", "-C", checkout, "checkout", f"v{version}"], timeout=900)
    package = read_json(checkout / "package.json")
    if str(package.get("version")) != version:
        raise BridgeError(f"{checkout} is package version {package.get('version')}, expected {version}")
    return checkout


def patch_state(checkout: pathlib.Path) -> str:
    """Return not-applied, already-applied, or conflict for the canonical patch."""
    patch = patch_for(str(read_json(checkout / "package.json")["version"]))
    if not patch.is_file():
        raise BridgeError(f"canonical patch is missing: {patch}")
    check = subprocess.run(
        ["git", "-C", str(checkout), "apply", "--check", str(patch)],
        capture_output=True,
        text=True,
    )
    if check.returncode == 0:
        return "not-applied"
    reverse = subprocess.run(
        ["git", "-C", str(checkout), "apply", "--reverse", "--check", str(patch)],
        capture_output=True,
        text=True,
    )
    if reverse.returncode == 0:
        return "already-applied"
    return "conflict"


def apply_patch(checkout: pathlib.Path, *, three_way: bool = True) -> str:
    patch = patch_for(str(read_json(checkout / "package.json")["version"]))
    state = patch_state(checkout)
    if state == "already-applied":
        return state
    if state == "not-applied":
        run(["git", "-C", checkout, "apply", str(patch)], timeout=600)
        return "applied"
    if not three_way:
        raise BridgeError(
            "the patch does not apply cleanly to this source checkout; "
            "resolve the differences or pass --three-way"
        )
    result = run(["git", "-C", checkout, "apply", "--3way", str(patch)], timeout=600, check=False)
    conflicts = subprocess.run(
        ["git", "-C", str(checkout), "diff", "--name-only", "--diff-filter=U"],
        capture_output=True,
        text=True,
    )
    detail = conflicts.stdout.strip()
    if result.returncode != 0 or detail:
        if not detail:
            detail = f"git apply --3way exited {result.returncode}: {result.stdout[-3000:]}{result.stderr[-3000:]}"
        raise BridgeError(
            "the patch has merge conflicts on this release:\n"
            + detail
            + "\nResolve them on a branch, update the canonical patch, and rerun prepare."
        )
    return "three-way"


def apply_patch_to_source(args: argparse.Namespace) -> dict:
    checkout = pathlib.Path(args.source).resolve()
    if not (checkout / ".git").exists():
        raise BridgeError(f"not a Git checkout: {checkout}")
    package_path = checkout / "package.json"
    if not package_path.is_file():
        raise BridgeError(f"package.json is missing: {package_path}")
    package = read_json(package_path)
    version = str(args.version or package.get("version") or "")
    if not version:
        raise BridgeError(f"could not determine the source version from {package_path}")
    if str(package.get("version")) != version:
        raise BridgeError(
            f"{checkout} is package version {package.get('version')}, expected {version}"
        )

    state = patch_state(checkout)
    if args.check:
        if state == "conflict":
            raise BridgeError(
                f"the patch does not apply cleanly to {checkout}; "
                "no files were changed"
            )
        return {
            "version": version,
            "source": str(checkout),
            "status": state,
            "changed": False,
            "patch": str(patch_for(version)),
            "patchSha256": patch_sha(version),
        }

    application = apply_patch(checkout, three_way=args.three_way)
    for required in sorted(PATCH_REQUIRED_FILES):
        if not (checkout / required).is_file():
            raise BridgeError(f"required hardening file is missing after patching: {required}")
    if application != "already-applied":
        changed = subprocess.run(
            ["git", "-C", str(checkout), "diff", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        missing = sorted(PATCH_REQUIRED_FILES - set(changed))
        if missing:
            raise BridgeError(
                "the patch applied but did not change required files: " + ", ".join(missing)
            )
    else:
        changed = []
    return {
        "version": version,
        "source": str(checkout),
        "status": application,
        "changed": bool(changed),
        "files": changed,
        "patch": str(patch_for(version)),
        "patchSha256": patch_sha(version),
    }


def prepare(args: argparse.Namespace) -> dict:
    version = args.version
    runtime, metadata = None, None
    if not args.force:
        try:
            runtime, metadata = artifact(version)
            verify_runtime(runtime, version=version, full=True)
            log(f"artifact {version} already exists ({metadata.get('bundleId')})")
            return {"version": version, "status": "already-prepared", **metadata}
        except BridgeError:
            pass

    checkout = source_checkout(version, pathlib.Path(args.source_dir).resolve() if args.source_dir else None)
    application = apply_patch(checkout)
    if application != "already-applied":
        changed = subprocess.run(
            ["git", "-C", str(checkout), "diff", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        missing = sorted(PATCH_REQUIRED_FILES - set(changed))
        if missing:
            raise BridgeError(
                "the patch applied but did not change required files: " + ", ".join(missing)
            )
    for required in ("src/adapters/chatgpt-web/browser-worker.ts", "tests/browser-turn-binding.test.ts"):
        if not (checkout / required).is_file():
            raise BridgeError(f"required hardening file is missing after patching: {required}")
    package = read_json(checkout / "package.json")
    manager = str(package.get("packageManager") or "")
    match = re.fullmatch(r"bun@(\d+\.\d+\.\d+)", manager)
    bun = ensure_bun(match.group(1) if match else args.bun)
    env = {
        "CODEX_CHATGPT_WEB_EMBEDDED_BUN": bun,
        "BUN_INSTALL_CACHE_DIR": str(ROOT / "tools" / "bun-cache"),
    }
    run([bun, "install", "--frozen-lockfile"], cwd=checkout, env=env, timeout=1_800)
    launcher = checkout / "launcher"
    if (launcher / "package.json").is_file():
        # The runtime build includes launcher licenses; only its production
        # dependencies are needed, and installing them avoids downloading Electron.
        run(
            [bun, "install", "--production", "--frozen-lockfile"],
            cwd=launcher,
            env=env,
            timeout=1_800,
        )
    if not args.skip_tests:
        run([bun, "run", "typecheck"], cwd=checkout, env=env, timeout=1_800)
        tests = [name for name in TARGET_TESTS if (checkout / name).is_file()]
        if tests:
            run([bun, "test", *tests], cwd=checkout, env=env, timeout=1_800)
    output = checkout / "dist" / f"runtime-hardened-{version}"
    shutil.rmtree(output, ignore_errors=True)
    run([bun, "run", "scripts/build-runtime-bundle.ts", str(output)], cwd=checkout, env=env, timeout=3_600)
    identity = verify_runtime(output, version=version, full=True)

    destination = VERSIONS / version
    destination.mkdir(parents=True, exist_ok=True)
    temporary = destination / f"runtime.tmp-{os.getpid()}"
    shutil.rmtree(temporary, ignore_errors=True)
    shutil.copytree(output, temporary, symlinks=True)
    verify_runtime(temporary, version=version, full=True)
    current = destination / "runtime"
    if current.exists():
        shutil.rmtree(current)
    temporary.rename(current)
    metadata = {
        "version": version,
        "sourceTag": f"v{version}",
        "sourceCommit": subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip(),
        "patchSha256": patch_sha(version),
        "patchMode": application,
        "bundleId": identity["bundleId"],
        "fileCount": len(identity["files"]),
        "bunVersion": identity["bunVersion"],
        "builtAt": utc_now(),
        "sourceDir": str(checkout),
        "tests": "skipped" if args.skip_tests else TARGET_TESTS,
    }
    (destination / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    verify_runtime(destination / "runtime", version=version, full=True)
    record("prepared", version=version, bundleId=identity["bundleId"])
    log(f"prepared hardened runtime {version} {identity['bundleId']}")
    return metadata


def guard(args: argparse.Namespace) -> dict:
    runtime, metadata = artifact(args.version)
    identity = verify_runtime(runtime, version=args.version, bundle=metadata.get("bundleId"), full=True)
    launcher_artifact(args.version)
    if not WORKER_TEMPLATE.is_file():
        raise BridgeError(f"hardened worker is missing: {WORKER_TEMPLATE}")
    if not ASAR_TOOL.is_file():
        raise BridgeError(f"asar tool is missing: {ASAR_TOOL}")
    if getattr(args, "require_idle", False):
        payload = health()
        if payload and busy(payload):
            raise BridgeError(
                "Codex Web GPT has an active turn; install the update after it finishes "
                f"(http={payload.get('active_http_turns', 0)}, "
                f"browser={payload.get('active_browser_turns', 0)})"
            )
        hermes_active, hermes_note = hermes_turn_leases()
        if hermes_active:
            raise BridgeError(
                "an active Hermes turn is present; install the update after it finishes "
                f"({hermes_note})"
            )
    return {
        "version": args.version,
        "bundleId": identity["bundleId"],
        "fileCount": len(identity["files"]),
        "patchSha256": metadata["patchSha256"],
        "artifact": str(runtime),
    }


def stage(args: argparse.Namespace) -> dict:
    app = pathlib.Path(args.app).resolve()
    worker = pathlib.Path(args.worker).resolve() if args.worker else WORKER_TEMPLATE
    runtime, _ = artifact(args.version)
    return patch_app_bundle(
        app,
        args.version,
        worker,
        runtime_source=runtime,
        keep_backup=False,
    )


def patch_installed_app(version: str, *, replace_installed: bool) -> dict:
    installed_version = app_version(APP)
    if installed_version != version:
        # The app may have been replaced by an official update while this
        # process was waiting for idle. Never downgrade or mix runtimes.
        raise BridgeError(
            f"installed app is version {installed_version}, expected {version}; refusing to patch"
        )
    runtime, metadata = artifact(version)
    runtime_ok = False
    try:
        verify_runtime(APP_RUNTIME, version=version, bundle=metadata["bundleId"], full=True)
        runtime_ok = True
    except BridgeError as error:
        log(f"installed app runtime needs repair: {error}")
    if not runtime_ok:
        replace_runtime(APP_RUNTIME, runtime, label=f"{version}-app-runtime", keep_backup=True)

    backup = backup_asar(APP, "app-asar-before-bridge")
    info_plist = APP / "Contents/Info.plist"
    info_plist_before = info_plist.read_bytes()
    marker, saved_marker = save_marker(APP)
    try:
        result = patch_asar(APP, WORKER_TEMPLATE)
        if not result["worker"]:
            raise BridgeError("could not install the hardened update worker")
        integrity = update_asar_integrity(APP)
        write_hardening_marker(
            APP,
            version,
            metadata,
            WORKER_TEMPLATE,
            result["guard"],
            launcher=existing_launcher_marker(APP),
        )
        sign_app(APP)
        verify_runtime(APP_RUNTIME, version=version, bundle=metadata["bundleId"], full=True)
        if WORKER_MARKER not in asar_read(APP, "electron/update-worker.cjs"):
            raise BridgeError("worker marker is missing after signing")
        verified_integrity = verify_asar_integrity(APP)
        if not verified_integrity["ok"]:
            raise BridgeError(f"installed app has a stale ElectronAsarIntegrity: {verified_integrity}")
        if not result["guard"]:
            # The worker is the authoritative gate. A changed update.cjs anchor
            # must not prevent installing the worker itself.
            log(
                "update preflight was not installed; worker gate remains active: "
                f"{result['guard_note']}"
            )
        installed_path: pathlib.Path | None = None
        if replace_installed:
            installed_path = CORE_HOME / "versions" / f"{version}-darwin-arm64"
            replace_runtime(installed_path, runtime, label=f"{version}-installed-runtime", keep_backup=True)
            verify_runtime(installed_path, version=version, bundle=metadata["bundleId"], full=True)
    except BaseException:
        restore_asar(APP, backup)
        restore_marker(marker, saved_marker)
        try:
            info_plist.write_bytes(info_plist_before)
            sign_app(APP)
        finally:
            record("bridge-install-failed", version=version, backup=str(backup))
        raise
    if saved_marker is not None:
        saved_marker.unlink(missing_ok=True)
    record(
        "bridge-installed",
        version=version,
        bundleId=metadata["bundleId"],
        guard=result["guard"],
        backup=str(backup),
        installed=str(installed_path) if installed_path else None,
        integrity=verified_integrity,
        integrityUpdated=integrity["updated"],
    )
    return {
        "version": version,
        "bundleId": metadata["bundleId"],
        **result,
        "integrity": verified_integrity,
        "integrityUpdated": integrity["updated"],
        "app": str(APP),
        "backup": str(backup),
        "installed": str(installed_path) if installed_path else None,
    }


def apply_to_app(args: argparse.Namespace) -> dict:
    version = args.version or app_version(APP)
    did_quit = False
    if args.wait_idle:
        wait_until_idle(args.idle_timeout, args.quiet_seconds, drain=args.restart)
        if args.restart and alive_commands():
            quit_app()
            did_quit = True
    running = alive_commands()
    if running:
        raise BridgeError("Codex Web GPT is still running:\n" + "\n".join(running))
    try:
        result = patch_installed_app(version, replace_installed=True)
        if args.launch or args.restart:
            launch_app()
            did_quit = False
        return result
    except BaseException:
        if did_quit:
            log("apply failed after quitting; relaunching the app")
            try:
                launch_app()
            except Exception as error:
                log(f"could not relaunch the app: {error}")
        raise


def bridge_installed(version: str) -> tuple[bool, dict]:
    details: dict = {"version": version}
    try:
        worker = asar_read(APP, "electron/update-worker.cjs")
        update = asar_read(APP, "electron/update.cjs")
        details["worker"] = WORKER_MARKER in worker
        details["guard"] = GUARD_MARKER in update
    except Exception as error:
        details["error"] = str(error)
        return False, details
    try:
        _, metadata = artifact(version)
        verify_runtime(APP_RUNTIME, version=version, bundle=metadata.get("bundleId"), full=True)
        details["runtime"] = True
        details["bundleId"] = metadata.get("bundleId")
    except Exception as error:
        details["runtime"] = False
        details["runtimeError"] = str(error)
    try:
        integrity = verify_asar_integrity(APP)
        details["integrity"] = integrity["ok"]
        details["integrityDetail"] = integrity
    except Exception as error:
        details["integrity"] = False
        details["integrityError"] = str(error)
    return bool(
        details.get("worker")
        and details.get("runtime")
        and details.get("integrity", False)
    ), details


def install_bridge(args: argparse.Namespace) -> dict:
    version = args.version or app_version(APP)
    if args.if_needed:
        installed, details = bridge_installed(version)
        if installed:
            record("bridge-already-installed", version=version, **details)
            return {"status": "already-installed", **details, "app": str(APP)}
    did_quit = False
    if args.wait_idle:
        wait_until_idle(args.idle_timeout, args.quiet_seconds, drain=args.restart)
        if args.restart and alive_commands():
            quit_app()
            did_quit = True
    running = alive_commands()
    if running:
        raise BridgeError("Codex Web GPT is still running:\n" + "\n".join(running))
    try:
        result = patch_installed_app(version, replace_installed=False)
        if args.launch or args.restart:
            launch_app()
            did_quit = False
        return result
    except BaseException:
        if did_quit:
            log("bridge install failed after quitting; relaunching the app")
            try:
                launch_app()
            except Exception as error:
                log(f"could not relaunch the app: {error}")
        raise


def install_launcher(args: argparse.Namespace) -> dict:
    version = args.version or app_version(APP)
    # Fail before quitting the app if the launcher patch is missing or invalid.
    _, manifest = launcher_artifact(version)
    if args.if_needed:
        details = launcher_status(APP, manifest)
        if details["installed"]:
            record("launcher-already-installed", version=version, **details)
            return {"status": "already-installed", **details, "app": str(APP)}
    did_quit = False
    if args.wait_idle:
        wait_until_idle(args.idle_timeout, args.quiet_seconds, drain=args.restart)
        if args.restart and alive_commands():
            quit_app()
            did_quit = True
    running = alive_commands()
    if running:
        raise BridgeError("Codex Web GPT is still running:\n" + "\n".join(running))
    try:
        result = patch_launcher(APP, version)
        if args.launch or args.restart:
            launch_app()
            did_quit = False
        return result
    except BaseException:
        if did_quit:
            log("launcher install failed after quitting; relaunching the app")
            try:
                launch_app()
            except Exception as error:
                log(f"could not relaunch the app: {error}")
        raise


def status(args: argparse.Namespace) -> dict:
    payload: dict = {"root": str(ROOT), "app": str(APP)}
    try:
        payload["appVersion"] = app_version(APP)
    except Exception as error:
        payload["appVersionError"] = str(error)
    for label, runtime in (
        ("appRuntime", APP_RUNTIME),
        ("installedRuntime", CORE_HOME / "versions" / f"{payload.get('appVersion', '')}-darwin-arm64"),
    ):
        try:
            manifest = read_json(runtime / "manifest.json")
            payload[label] = {
                "bundleId": manifest.get("bundleId"),
                "appVersion": manifest.get("appVersion"),
                "fileCount": len(manifest.get("files") or []),
            }
        except Exception as error:
            payload[label] = {"error": str(error)}
    try:
        path, metadata = artifact(str(payload.get("appVersion")))
        payload["artifact"] = {"path": str(path), **metadata}
    except Exception as error:
        payload["artifactError"] = str(error)
    try:
        worker = asar_read(APP, "electron/update-worker.cjs")
        update = asar_read(APP, "electron/update.cjs")
        payload["bridge"] = {
            "worker": WORKER_MARKER in worker,
            "guard": GUARD_MARKER in update,
        }
    except Exception as error:
        payload["bridgeError"] = str(error)
    try:
        payload["launcher"] = launcher_status(APP)
    except Exception as error:
        payload["launcher"] = {"error": str(error)}
    try:
        payload["integrity"] = verify_asar_integrity(APP)
    except Exception as error:
        payload["integrityError"] = str(error)
    payload["health"] = health()
    payload["running"] = alive_commands()
    return payload


def watch(args: argparse.Namespace) -> dict:
    release = latest_release()
    latest = str(release.get("tag_name") or "").lstrip("v")
    current = app_version(APP)
    payload = health()
    result: dict = {
        "current": current,
        "latest": latest,
        "busy": busy(payload),
        "prepared": False,
        "artifacts": {},
    }
    targets: list[str] = []
    if version_tuple(latest) > version_tuple(current):
        targets.append(latest)
    targets.append(current)
    for target in dict.fromkeys(targets):
        try:
            _, metadata = artifact(target)
            verify_runtime(VERSIONS / target / "runtime", version=target, full=True)
            result["artifacts"][target] = "already-present"
            continue
        except BridgeError:
            pass
        # Artifact preparation uses an isolated checkout and does not touch the running app.
        # Skipping it during active turns can leave every release update permanently blocked.
        log(f"preparing hardened runtime for v{target}")
        metadata = prepare(argparse.Namespace(
            version=target,
            source_dir=None,
            bun=None,
            skip_tests=False,
            force=False,
        ))
        result["artifacts"][target] = metadata
        result["prepared"] = True

    runtime = APP_RUNTIME / "manifest.json"
    if runtime.is_file():
        app_bundle = read_json(runtime).get("bundleId")
        try:
            _, metadata = artifact(current)
            result["runtimeMatchesArtifact"] = app_bundle == metadata.get("bundleId")
        except BridgeError:
            result["runtimeMatchesArtifact"] = None
    record("watch", **result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    status_parser = sub.add_parser("status")
    status_parser.set_defaults(func=status)

    prepare_parser = sub.add_parser("prepare", help="build a hardened runtime for a release tag")
    prepare_parser.add_argument("--version", required=True)
    prepare_parser.add_argument("--source-dir")
    prepare_parser.add_argument("--bun")
    prepare_parser.add_argument("--skip-tests", action="store_true")
    prepare_parser.add_argument("--force", action="store_true")
    prepare_parser.set_defaults(func=prepare)

    apply_patch_parser = sub.add_parser(
        "apply-patch", help="apply the canonical patch to a source checkout without building"
    )
    apply_patch_parser.add_argument("--source", required=True)
    apply_patch_parser.add_argument("--version")
    apply_patch_parser.add_argument("--check", action="store_true", help="verify applicability; change nothing")
    apply_patch_parser.add_argument("--three-way", action="store_true", help="allow git apply --3way")
    apply_patch_parser.set_defaults(func=apply_patch_to_source)

    guard_parser = sub.add_parser("guard", help="verify a hardened artifact; used by the updater preflight")
    guard_parser.add_argument("--version", required=True)
    guard_parser.add_argument("--require-idle", action="store_true", help="refuse if a turn is active")
    guard_parser.set_defaults(func=guard)

    stage_parser = sub.add_parser("stage", help="inject the hardened runtime and worker into a staged .app")
    stage_parser.add_argument("--app", required=True)
    stage_parser.add_argument("--version", required=True)
    stage_parser.add_argument("--worker")
    stage_parser.set_defaults(func=stage)

    apply_parser = sub.add_parser("apply", help="apply a hardened runtime to the installed app")
    apply_parser.add_argument("--version")
    apply_parser.add_argument("--wait-idle", action="store_true")
    apply_parser.add_argument("--idle-timeout", type=int, default=3_600)
    apply_parser.add_argument("--quiet-seconds", type=int, default=120)
    apply_parser.add_argument("--restart", action="store_true", help="quit and relaunch the app around the apply")
    apply_parser.add_argument("--launch", action="store_true")
    apply_parser.set_defaults(func=apply_to_app)

    install_parser = sub.add_parser("install-bridge", help="install only the update worker/preflight into the app")
    install_parser.add_argument("--version")
    install_parser.add_argument("--wait-idle", action="store_true")
    install_parser.add_argument("--idle-timeout", type=int, default=3_600)
    install_parser.add_argument("--quiet-seconds", type=int, default=120)
    install_parser.add_argument("--restart", action="store_true", help="quit and relaunch the app around the install")
    install_parser.add_argument("--if-needed", action="store_true", help="exit without touching the app when bridge and runtime are already current")
    install_parser.add_argument("--launch", action="store_true")
    install_parser.set_defaults(func=install_bridge)

    launcher_parser = sub.add_parser(
        "install-launcher",
        help="replace the reviewed Launcher session/JS patch inside app.asar",
    )
    launcher_parser.add_argument("--version")
    launcher_parser.add_argument("--wait-idle", action="store_true")
    launcher_parser.add_argument("--idle-timeout", type=int, default=3_600)
    launcher_parser.add_argument("--quiet-seconds", type=int, default=120)
    launcher_parser.add_argument("--restart", action="store_true", help="quit and relaunch the app around the install")
    launcher_parser.add_argument("--if-needed", action="store_true", help="exit without touching the app when already patched")
    launcher_parser.add_argument("--launch", action="store_true")
    launcher_parser.set_defaults(func=install_launcher)

    watch_parser = sub.add_parser("watch", help="prepare an artifact for the latest release")
    watch_parser.add_argument("--force", action="store_true")
    watch_parser.set_defaults(func=watch)

    args = parser.parse_args()
    try:
        result = args.func(args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if args.command == "guard":
            record("guard-ok", version=args.version, bundleId=result.get("bundleId"))
        return 0
    except BridgeError as error:
        record("error", command=args.command, error=str(error))
        print(f"update-bridge: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # pragma: no cover - keep the updater fail-closed
        record("error", command=args.command, error=f"{type(error).__name__}: {error}")
        print(f"update-bridge: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
