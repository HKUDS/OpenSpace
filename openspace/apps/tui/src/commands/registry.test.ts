import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  formatSlashCommandDetailText,
  formatSlashCommandHelpText,
  getCommandCompletions,
  getSlashCommandDefinition,
  parseSlashCommandInput,
} from "./registry.js";

type SlashCommandManifest = {
  commands: Array<{
    name: string;
    handler: "local" | "core";
    summary: string;
    usage: string;
    aliases?: string[];
    category: string;
    args?: Array<{
      name: string;
      required: boolean;
      description: string;
    }>;
    tui_visible?: boolean;
  }>;
};

function readSlashCommandManifest(): SlashCommandManifest {
  const url = new URL(
    "../../../../openspace/protocol/schema/slash_commands.json",
    import.meta.url,
  );
  return JSON.parse(readFileSync(url, "utf8")) as SlashCommandManifest;
}

test("registry exposes migrated core commands in help and completion", () => {
  const completions = getCommandCompletions("su").map(command => command.name);
  assert.deepEqual(completions, ["summary"]);

  const help = formatSlashCommandHelpText();
  assert.match(help, /\/summary — Update session memory/);
  assert.match(help, /\/effort — View or set reasoning effort/);
  assert.match(help, /\/config \(\/settings\) — View or modify local TUI settings/);
});

test("TUI exposes shared core slash command metadata", () => {
  const manifest = readSlashCommandManifest();
  for (const command of manifest.commands.filter(command => command.handler === "core")) {
    const definition = getSlashCommandDefinition(command.name);
    assert.ok(definition, `missing ${command.name}`);
    assert.equal(definition.summary, command.summary);
    assert.equal(definition.usage, command.usage);
    assert.deepEqual(definition.aliases ?? [], command.aliases ?? []);
    assert.equal(definition.category, command.category);
    assert.deepEqual(definition.args ?? [], command.args ?? []);
  }
});

test("settings aliases to config and detailed help includes args", () => {
  const parsed = parseSlashCommandInput("/settings theme light");
  assert.equal(parsed?.command, "config");
  assert.deepEqual(parsed?.args, ["theme", "light"]);
  assert.equal(parsed?.definition?.handler, "core");

  const definition = getSlashCommandDefinition("config");
  assert.ok(definition);
  assert.equal(definition.handler, "core");
  const detail = formatSlashCommandDetailText(definition);
  assert.match(detail, /\/config \[key\] \[value\]/);
  assert.match(detail, /Aliases: \/settings/);
  assert.match(detail, /key \(optional\)/);
});

test("permissions command is routed to core", () => {
  const parsed = parseSlashCommandInput("/permissions");
  assert.equal(parsed?.command, "permissions");
  assert.equal(parsed?.definition?.handler, "core");
});
