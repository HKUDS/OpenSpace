export type ProjectConfig = {
  lastSessionMetrics?: Record<string, number>;
};

let currentProjectConfig: ProjectConfig = {};

export function saveCurrentProjectConfig(
  updater: (current: ProjectConfig) => ProjectConfig,
): void {
  currentProjectConfig = updater(currentProjectConfig);
}

export function getCurrentProjectConfig(): ProjectConfig {
  return currentProjectConfig;
}
