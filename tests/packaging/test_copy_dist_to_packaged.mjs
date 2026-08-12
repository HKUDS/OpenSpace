/**
 * Smoke tests for scripts/copy-dist-to-packaged.mjs
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { main } from "../../scripts/copy-dist-to-packaged.mjs";

const repoRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);

test("copies dist tree into packaged destination", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "openspace-pack-"));
  const dist = path.join(tmp, "dist");
  const dest = path.join(tmp, "packaged");
  fs.mkdirSync(dist, { recursive: true });
  fs.writeFileSync(path.join(dist, "index.html"), "<html>ok</html>\n");
  fs.mkdirSync(path.join(dist, "assets"), { recursive: true });
  fs.writeFileSync(path.join(dist, "assets", "app.js"), "console.log(1);\n");

  main([dist, dest]);

  assert.equal(
    fs.readFileSync(path.join(dest, "index.html"), "utf8"),
    "<html>ok</html>\n",
  );
  assert.equal(
    fs.readFileSync(path.join(dest, "assets", "app.js"), "utf8"),
    "console.log(1);\n",
  );
});

test("copies optional node_modules and package.json for TUI", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "openspace-pack-tui-"));
  const dist = path.join(tmp, "dist");
  const dest = path.join(tmp, "packaged");
  const nodeModules = path.join(tmp, "node_modules");
  const packageJson = path.join(tmp, "package.json");

  fs.mkdirSync(path.join(nodeModules, "ink"), { recursive: true });
  fs.writeFileSync(path.join(nodeModules, "ink", "index.js"), "export {};\n");
  fs.writeFileSync(packageJson, JSON.stringify({ name: "openspace-tui" }));
  fs.mkdirSync(dist, { recursive: true });
  fs.writeFileSync(path.join(dist, "index.js"), "console.log('tui');\n");

  main([
    dist,
    dest,
    "--node-modules",
    nodeModules,
    "--package-json",
    packageJson,
  ]);

  assert.ok(fs.existsSync(path.join(dest, "index.js")));
  assert.ok(fs.existsSync(path.join(dest, "node_modules", "ink", "index.js")));
  assert.equal(
    JSON.parse(fs.readFileSync(path.join(dest, "package.json"), "utf8")).name,
    "openspace-tui",
  );
});

test("fails when dist directory is missing", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "openspace-pack-miss-"));
  assert.throws(
    () => main([path.join(tmp, "missing-dist"), path.join(tmp, "out")]),
    /distDir is missing/,
  );
});

test("script exists at the path apps/* package.json expects", () => {
  const scriptPath = path.join(repoRoot, "scripts", "copy-dist-to-packaged.mjs");
  assert.ok(fs.existsSync(scriptPath), `missing ${scriptPath}`);
});
