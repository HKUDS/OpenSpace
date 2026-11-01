import type { AppState } from "./AppStateStore.js";

export const selectMessages = (state: AppState) => state.messages;
export const selectInput = (state: AppState) => state.input;
export const selectInputMode = (state: AppState) => state.inputMode;
export const selectCommandHistory = (state: AppState) => state.commandHistory;
export const selectIsQuerying = (state: AppState) => state.isQuerying;
export const selectSettings = (state: AppState) => state.settings;
export const selectVerbose = (state: AppState) => state.verbose;
export const selectToolPermissionContext = (state: AppState) =>
  state.toolPermissionContext;
export const selectPendingPermissionRequest = (state: AppState) =>
  state.toolPermissionContext.pendingRequest;
export const selectNotifications = (state: AppState) => state.notifications;
export const selectTasks = (state: AppState) => state.tasks;
export const selectMcpClientStates = (state: AppState) =>
  state.mcp?.clients ?? [];
export const selectMcpTools = (state: AppState) => state.mcp?.tools ?? [];
export const selectMcpCommands = (state: AppState) => state.mcp?.commands ?? [];
export const selectMcpResources = (state: AppState) =>
  state.mcp?.resources ?? {};
export const selectPlugins = (state: AppState) => state.plugins;
export const selectModel = (state: AppState) => state.runtime.model;
export const selectMainLoopModel = (state: AppState) => state.mainLoopModel;
export const selectSessionId = (state: AppState) => state.runtime.sessionId;
export const selectCostUsd = (state: AppState) => state.runtime.costUsd;
export const selectScreen = (state: AppState) => state.runtime.screen;
export const selectExpandedView = (state: AppState) => state.expandedView;
export const selectFooterSelection = (state: AppState) => state.footerSelection;
export const selectStatusLineText = (state: AppState) => state.statusLineText;
export const selectElicitationQueue = (state: AppState) =>
  state.elicitation.queue;
export const selectWorkerSandboxPermissions = (state: AppState) =>
  state.workerSandboxPermissions;
export const selectPendingWorkerRequest = (state: AppState) =>
  state.pendingWorkerRequest;
export const selectPendingSandboxRequest = (state: AppState) =>
  state.pendingSandboxRequest;
export const selectFastMode = (state: AppState) => state.fastMode;
export const selectActiveOverlays = (state: AppState) => state.activeOverlays;
