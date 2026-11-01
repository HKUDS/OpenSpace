import assert from "node:assert/strict";
import test from "node:test";
import {
  applyMcpStatusUpdate,
  createAppStateStore,
  getDefaultAppState,
  type AppState,
  type MCPClientState,
} from "./AppStateStore.js";
import { selectMcpClientStates } from "./selectors.js";

const canonicalClient: MCPClientState = {
  serverName: "canonical",
  status: "error",
  error: "failed",
  updatedAt: 2,
};

test("default state exposes MCP clients only through nested MCP state", () => {
  const state = createAppStateStore(getDefaultAppState()).getState();

  assert.deepEqual(selectMcpClientStates(state), []);
  assert.deepEqual(
    Object.keys(state).filter(key => key.toLowerCase().includes("mcp")),
    ["mcp"],
  );
});

test("drops deprecated top-level MCP client arrays without importing them", () => {
  const deprecatedField = "mcp" + "Clients";
  const store = createAppStateStore({
    ...getDefaultAppState(),
    mcp: undefined,
    [deprecatedField]: [canonicalClient],
  } as AppState & Record<string, unknown>);
  const state = store.getState();

  assert.deepEqual(selectMcpClientStates(state), []);
  assert.equal(Object.hasOwn(state, deprecatedField), false);
});

test("keeps canonical nested MCP clients", () => {
  const base = getDefaultAppState();
  const state: AppState = {
    ...base,
    mcp: {
      ...base.mcp!,
      clients: [canonicalClient],
    },
  };
  const store = createAppStateStore(state);

  assert.deepEqual(selectMcpClientStates(store.getState()), [canonicalClient]);
});

test("normalizes missing MCP state to an empty canonical state", () => {
  const store = createAppStateStore({
    ...getDefaultAppState(),
    mcp: undefined,
  });

  assert.deepEqual(store.getState().mcp?.clients, []);
  assert.deepEqual(selectMcpClientStates(store.getState()), []);
});

test("setState preserves canonical nested MCP clients", () => {
  const base = getDefaultAppState();
  const store = createAppStateStore({
    ...base,
    mcp: {
      ...base.mcp!,
      clients: [canonicalClient],
    },
  });

  store.setState(prev => ({
    ...prev,
    input: "next",
  }));

  assert.deepEqual(selectMcpClientStates(store.getState()), [canonicalClient]);
});

test("applies MCP status updates against canonical nested clients", () => {
  const base = getDefaultAppState();
  const secondClient: MCPClientState = {
    serverName: "second",
    status: "connected",
    updatedAt: 3,
  };
  const store = createAppStateStore({
    ...base,
    mcp: {
      ...base.mcp!,
      clients: [canonicalClient, secondClient],
      tools: [{ name: "existing-tool" }],
      commands: [
        {
          name: "existing-command",
          description: "Existing command",
          handler: "core",
        },
      ],
      resources: { canonical: ["resource"] },
    },
  });

  store.setState(prev =>
    applyMcpStatusUpdate(prev, {
      serverName: "canonical",
      status: "connected",
      updatedAt: 4,
    }),
  );

  const state = store.getState();
  assert.deepEqual(state.mcp?.clients, [
    {
      serverName: "canonical",
      status: "connected",
      error: undefined,
      updatedAt: 4,
    },
    secondClient,
  ]);
  assert.deepEqual(state.mcp?.tools, [{ name: "existing-tool" }]);
  assert.deepEqual(state.mcp?.resources, { canonical: ["resource"] });
});
