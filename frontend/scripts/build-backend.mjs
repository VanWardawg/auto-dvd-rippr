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

mkdirSync(backendOutDir, { recursive: true });
mkdirSync(configOutDir, { recursive: true });
mkdirSync(workDir, { recursive: true });
mkdirSync(buildWorkDir, { recursive: true });
mkdirSync(buildSpecDir, { recursive: true });
copyFileSync(configExample, path.join(configOutDir, "config.example.json"));
rmSync(path.join(backendOutDir, "autorippr-backend.exe"), { force: true });
rmSync(path.join(backendOutDir, "autorippr-backend"), { recursive: true, force: true });

const pyinstallerProbe = spawnSync("py", ["-3.11", "-m", "PyInstaller", "--version"], {
  stdio: "pipe",
  shell: true,
  cwd: repoRoot,
});

if (pyinstallerProbe.status !== 0) {
  console.error("PyInstaller is required for self-contained builds. Install it with:");
  console.error("  py -3.11 -m pip install pyinstaller");
  process.exit(pyinstallerProbe.status ?? 1);
}

const build = spawnSync(
  "py",
  [
    "-3.11",
    "-m",
    "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onedir",
    "--name",
    "autorippr-backend",
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
