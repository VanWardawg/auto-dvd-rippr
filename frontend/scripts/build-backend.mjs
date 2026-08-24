import { copyFileSync, mkdirSync, rmSync } from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const appMain = path.join(repoRoot, "app", "main.py");
const configExample = path.join(repoRoot, "app", "config.example.json");
const resourcesRoot = path.join(repoRoot, "frontend", "src-tauri", "resources");
const backendOutDir = path.join(resourcesRoot, "backend");
const configOutDir = path.join(resourcesRoot, "config");
const workDir = path.join(repoRoot, "frontend", "src-tauri", "target", "pyinstaller");
const runId = Date.now().toString();
const buildWorkDir = path.join(workDir, "work-" + runId);
const buildSpecDir = path.join(workDir, "spec-" + runId);

/**
 * The backend is stdlib-only, so anything PyInstaller pulls in beyond that is
 * dead weight in every release download. tkinter alone drags in ~20 MB of
 * Tcl/Tk data; the legacy Tk GUI that needed it was removed in favour of the
 * Tauri frontend. See CLAUDE.md before adding an import that reintroduces one
 * of these.
 */
const EXCLUDED_MODULES = ["tkinter", "_tkinter", "test", "distutils", "pydoc_data"];

/**
 * Resolve a Python that actually has PyInstaller importable.
 *
 * Locally this is `py -3.11`. On GitHub Actions, `actions/setup-python` puts
 * its interpreter on PATH as `python` but does not register it with the `py`
 * launcher -- so `py -3.11` can resolve to a *different* Python that never had
 * `pip install -r requirements-dev.txt` run against it. Probing avoids that
 * mismatch. Set AUTORIPPR_PYTHON to override.
 */
function resolvePython() {
  const override = process.env.AUTORIPPR_PYTHON;
  const candidates = override
    ? [override.split(" ")]
    : [["py", "-3.11"], ["python"], ["python3"]];

  for (const candidate of candidates) {
    const probe = spawnSync(candidate[0], [...candidate.slice(1), "-m", "PyInstaller", "--version"], {
      stdio: "pipe",
      shell: true,
      cwd: repoRoot,
    });
    if (probe.status === 0) {
      const version = String(probe.stdout ?? "").trim();
      console.log(`Using ${candidate.join(" ")} with PyInstaller ${version}`);
      return candidate;
    }
  }
  return null;
}

mkdirSync(backendOutDir, { recursive: true });
mkdirSync(configOutDir, { recursive: true });
mkdirSync(workDir, { recursive: true });
mkdirSync(buildWorkDir, { recursive: true });
mkdirSync(buildSpecDir, { recursive: true });
copyFileSync(configExample, path.join(configOutDir, "config.example.json"));
rmSync(path.join(backendOutDir, "autorippr-backend.exe"), { force: true });
rmSync(path.join(backendOutDir, "autorippr-backend"), { recursive: true, force: true });

const python = resolvePython();

if (!python) {
  console.error("PyInstaller is required for self-contained builds. Install it with:");
  console.error("  py -3.11 -m pip install -r requirements-dev.txt");
  console.error("Or set AUTORIPPR_PYTHON to an interpreter that has it.");
  process.exit(1);
}

const excludeArgs = EXCLUDED_MODULES.flatMap((name) => ["--exclude-module", name]);

const build = spawnSync(
  python[0],
  [
    ...python.slice(1),
    "-m",
    "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onedir",
    "--name",
    "autorippr-backend",
    ...excludeArgs,
    "--distpath",
    backendOutDir,
    "--workpath",
    buildWorkDir,
    "--specpath",
    buildSpecDir,
    appMain,
  ],
  {
    stdio: "inherit",
    shell: true,
    cwd: repoRoot,
  },
);

process.exit(build.status ?? 1);
