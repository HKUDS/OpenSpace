import apiClient from './client';
import type { WorkflowDetail, WorkflowSummary } from './types';

export const workflowsApi = {
  async listWorkflows(): Promise<WorkflowSummary[]> {
    const response = await apiClient.get<{ items: WorkflowSummary[] }>('/workflows');
    return response.data.items;
  },

  async getWorkflow(workflowId: string): Promise<WorkflowDetail> {
    const response = await apiClient.get<WorkflowDetail>(`/workflows/${workflowId}`);
    return response.data;
  },
};
