import apiClient from './client';
import type { Skill, SkillDetail, SkillLineage, SkillStats } from './types';

export const skillsApi = {
  async listSkills(params?: { activeOnly?: boolean; sort?: string; limit?: number; query?: string }): Promise<Skill[]> {
    const response = await apiClient.get<{ items: Skill[] }>('/skills', {
      params: {
        active_only: params?.activeOnly ?? true,
        sort: params?.sort ?? 'score',
        limit: params?.limit ?? 200,
        query: params?.query ?? '',
      },
    });
    return response.data.items;
  },

  async getSkillStats(): Promise<SkillStats> {
    const response = await apiClient.get<SkillStats>('/skills/stats');
    return response.data;
  },

  async getSkill(skillId: string): Promise<SkillDetail> {
    const response = await apiClient.get<SkillDetail>(`/skills/${skillId}`);
    return response.data;
  },

  async getLineage(skillId: string): Promise<SkillLineage> {
    const response = await apiClient.get<SkillLineage>(`/skills/${skillId}/lineage`);
    return response.data;
  },
};
