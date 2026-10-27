// CODEX_WEB_GPT_HARDENING_WORKER_V1
//
// Drop-in replacement for the launcher's electron/update-worker.cjs.
//
// The official worker replaces the whole .app with the downloaded official build.
// This worker instead requires a verified hardened runtime for the target version,
// injects it into the staged app (together with this worker), re-signs the staged
// app, and only then swaps it into place. If anything cannot be verified, the
// update is refused and the existing app is relaunched.
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");

const HARDENING_ROOT = process.env.CODEX_WEB_GPT_HARDENING_ROOT
  ? path.resolve(process.env.CODEX_WEB_GPT_HARDENING_ROOT)
  : path.join(os.homedir(), ".codex-chatgpt-web", "codex-web-patcher-7a2674e73e1833ac");
const BRIDGE = path.join(HARDENING_ROOT, "bin", "update-bridge.py");
const PYTHON = "/usr/bin/python3";

function appendLog(job, message) {
  try {
    fs.mkdirSync(path.dirname(job.logPath), { recursive: true, mode: 0o700 });
    fs.appendFileSync(job.logPath, `${new Date().toISOString()} [hardened] ${message}\n`, { mode: 0o600 });
  } catch {}
}

function processAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code === "EPERM";
  }
}

async function waitForParent(pid, timeoutMs = 120_000) {
  const deadline = Date.now() + timeoutMs;
  while (processAlive(pid)) {
    if (Date.now() >= deadline) throw new Error(`Launcher process ${pid} did not exit in time`);
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
}

function launch(bin, args = []) {
  if (process.env.CODEX_WEB_GPT_UPDATE_SKIP_LAUNCH === "1") return;
  const env = { ...process.env };
  // Agent shells export this; macOS `open` would pass it to Electron and the
  // relaunched app would immediately exit as Node.
  delete env.ELECTRON_RUN_AS_NODE;
  const child = spawn(bin, args, { detached: true, stdio: "ignore", windowsHide: true, env });
  child.unref();
}

function requireFile(filePath, label) {
  if (!filePath || !path.isAbsolute(filePath) || !fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
    throw new Error(`${label} is missing: ${filePath || "unknown"}`);
  }
}

function requireDirectory(directory, label) {
  if (!directory || !path.isAbsolute(directory) || !fs.existsSync(directory) || !fs.statSync(directory).isDirectory()) {
    throw new Error(`${label} is missing: ${directory || "unknown"}`);
  }
}

function runBridge(job, args, timeoutMs = 30 * 60_000) {
  if (!fs.existsSync(BRIDGE)) throw new Error(`Hardening bridge is missing: ${BRIDGE}`);
  const result = spawnSync(PYTHON, [BRIDGE, ...args], {
    encoding: "utf8",
    timeout: timeoutMs,
    env: { ...process.env, PYTHONNOUSERSITE: "1", CODEX_WEB_GPT_HARDENING_ROOT: HARDENING_ROOT },
  });
  if (result.error) throw result.error;
  const output = `${result.stdout || ""}${result.stderr || ""}`.trim();
  if (result.status !== 0) {
    throw new Error(`Hardening bridge exited with ${result.status}: ${output.slice(-4000)}`);
  }
  return output;
}

function stageHardenedApp(job) {
  requireFile(BRIDGE, "Hardening bridge");
  requireDirectory(job.source, "Staged macOS application");
  requireFile(path.join(job.source, "Contents", "MacOS", "Codex Web GPT"), "Staged macOS launcher");
  const output = runBridge(job, [
    "stage",
    "--app", job.source,
    "--version", job.version,
    "--worker", __filename,
  ]);
  appendLog(job, `hardening stage completed: ${output.slice(-2000)}`);
}

function updateMac(job) {
  stageHardenedApp(job);

  const sourceExecutable = path.join(job.source, "Contents", "MacOS", "Codex Web GPT");
  requireFile(sourceExecutable, "Staged macOS launcher");
  const next = `${job.target}.updating-${process.pid}`;
  const previous = `${job.target}.swap-${process.pid}`;
  fs.rmSync(next, { recursive: true, force: true });
  fs.rmSync(previous, { recursive: true, force: true });
  const copied = spawnSync("/usr/bin/ditto", [job.source, next], { encoding: "utf8", timeout: 180_000 });
  if (copied.error) throw copied.error;
  if (copied.status !== 0) throw new Error(`Could not stage the macOS application: ${copied.stderr.trim()}`);
  requireFile(path.join(next, "Contents", "MacOS", "Codex Web GPT"), "Copied macOS launcher");

  fs.renameSync(job.target, previous);
  try {
    fs.renameSync(next, job.target);
    if (process.env.CODEX_WEB_GPT_UPDATE_SKIP_LAUNCH !== "1") {
      const env = { ...process.env };
      delete env.ELECTRON_RUN_AS_NODE;
      const opened = spawnSync("/usr/bin/open", [job.target], { encoding: "utf8", timeout: 30_000, env });
      if (opened.error) throw opened.error;
      if (opened.status !== 0) throw new Error(`Could not relaunch updated macOS application: ${opened.stderr?.trim() || opened.status}`);
    }
  } catch (error) {
    if (fs.existsSync(job.target)) fs.rmSync(job.target, { recursive: true, force: true });
    fs.renameSync(previous, job.target);
    throw error;
  } finally {
    fs.rmSync(next, { recursive: true, force: true });
  }
  try {
    fs.rmSync(previous, { recursive: true, force: true });
  } catch (error) {
    appendLog(job, `could not remove previous app: ${error instanceof Error ? error.message : String(error)}`);
  }
}

function relaunchExisting(job) {
  try {
    if (job.platform === "darwin" && fs.existsSync(job.target)) launch("/usr/bin/open", [job.target]);
  } catch {}
}

async function main() {
  const jobPath = process.argv[2];
  if (!jobPath || !path.isAbsolute(jobPath)) throw new Error("Update worker requires an absolute job path");
  const job = JSON.parse(fs.readFileSync(jobPath, "utf8"));
  if (job.platform !== "darwin") {
    appendLog(job, `refusing update on unsupported platform ${job.platform}`);
    throw new Error("The hardened updater supports macOS only");
  }
  appendLog(job, `waiting for launcher PID ${job.parentPid} before installing v${job.version}`);
  await waitForParent(job.parentPid);
  appendLog(job, `installing hardened v${job.version}`);
  try {
    updateMac(job);
    appendLog(job, `hardened v${job.version} installed and relaunched`);
    try { fs.rmSync(job.tempRoot, { recursive: true, force: true }); } catch {}
  } catch (error) {
    appendLog(job, `update refused: ${error instanceof Error ? error.stack || error.message : String(error)}`);
    relaunchExisting(job);
    throw error;
  }
}

if (require.main === module) void main().catch(() => process.exit(1));

module.exports = { updateMac };
