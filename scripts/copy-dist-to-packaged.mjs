#!/usr/bin/env node
/**
 * Copy a frontend build `dist/` into openspace/packaged/<target>.
 *
 * Usage:
 *   node scripts/copy-dist-to-packaged.mjs <distDir> <destDir> \
 *     [--node-modules <dir>] [--package-json <file>]
 *
 * Dashboard only needs the Vite dist tree.
 * TUI also copies production node_modules + package.json so the packaged
 * Ink entrypoint can run without a separate npm install after pip install.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

function usage(message) {
  if (message) {
    console.error(message);
  }
  console.error(
    "Usage: node scripts/copy-dist-to-packaged.mjs <distDir> <destDir> " +
      "[--node-modules <dir>] [--package-json <file>]",
  );
  process.exit(1);
}

function parseArgs(argv) {
  const positional = [];
  let nodeModules = null;
  let packageJson = null;

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--node-modules") {
      nodeModules = argv[++i];
      if (!nodeModules) usage("Missing value for --node-modules");
      continue;
    }
    if (arg === "--package-json") {
      packageJson = argv[++i];
      if (!packageJson) usage("Missing value for --package-json");
      continue;
    }
    if (arg.startsWith("-")) {
      usage(`Unknown option: ${arg}`);
    }
    positional.push(arg);
  }

  if (positional.length !== 2) {
    usage("Expected exactly <distDir> and <destDir>");
  }

  return {
    distDir: path.resolve(positional[0]),
    destDir: path.resolve(positional[1]),
    nodeModules: nodeModules ? path.resolve(nodeModules) : null,
    packageJson: packageJson ? path.resolve(packageJson) : null,
  };
}

function assertDirectory(dirPath, label) {
  if (!fs.existsSync(dirPath) || !fs.statSync(dirPath).isDirectory()) {
    throw new Error(`${label} is missing or not a directory: ${dirPath}`);
  }
}

function assertFile(filePath, label) {
  if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
    throw new Error(`${label} is missing or not a file: ${filePath}`);
  }
}

function emptyDirectory(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
  for (const entry of fs.readdirSync(dirPath)) {
    fs.rmSync(path.join(dirPath, entry), { recursive: true, force: true });
  }
}

function copyTree(source, destination) {
  fs.cpSync(source, destination, {
    recursive: true,
    force: true,
    errorOnExist: false,
  });
}

function main(argv = process.argv.slice(2)) {
  const options = parseArgs(argv);
  assertDirectory(options.distDir, "distDir");

  if (options.nodeModules) {
    assertDirectory(options.nodeModules, "nodeModules");
  }
  if (options.packageJson) {
    assertFile(options.packageJson, "packageJson");
  }

  emptyDirectory(options.destDir);
  copyTree(options.distDir, options.destDir);

  if (options.nodeModules) {
    copyTree(options.nodeModules, path.join(options.destDir, "node_modules"));
  }
  if (options.packageJson) {
    fs.copyFileSync(
      options.packageJson,
      path.join(options.destDir, "package.json"),
    );
  }

  console.log(`Packaged assets copied to ${options.destDir}`);
  return options.destDir;
}

const isDirectRun =
  process.argv[1] &&
  path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);

if (isDirectRun) {
  try {
    main();
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exit(1);
  }
}

export { main, parseArgs };
