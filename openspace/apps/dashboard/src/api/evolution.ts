import apiClient from './client';
import type {
  EvidenceRef,
  EvidenceRefPreview,
  EvolutionAction,
  EvolutionCandidate,
  EvolutionJob,
  EvolutionReviewItem,
  QualitySignalAuditRow,
} from './types';

export const evolutionApi = {
  async listJobs(params?: { status?: string; limit?: number }): Promise<EvolutionJob[]> {
    const response = await apiClient.get<{ items: EvolutionJob[] }>('/evolution/jobs', {
      params: {
        status: params?.status ?? '',
        limit: params?.limit ?? 100,
      },
    });
    return response.data.items;
  },

  async getJob(jobId: string): Promise<EvolutionJob> {
    const response = await apiClient.get<EvolutionJob>(`/evolution/jobs/${encodeURIComponent(jobId)}`);
    return response.data;
  },

  async getPacket(packetId: string): Promise<Record<string, unknown>> {
    const response = await apiClient.get<Record<string, unknown>>(
      `/evolution/packets/${encodeURIComponent(packetId)}`,
    );
    return response.data;
  },

  async getDecision(decisionId: string): Promise<Record<string, unknown>> {
    const response = await apiClient.get<Record<string, unknown>>(
      `/evolution/decisions/${encodeURIComponent(decisionId)}`,
    );
    return response.data;
  },

  async listCandidates(params?: { status?: string; limit?: number }): Promise<EvolutionCandidate[]> {
    const response = await apiClient.get<{ items: EvolutionCandidate[] }>('/evolution/candidates', {
      params: {
        status: params?.status ?? 'pending',
        limit: params?.limit ?? 100,
      },
    });
    return response.data.items;
  },

  async listReviewItems(params?: { limit?: number }): Promise<EvolutionReviewItem[]> {
    const response = await apiClient.get<{ items: EvolutionReviewItem[] }>('/evolution/review-items', {
      params: {
        limit: params?.limit ?? 100,
      },
    });
    return response.data.items;
  },

  async listQualitySignals(params?: {
    actionability?: string;
    subjectType?: string;
    subjectId?: string;
    notTriggerable?: boolean;
    limit?: number;
  }): Promise<QualitySignalAuditRow[]> {
    const response = await apiClient.get<{ items: QualitySignalAuditRow[] }>('/quality-signals', {
      params: {
        actionability: params?.actionability ?? '',
        subject_type: params?.subjectType ?? '',
        subject_id: params?.subjectId ?? '',
        not_triggerable: params?.notTriggerable ?? false,
        limit: params?.limit ?? 100,
      },
    });
    return response.data.items;
  },

  async listQualitySignalJobs(params?: { limit?: number }): Promise<QualitySignalAuditRow[]> {
    const response = await apiClient.get<{ items: QualitySignalAuditRow[] }>('/quality-signals/jobs', {
      params: {
        limit: params?.limit ?? 100,
      },
    });
    return response.data.items;
  },

  async getCandidate(candidateId: string): Promise<EvolutionCandidate> {
    const response = await apiClient.get<EvolutionCandidate>(
      `/evolution/candidates/${encodeURIComponent(candidateId)}`,
    );
    return response.data;
  },

  async rejectCandidate(candidateId: string, reason: string): Promise<EvolutionCandidate> {
    const response = await apiClient.post<EvolutionCandidate>(
      `/evolution/candidates/${encodeURIComponent(candidateId)}/reject`,
      { reason },
    );
    return response.data;
  },

  async getAction(actionId: string): Promise<EvolutionAction> {
    const response = await apiClient.get<EvolutionAction>(
      `/evolution/actions/${encodeURIComponent(actionId)}`,
    );
    return response.data;
  },

  async getEvidenceRef(refId: string, includePreview = true): Promise<EvidenceRef> {
    const response = await apiClient.get<EvidenceRef>(
      `/evidence/refs/${encodeURIComponent(refId)}`,
      { params: { include_preview: includePreview } },
    );
    return response.data;
  },

  async previewEvidenceRef(refId: string, maxChars = 2000): Promise<EvidenceRefPreview> {
    const response = await apiClient.get<EvidenceRefPreview>(
      `/evidence/refs/${encodeURIComponent(refId)}/preview`,
      { params: { max_chars: maxChars } },
    );
    return response.data;
  },
};
