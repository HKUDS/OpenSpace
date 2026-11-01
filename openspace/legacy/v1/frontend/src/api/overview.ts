import apiClient from './client';
import type { OverviewResponse } from './types';

export const overviewApi = {
  async getOverview(): Promise<OverviewResponse> {
    const response = await apiClient.get<OverviewResponse>('/overview');
    return response.data;
  },
};
