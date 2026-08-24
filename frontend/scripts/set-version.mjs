/**
 * Stamp one version across every file that carries it.
 *
 * The version lives in three places that must agree -- tauri.conf.json (drives
 * the installer and the About box), Cargo.toml (the Rust crate), and
 * package.json -- and keeping them in sync by hand is how a release ends up
 * labelled 0.1.0 six versions later.
 *
 * Usage:
 *   node ./scripts/set-version.mjs 1.2.3
 *   node ./scripts/set-version.mjs            # derive from the current git tag
 *
 * Release CI calls this with the pushed tag, so the tag is the single source
 * of truth and the values committed in the repo are only a dev baseline.
 */
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = path.resolve(frontendRoot, "..");

function deriveVersionFromGit() {
  const result = spawnSync("git", ["describe", "--tags", "--abbrev=0"], {
    stdio: "pipe",
    encoding: "utf8",
    cwd: repoRoot,
  });
  if (result.status !== 0) {
    return null;
  }
  return String(result.stdout ?? "").trim();
}

const raw = process.argv[2] ?? deriveVersionFromGit();

if (!raw) {
  console.error("No version given and no git tag found.");
  console.error("Usage: node ./scripts/set-version.mjs <version>");
  process.exit(1);
}

// Accept both `v1.2.3` (tag form) and `1.2.3`.
const version = raw.replace(/^v/, "");

if (!/^\d+\.\d+\.\d+$/.test(version)) {
  console.error(`Not a valid semver version: "${version}" (from "${raw}")`);
  process.exit(1);
}

function patch(file, transform) {
  const full = path.join(repoRoot, file);
  const before = readFileSync(full, "utf8");
  const after = transform(before);
  if (before === after) {
    console.warn(`  ! ${file} unchanged -- version field not found or already ${version}`);
    return;
  }
  writeFileSync(full, after);
  console.log(`  ${file} -> ${version}`);
}

console.log(`Stamping version ${version}`);

patch("frontend/src-tauri/tauri.conf.json", (t) =>
  t.replace(/("version"\s*:\s*)"[^"]*"/, `$1"${version}"`),
);

// Only the [package] version, which is the first `version = "..."` in the file.
patch("frontend/src-tauri/Cargo.toml", (t) =>
  t.replace(/^version\s*=\s*"[^"]*"/m, `version = "${version}"`),
);

patch("frontend/package.json", (t) =>
  t.replace(/("version"\s*:\s*)"[^"]*"/, `$1"${version}"`),
);
