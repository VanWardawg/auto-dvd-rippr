import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

/**
 * Run the Tauri CLI.
 *
 * Prefers a globally installed `tauri` (from `cargo install tauri-cli`) but
 * falls back to the local @tauri-apps/cli devDependency, which is what most
 * checkouts actually have. Without the fallback this failed with a bare
 * "command not found" on any machine that had not installed the CLI globally.
 */
const userProfile = process.env.USERPROFILE ?? process.env.HOME ?? "";
const cargoBin = path.join(userProfile, ".cargo", "bin");

/**
 * Keep build output out of a cloud-synced folder.
 *
 * When the checkout lives under OneDrive, its sync engine follows `target/`
 * and holds files open mid-build. That surfaces as `failed to bundle project:
 * Access is denied (os error 5)` from WiX, after a successful compile, and the
 * offending file cannot even be deleted afterwards -- no user process holds
 * it, so it is the filter driver rather than anything killable.
 *
 * Redirecting the target directory outside the sync root avoids it entirely.
 * Only applied when the repo really is inside a synced folder, so CI and
 * ordinary checkouts keep the standard `src-tauri/target` path that the
 * release workflow globs.
 */
function resolveTargetDir(repoRoot) {
  if (process.env.CARGO_TARGET_DIR) return process.env.CARGO_TARGET_DIR;
  const syncRoots = [process.env.OneDrive, process.env.OneDriveCommercial, process.env.OneDriveConsumer]
    .filter(Boolean)
    .map((dir) => path.resolve(dir).toLowerCase());
  const here = path.resolve(repoRoot).toLowerCase();
  if (!syncRoots.some((root) => here.startsWith(root))) return null;

  const base = process.env.LOCALAPPDATA ?? process.env.TMPDIR ?? userProfile;
  return path.join(base, "autorippr-build", "target");
}

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const targetDir = resolveTargetDir(repoRoot);

const env = {
  ...process.env,
  PATH: cargoBin + path.delimiter + (process.env.PATH ?? ""),
};

if (targetDir) {
  env.CARGO_TARGET_DIR = targetDir;
  console.log(`This checkout is inside a synced folder, so build output goes to:
  ${targetDir}`);
}

const args = process.argv.slice(2);

function run(command, commandArgs) {
  return spawn(command, commandArgs, { stdio: "inherit", shell: true, env });
}

const child = run("tauri", args);

child.on("error", () => {
  // No global CLI; use the one in node_modules.
  const fallback = run("npx", ["tauri", ...args]);
  fallback.on("exit", (code) => process.exit(code ?? 0));
});

child.on("exit", (code) => {
  if (code === 127) {
    const fallback = run("npx", ["tauri", ...args]);
    fallback.on("exit", (fallbackCode) => process.exit(fallbackCode ?? 0));
    return;
  }
  process.exit(code ?? 0);
});
