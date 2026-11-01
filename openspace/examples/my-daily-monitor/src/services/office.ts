export interface OfficeDocument {
  id: string;
  name: string;
  type: 'doc' | 'xlsx' | 'ppt' | 'pdf';
  modifiedAt: string;
  modifiedBy: string;
  url?: string;
}

const TYPE_ICONS: Record<string, string> = {
  doc: '\u{1F4DD}',   // memo
  xlsx: '\u{1F4CA}',  // bar chart
  ppt: '\u{1F4CA}',   // bar chart (presentation)
  pdf: '\u{1F4C4}',   // page facing up
};

export function getDocTypeIcon(type: string): string {
  return TYPE_ICONS[type] || '\u{1F4C4}';
}

/**
 * Get Microsoft credentials from settings store.
 * This is a placeholder - replace with your actual settings store access.
 */
function getMsCredentials(): { clientId: string; clientSecret: string; tenantId: string } | null {
  // TODO: Replace with actual settings store integration
  // Example: const settings = useSettingsStore.getState();
  // return {
  //   clientId: settings.msClientId || '',
  //   clientSecret: settings.msClientSecret || '',
  //   tenantId: settings.msTenantId || '',
  // };
  
  // For now, try to get from localStorage as fallback
  if (typeof window !== 'undefined' && window.localStorage) {
    const clientId = localStorage.getItem('ms-client-id') || '';
    const clientSecret = localStorage.getItem('ms-client-secret') || '';
    const tenantId = localStorage.getItem('ms-tenant-id') || '';
    
    if (clientId && clientSecret && tenantId) {
      return { clientId, clientSecret, tenantId };
    }
  }
  
  return null;
}

/**
 * Fetch recent Office documents via API proxy.
 * Falls back to demo data if credentials are not configured.
 */
export async function fetchRecentDocuments(): Promise<OfficeDocument[]> {
  // Try to get credentials
  const credentials = getMsCredentials();
  
  // If no credentials, use demo data
  if (!credentials) {
    console.warn('[Office] Microsoft credentials not configured, using demo data');
    return getDemoDocuments();
  }

  try {
    const resp = await fetch('/api/office', {
      method: 'GET',
      headers: {
        'X-MS-Client-Id': credentials.clientId,
        'X-MS-Client-Secret': credentials.clientSecret,
        'X-MS-Tenant-Id': credentials.tenantId,
      },
    });
    
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}`);
    }
    
    const data = await resp.json() as { documents?: OfficeDocument[]; error?: string };
    
    // If API returns an error or empty documents, fall back to demo
    if (data.error || !data.documents || data.documents.length === 0) {
      console.warn('[Office] API error or no documents:', data.error);
      return getDemoDocuments();
    }
    
    return data.documents;
  } catch (err) {
    console.error('[Office] Fetch failed:', err);
    return getDemoDocuments();
  }
}

/** Demo data for development */
export function getDemoDocuments(): OfficeDocument[] {
  const now = new Date();
  const ago = (mins: number) => new Date(now.getTime() - mins * 60_000).toISOString();

  return [
    { id: '1', name: 'Q1 Revenue Report.xlsx', type: 'xlsx', modifiedAt: ago(10), modifiedBy: 'You' },
    { id: '2', name: 'Product Roadmap 2026.ppt', type: 'ppt', modifiedAt: ago(30), modifiedBy: 'Alice Wang' },
    { id: '3', name: 'API Design Document.doc', type: 'doc', modifiedAt: ago(60), modifiedBy: 'You' },
    { id: '4', name: 'Sprint Planning Notes.doc', type: 'doc', modifiedAt: ago(120), modifiedBy: 'Bob Chen' },
    { id: '5', name: 'Budget Forecast.xlsx', type: 'xlsx', modifiedAt: ago(180), modifiedBy: 'Finance Team' },
    { id: '6', name: 'Investor Deck v3.ppt', type: 'ppt', modifiedAt: ago(360), modifiedBy: 'You' },
    { id: '7', name: 'Architecture Overview.pdf', type: 'pdf', modifiedAt: ago(720), modifiedBy: 'Li Ming' },
  ];
}
