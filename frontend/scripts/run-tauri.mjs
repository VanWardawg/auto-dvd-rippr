import { spawn } from "node:child_process";
import path from "node:path";

const userProfile = process.env.USERPROFILE ?? "";
const cargoBin = path.join(userProfile, ".cargo", "bin");
const env = {
  ...process.env,
  PATH: cargoBin + path.delimiter + (process.env.PATH ?? ""),
};

const child = spawn("tauri", process.argv.slice(2), {
  stdio: "inherit",
  shell: true,
  env,
});

child.on("exit", (code) => {
  process.exit(code ?? 0);
});
