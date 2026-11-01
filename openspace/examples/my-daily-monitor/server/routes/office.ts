/**
 * Microsoft Graph API proxy — Office 365 recent documents.
 * Accepts MS credentials via headers, exchanges for token, fetches recent files.
 */
import type { IncomingHttpHeaders } from 'node:http';

interface MsGraphToken {
  access_token: string;
  expires_in: number;
  token_type: string;
}

interface MsGraphRecentItem {
  id: string;
  name: string;
  lastModifiedDateTime: string;
  lastModifiedBy?: {
    user?: {
      displayName?: string;
    };
  };
  file?: {
    mimeType?: string;
  };
  webUrl?: string;
}

interface OfficeDocument {
  id: string;
  name: string;
  type: 'doc' | 'xlsx' | 'ppt' | 'pdf';
  modifiedAt: string;
  modifiedBy: string;
  url?: string;
}

// Simple in-memory token cache (5 min TTL)
const tokenCache = new Map<string, { token: string; expiresAt: number }>();

/**
 * Exchange client credentials for Microsoft Graph access token.
 */
async function getAccessToken(
  clientId: string,
  clientSecret: string,
  tenantId: string
): Promise<string> {
  const cacheKey = `${clientId}:${tenantId}`;
  const cached = tokenCache.get(cacheKey);
  
  // Return cached token if still valid (with 1 min buffer)
  if (cached && cached.expiresAt > Date.now() + 60_000) {
    return cached.token;
  }

  // Request new token via OAuth2 client_credentials grant
  const tokenUrl = `https://login.microsoftonline.com/${tenantId}/oauth2/v2.0/token`;
  const params = new URLSearchParams({
    client_id: clientId,
    client_secret: clientSecret,
    scope: 'https://graph.microsoft.com/.default',
    grant_type: 'client_credentials',
  });

  const resp = await fetch(tokenUrl, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: params.toString(),
    signal: AbortSignal.timeout(10000),
  });

  if (!resp.ok) {
    const errText = await resp.text();
    throw new Error(`MS Auth failed (${resp.status}): ${errText.slice(0, 200)}`);
  }

  const data = (await resp.json()) as MsGraphToken;
  
  // Cache the token
  tokenCache.set(cacheKey, {
    token: data.access_token,
    expiresAt: Date.now() + (data.expires_in * 1000),
  });

  return data.access_token;
}

/**
 * Fetch recent documents from Microsoft Graph API.
 */
async function fetchRecentDocuments(accessToken: string): Promise<OfficeDocument[]> {
  const graphUrl = 'https://graph.microsoft.com/v1.0/me/drive/recent';
  
  const resp = await fetch(graphUrl, {
    headers: {
      Authorization: `Bearer ${accessToken}`,
      Accept: 'application/json',
    },
    signal: AbortSignal.timeout(10000),
  });

  if (!resp.ok) {
    const errText = await resp.text();
    throw new Error(`MS Graph failed (${resp.status}): ${errText.slice(0, 200)}`);
  }

  const data = await resp.json() as { value: MsGraphRecentItem[] };
  
  // Normalize the response to our format
  return (data.value || []).slice(0, 20).map(item => normalizeDocument(item));
}

/**
 * Convert MS Graph item to our OfficeDocument format.
 */
function normalizeDocument(item: MsGraphRecentItem): OfficeDocument {
  return {
    id: item.id,
    name: item.name,
    type: inferDocType(item.name, item.file?.mimeType),
    modifiedAt: item.lastModifiedDateTime,
    modifiedBy: item.lastModifiedBy?.user?.displayName || 'Unknown',
    url: item.webUrl,
  };
}

/**
 * Infer document type from filename extension and MIME type.
 */
function inferDocType(name: string, mimeType?: string): 'doc' | 'xlsx' | 'ppt' | 'pdf' {
  const lowerName = name.toLowerCase();
  
  // Check file extension
  if (lowerName.endsWith('.doc') || lowerName.endsWith('.docx')) return 'doc';
  if (lowerName.endsWith('.xls') || lowerName.endsWith('.xlsx')) return 'xlsx';
  if (lowerName.endsWith('.ppt') || lowerName.endsWith('.pptx')) return 'ppt';
  if (lowerName.endsWith('.pdf')) return 'pdf';
  
  // Check MIME type as fallback
  if (mimeType) {
    if (mimeType.includes('word')) return 'doc';
    if (mimeType.includes('spreadsheet') || mimeType.includes('excel')) return 'xlsx';
    if (mimeType.includes('presentation') || mimeType.includes('powerpoint')) return 'ppt';
    if (mimeType.includes('pdf')) return 'pdf';
  }
  
  // Default to doc
  return 'doc';
}

/**
 * Main request handler for /api/office
 */
export async function handleOfficeRequest(
  _query: Record<string, string>,
  _body: string,
  headers: IncomingHttpHeaders,
): Promise<unknown> {
  try {
    // Extract credentials from headers
    const clientId = headers['x-ms-client-id'] as string;
    const clientSecret = headers['x-ms-client-secret'] as string;
    const tenantId = headers['x-ms-tenant-id'] as string;

    // Validate required credentials
    if (!clientId || !clientSecret || !tenantId) {
      return {
        error: 'Missing Microsoft credentials',
        message: 'Please configure X-MS-Client-Id, X-MS-Client-Secret, and X-MS-Tenant-Id headers',
        documents: [],
      };
    }

    // Step 1: Get access token
    const accessToken = await getAccessToken(clientId, clientSecret, tenantId);

    // Step 2: Fetch recent documents
    const documents = await fetchRecentDocuments(accessToken);

    return { documents };
  } catch (err: unknown) {
    const error = err as Error;
    console.error('[Office API] Error:', error.message);
    
    return {
      error: error.message,
      documents: [],
    };
  }
}
