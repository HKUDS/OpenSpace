import type { Notification } from "../context/notifications.js";
import type {
  ElicitationRequestData,
  PermissionRequestData,
  PromptRequestData,
  SandboxStatusData,
  StructuredMessageContentBlock,
  TokenWarningEventData,
} from "../bridge/protocol.js";
import type { Command } from "../types/command.js";
import { createStore, type Store } from "./store.js";

export type InputMode = "insert" | "command";
export type PermissionMode = "default" | "acceptEdits" | "plan";
export type ScreenName = "repl" | "resume" | "doctor";
export type ExpandedView = "none" | "tasks" | "teammates";

export type AppMessageRole =
  | "system"
  | "user"
  | "assistant"
  | "tool"
  | "status"
  | "error";

export type AppMessage = {
  id: string;
  role: AppMessageRole;
  text: string;
  content: StructuredMessageContentBlock[];
  timestamp: number;
  meta?: Record<string, unknown>;
};

export type SessionContextState = {
  title?: string;
  mode?: string;
  metadata: Record<string, unknown>;
  runtime: Partial<RuntimeState>;
  agent?: Record<string, unknown> | null;
  standaloneAgentContext?: Record<string, unknown> | null;
  worktree?: Record<string, unknown> | null;
  fileHistorySnapshots: unknown[];
  contentReplacements: unknown[];
};

export type PromptDialogState = {
  queue: Array<{
    request: PromptRequestData;
    value: string;
    error: string | null;
  }>;
};

export type AgentRuntimeState = {
  sessionId?: string;
  selectedPanelTab: "list" | "events" | "transcript";
  selectedEventIndex: number;
  transcriptCursor: number | null;
  list: Array<Record<string, unknown>>;
  events: Array<{
    id: string;
    agentId: string;
    event: string;
    timestamp: number;
    payload?: Record<string, unknown>;
  }>;
  transcripts: Record<string, AppMessage[]>;
  viewedAgentId: string | null;
  backgroundTasks: Record<string, BackgroundAgentTaskState>;
  coordinator: CoordinatorRuntimeState;
  todos: TodoRuntimeState;
};

export type BackgroundAgentTaskState = {
  id: string;
  agentId: string;
  name?: string;
  agentType?: string;
  taskType?: string;
  teamName?: string;
  status: string;
  description?: string;
  currentOperation?: string;
  startedAt: number;
  updatedAt: number;
  completedAt?: number;
  outputFile?: string;
  outputTail?: string;
  parentTaskId?: string;
  model?: string;
  background?: boolean;
  metadata?: Record<string, unknown>;
};

export type CoordinatorRuntimeState = {
  teamName?: string;
  status?: string;
  runningWorkers: number;
  totalWorkers: number;
  updatedAt?: number;
  message?: string;
};

export type TodoItemState = {
  content: string;
  status: "pending" | "in_progress" | "completed" | string;
  activeForm?: string;
};

export type TodoRuntimeState = {
  key?: string;
  agentId?: string;
  todos: TodoItemState[];
  oldTodos: TodoItemState[];
  updatedAt?: number;
  verificationNudgeNeeded?: boolean;
};

export type BackgroundRuntimeState = {
  sessionId?: string;
  taskId?: string;
  status?: string;
  title?: string;
  updatedAt?: number;
  activeAgentId?: string;
  agentCount?: number;
  eventCount?: number;
  transcriptCount?: number;
  metadata?: Record<string, unknown>;
} | null;

export type TaskState = {
  id: string;
  title?: string;
  status: "idle" | "running" | "success" | "error" | "cancelled" | "incomplete";
  turn?: number;
  iterations?: number;
  phase?: string;
  maxIterations?: number;
  toolCalls?: number;
  executionTime?: number;
  error?: string;
  updatedAt: number;
};

export type MCPClientState = {
  serverName: string;
  status: "connected" | "disconnected" | "error";
  error?: string;
  updatedAt: number;
};

export type MCPToolState = {
  name: string;
  description?: string;
  serverName?: string;
};

export type MCPStatusUpdate = {
  serverName: string;
  status: MCPClientState["status"];
  error?: string;
  updatedAt: number;
  tools?: MCPToolState[];
  commands?: Command[];
  resources?: Record<string, string[]>;
};

export type MCPState = {
  clients: MCPClientState[];
  tools: MCPToolState[];
  commands: Command[];
  resources: Record<string, string[]>;
  pluginReconnectKey: number;
};

export type ToolPermissionContext = {
  mode: PermissionMode;
  pendingRequest: PermissionRequestData | null;
  isBypassPermissionsModeAvailable: boolean;
};

export type FooterItem =
  | "tasks"
  | "mcp"
  | "commands"
  | "settings"
  | "agents"
  | "background";

export type CommandHistoryState = {
  entries: string[];
  selectedIndex: number | null;
  draftInput: string;
};

export type PluginState = {
  id: string;
  name: string;
  version?: string;
};

export type PluginInstallationState = {
  marketplaces: Array<{
    name: string;
    status: "pending" | "installing" | "installed" | "failed";
    error?: string;
  }>;
  plugins: Array<{
    id: string;
    name: string;
    status: "pending" | "installing" | "installed" | "failed";
    error?: string;
  }>;
};

export type PluginsState = {
  enabled: PluginState[];
  disabled: PluginState[];
  commands: Command[];
  errors: string[];
  installationStatus: PluginInstallationState;
  needsRefresh: boolean;
};

export type WorkerSandboxPermissionsState = {
  queue: Array<{
    requestId: string;
    workerId: string;
    workerName: string;
    workerColor?: string;
    host: string;
    createdAt: number;
  }>;
  selectedIndex: number;
};

export type PendingWorkerRequestState = {
  toolName: string;
  toolUseId: string;
  description: string;
  workerId: string;
  workerName: string;
  workerColor?: string;
  host?: string;
  requestKind: "tool" | "network" | "sandbox";
} | null;

export type PendingSandboxRequestState = {
  requestId: string;
  host: string;
  requestKind: "network" | "sandbox";
} | null;

export type RuntimeState = {
  screen: ScreenName;
  viewMode?: "prompt" | "transcript" | "agent";
  model?: string;
  sessionId?: string;
  costUsd?: number;
  inputTokens?: number;
  outputTokens?: number;
  phase?: string;
  activeTaskId?: string;
  maxIterations?: number;
  totalIterations?: number;
  tokenWarning?: TokenWarningEventData;
  sandbox?: SandboxStatusData;
};

export type AppState = {
  messages: AppMessage[];
  input: string;
  inputMode: InputMode;
  commandHistory: CommandHistoryState;
  isQuerying: boolean;
  settings: Record<string, unknown>;
  verbose: boolean;
  mainLoopModel: string | null;
  statusLineText?: string;
  expandedView: ExpandedView;
  footerSelection: FooterItem | null;
  toolPermissionContext: ToolPermissionContext;
  notifications: {
    current: Notification | null;
    queue: Notification[];
  };
  tasks: Record<string, TaskState>;
  mcp?: MCPState;
  plugins: PluginsState;
  elicitation: {
    queue: ElicitationRequestData[];
  };
  workerSandboxPermissions: WorkerSandboxPermissionsState;
  pendingWorkerRequest: PendingWorkerRequestState;
  pendingSandboxRequest: PendingSandboxRequestState;
  sessionContext: SessionContextState | null;
  promptDialog: PromptDialogState;
  agents: AgentRuntimeState;
  backgroundSession: BackgroundRuntimeState;
  fastMode: boolean;
  runtime: RuntimeState;
  activeOverlays: ReadonlySet<string>;
};

export type AppStateStore = Store<AppState>;

function createMcpState(clients: MCPClientState[]): MCPState {
  return {
    clients,
    tools: [],
    commands: [],
    resources: {},
    pluginReconnectKey: 0,
  };
}

function upsertMcpClient(
  clients: MCPClientState[],
  update: MCPClientState,
): MCPClientState[] {
  const index = clients.findIndex(
    client => client.serverName === update.serverName,
  );

  if (index === -1) {
    return [...clients, update];
  }

  const next = [...clients];
  next[index] = {
    ...next[index],
    ...update,
  };
  return next;
}

export function applyMcpStatusUpdate(
  state: AppState,
  update: MCPStatusUpdate,
): AppState {
  const prevMcp = state.mcp ?? createMcpState([]);
  return {
    ...state,
    mcp: {
      ...prevMcp,
      clients: upsertMcpClient(prevMcp.clients, {
        serverName: update.serverName,
        status: update.status,
        error: update.error,
        updatedAt: update.updatedAt,
      }),
      tools: update.tools ?? prevMcp.tools,
      commands: update.commands ?? prevMcp.commands,
      resources: update.resources ?? prevMcp.resources,
    },
  };
}

function createPluginInstallationState(): PluginInstallationState {
  return {
    marketplaces: [],
    plugins: [],
  };
}

function createPluginsState(): PluginsState {
  return {
    enabled: [],
    disabled: [],
    commands: [],
    errors: [],
    installationStatus: createPluginInstallationState(),
    needsRefresh: false,
  };
}

function createWorkerSandboxPermissionsState(): WorkerSandboxPermissionsState {
  return {
    queue: [],
    selectedIndex: 0,
  };
}

function createPromptDialogState(): PromptDialogState {
  return {
    queue: [],
  };
}

function createAgentRuntimeState(): AgentRuntimeState {
  return {
    sessionId: undefined,
    selectedPanelTab: "list",
    selectedEventIndex: 0,
    transcriptCursor: null,
    list: [],
    events: [],
    transcripts: {},
    viewedAgentId: null,
    backgroundTasks: {},
    coordinator: {
      runningWorkers: 0,
      totalWorkers: 0,
    },
    todos: {
      todos: [],
      oldTodos: [],
    },
  };
}

function createNotificationState(): {
  current: Notification | null;
  queue: Notification[];
} {
  return {
    current: null,
    queue: [],
  };
}

function normalizeAppState(state: AppState): AppState {
  const rawPlugins = state.plugins ?? createPluginsState();
  const deprecatedMcpClientField = "mcp" + "Clients";
  const hasDeprecatedMcpClientField = Object.prototype.hasOwnProperty.call(
    state,
    deprecatedMcpClientField,
  );
  const clients = state.mcp?.clients ?? [];
  const normalizedMcp =
    state.mcp?.clients === clients &&
    state.mcp.tools !== undefined &&
    state.mcp.commands !== undefined &&
    state.mcp.resources !== undefined
      ? state.mcp
      : {
          ...(state.mcp ?? createMcpState(clients)),
          clients,
          tools: state.mcp?.tools ?? [],
          commands: state.mcp?.commands ?? [],
          resources: state.mcp?.resources ?? {},
          pluginReconnectKey: state.mcp?.pluginReconnectKey ?? 0,
        };
  const plugins =
    rawPlugins.enabled !== undefined &&
    rawPlugins.disabled !== undefined &&
    rawPlugins.installationStatus !== undefined
      ? rawPlugins
      : {
          ...createPluginsState(),
          ...rawPlugins,
          enabled: rawPlugins.enabled ?? [],
          disabled: rawPlugins.disabled ?? [],
          commands: rawPlugins.commands ?? [],
          errors: rawPlugins.errors ?? [],
          installationStatus:
            rawPlugins.installationStatus ?? createPluginInstallationState(),
          needsRefresh: rawPlugins.needsRefresh ?? false,
        };
  const elicitation = state.elicitation ?? { queue: [] };
  const commandHistory = state.commandHistory ?? {
    entries: [],
    selectedIndex: null,
    draftInput: "",
  };
  const notifications = state.notifications ?? createNotificationState();
  const workerSandboxPermissions =
    state.workerSandboxPermissions ?? createWorkerSandboxPermissionsState();
  const verbose = state.verbose ?? false;
  const mainLoopModel = state.mainLoopModel ?? null;
  const expandedView = state.expandedView ?? "none";
  const fastMode = state.fastMode ?? false;
  const activeOverlays = state.activeOverlays ?? new Set<string>();
  const sessionContext = state.sessionContext ?? null;
  const promptDialog = state.promptDialog ?? createPromptDialogState();
  const agents =
    state.agents?.list !== undefined &&
    state.agents?.events !== undefined &&
    state.agents?.transcripts !== undefined
      ? {
          ...createAgentRuntimeState(),
          ...state.agents,
          selectedPanelTab:
            state.agents.selectedPanelTab ?? "list",
          selectedEventIndex:
            state.agents.selectedEventIndex ?? 0,
          transcriptCursor:
            state.agents.transcriptCursor ?? null,
        }
      : createAgentRuntimeState();
  const backgroundSession = state.backgroundSession ?? null;
  const pendingWorkerRequest = state.pendingWorkerRequest ?? null;
  const pendingSandboxRequest = state.pendingSandboxRequest ?? null;

  if (
    !hasDeprecatedMcpClientField &&
    state.mcp === normalizedMcp &&
    state.plugins === plugins &&
    state.elicitation === elicitation &&
    state.commandHistory === commandHistory &&
    state.notifications === notifications &&
    state.workerSandboxPermissions === workerSandboxPermissions &&
    state.verbose === verbose &&
    state.mainLoopModel === mainLoopModel &&
    state.expandedView === expandedView &&
    state.fastMode === fastMode &&
    state.activeOverlays === activeOverlays &&
    state.sessionContext === sessionContext &&
    state.promptDialog === promptDialog &&
    state.agents === agents &&
    state.backgroundSession === backgroundSession &&
    state.pendingWorkerRequest === pendingWorkerRequest &&
    state.pendingSandboxRequest === pendingSandboxRequest
  ) {
    return state;
  }

  const {
    [deprecatedMcpClientField]: _deprecatedMcpClientValue,
    ...stateWithoutDeprecatedMcpClientField
  } = state as AppState & Record<string, unknown>;

  return {
    ...(stateWithoutDeprecatedMcpClientField as AppState),
    mcp: normalizedMcp,
    plugins,
    elicitation,
    commandHistory,
    notifications,
    workerSandboxPermissions,
    verbose,
    mainLoopModel,
    expandedView,
    fastMode,
    activeOverlays,
    sessionContext,
    promptDialog,
    agents,
    backgroundSession,
    pendingWorkerRequest,
    pendingSandboxRequest,
  };
}

export function getDefaultAppState(
  screen: ScreenName = "repl",
): AppState {
  return {
    messages: [],
    input: "",
    inputMode: "insert",
    commandHistory: {
      entries: [],
      selectedIndex: null,
      draftInput: "",
    },
    isQuerying: false,
    settings: {},
    verbose: false,
    mainLoopModel: null,
    statusLineText: undefined,
    expandedView: "none",
    footerSelection: null,
    toolPermissionContext: {
      mode: "default",
      pendingRequest: null,
      isBypassPermissionsModeAvailable: false,
    },
    notifications: createNotificationState(),
    tasks: {},
    mcp: createMcpState([]),
    plugins: createPluginsState(),
    elicitation: {
      queue: [],
    },
    workerSandboxPermissions: createWorkerSandboxPermissionsState(),
    pendingWorkerRequest: null,
    pendingSandboxRequest: null,
    sessionContext: null,
    promptDialog: createPromptDialogState(),
    agents: createAgentRuntimeState(),
    backgroundSession: null,
    fastMode: false,
    runtime: {
      screen,
    },
    activeOverlays: new Set<string>(),
  };
}

export function createAppStateStore(
  initialState: AppState,
  onChangeAppState?: (args: {
    newState: AppState;
    oldState: AppState;
  }) => void,
): AppStateStore {
  const store = createStore(
    normalizeAppState(initialState),
    onChangeAppState,
  );

  return {
    getState: store.getState,
    subscribe: store.subscribe,
    setState: updater => {
      store.setState(prev => normalizeAppState(updater(prev)));
    },
  };
}

export {
  AppStateProvider,
  useAppState,
  useAppStateMaybeOutsideOfProvider,
  useAppStateStore,
  useSetAppState,
} from "./AppState.js";
