/**
 * Verify every command the frontend invokes is actually registered in Rust.
 *
 * This mismatch is invisible at build time. `tsc` cannot know what Tauri
 * registered, and `cargo` cannot know what the frontend calls -- so a command
 * that does not exist compiles cleanly and fails at runtime with
 * "Command <name> not found", when the user clicks the button.
 *
 * That is exactly how `delete_job` shipped broken: it was declared
 * `#[tauri::command(name = "delete_job")]`, but `name` is not a recognised
 * attribute (tauri-macros only understands `rename_all`, `rename`, `root` and
 * `async`), and it silently ignores unknown name-value pairs. The command
 * registered under its function identifier, `delete_job_cmd`, while api.ts
 * kept invoking `delete_job`.
 *
 * Run: node ./scripts/check-commands.mjs
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const srcDir = path.join(frontendRoot, "src");
const mainRs = path.join(frontendRoot, "src-tauri", "src", "main.rs");

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) {
      out.push(...walk(full));
    } else if (/\.(ts|tsx)$/.test(entry)) {
      out.push(full);
    }
  }
  return out;
}

/** Command names the frontend calls, with the file they were called from. */
function collectInvoked() {
  const invoked = new Map();
  for (const file of walk(srcDir)) {
    const text = readFileSync(file, "utf8");
    // invoke("name" | 'name' | `name`, ...) including invoke<T>("name", ...)
    const pattern = /\binvoke\s*(?:<[^>]*>)?\s*\(\s*["'`]([A-Za-z0-9_]+)["'`]/g;
    let match;
    while ((match = pattern.exec(text)) !== null) {
      const relative = path.relative(frontendRoot, file).replace(/\\/g, "/");
      if (!invoked.has(match[1])) {
        invoked.set(match[1], relative);
      }
    }
  }
  return invoked;
}

/** Command names Rust actually registers, honouring `rename = "..."`. */
function collectRegistered() {
  const text = readFileSync(mainRs, "utf8");

  const handler = text.match(/generate_handler!\s*\[([\s\S]*?)\]/);
  if (!handler) {
    console.error("Could not find generate_handler![...] in main.rs");
    process.exit(1);
  }

  const idents = handler[1]
    .split(",")
    .map((s) => s.trim())
    .filter((s) => /^[A-Za-z_][A-Za-z0-9_]*$/.test(s));

  return new Set(
    idents.map((ident) => {
      // A `rename` on the command attribute changes the registered name;
      // anything else (including the invalid `name`) leaves it as the ident.
      const declaration = new RegExp(
        `#\\[tauri::command([^\\]]*)\\]\\s*(?:pub\\s+)?(?:async\\s+)?fn\\s+${ident}\\b`,
      );
      const found = text.match(declaration);
      const rename = found?.[1]?.match(/\brename\s*=\s*"([^"]+)"/);
      return rename ? rename[1] : ident;
    }),
  );
}

const invoked = collectInvoked();
const registered = collectRegistered();

const missing = [...invoked.entries()].filter(([name]) => !registered.has(name));
const unused = [...registered].filter((name) => !invoked.has(name));

if (missing.length > 0) {
  console.error("Frontend invokes commands that Rust does not register:\n");
  for (const [name, file] of missing) {
    console.error(`  ${name}  (called from ${file})`);
  }
  console.error("\nAdd the command to generate_handler![] in src-tauri/src/main.rs,");
  console.error("or correct the name passed to invoke().");
  process.exit(1);
}

console.log(`All ${invoked.size} invoked command(s) are registered.`);
if (unused.length > 0) {
  // Not a failure: a command may be intended for future use or called from
  // somewhere this script does not scan.
  console.log(`Registered but never invoked: ${unused.join(", ")}`);
}
