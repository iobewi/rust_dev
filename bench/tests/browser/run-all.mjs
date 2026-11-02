// Runs every qualification file in turn (each starts its own test server and browser) and fails if any check fails.
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
const here = path.dirname(fileURLToPath(import.meta.url));
let failed = 0;
for (const [file, arg] of [["qual-base.mjs", "base"], ["qual-critical.mjs", "all"], ["qual-verdicts-inventory.mjs", "all"], ["qual-security.mjs", "all"], ["qual-truncation.mjs", "all"]]) {
  console.log("=== " + file);
  const r = spawnSync("node", [path.join(here, file), arg], { stdio: "inherit", timeout: 900000 });
  if (r.status !== 0) failed++;
}
process.exit(failed ? 1 : 0);
