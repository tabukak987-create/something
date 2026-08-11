// Match the API host to the address used to open the web app. On another
// device, "localhost" would otherwise point to that device instead of POI.
const defaultBase = `http://${window.location.hostname}:8080/api/v1`;
const base = import.meta.env.VITE_API_URL || defaultBase;

export type Session = { accessToken: string; username: string; role: string };
export type Bond = { isin: string; issuer: string; couponRate: number | null; marketPrice: number | null; rating: string | null };
export type FileResult = { filename: string; type: string; sizeBytes: number; extractedRows: number; status: string; notes: string[] };
export type InstrumentResult = Bond & { quantity: number; faceValue: number | null; marketValue: number; completeness: number; sources: string[] };
export type Finding = { severity: 'HIGH' | 'MEDIUM' | 'LOW'; category: string; title: string; detail: string };
export type AnalysisSummary = { fileCount: number; instrumentCount: number; totalMarketValue: number; weightedCoupon: number; dataQualityScore: number; investmentGradePercent: number; decision: string };
export type AnalysisResult = {
  id: string; name: string; status: string; createdBy: string; createdAt: string; completedAt: string;
  summary: AnalysisSummary; files: FileResult[]; instruments: InstrumentResult[]; findings: Finding[];
  issuerExposure: Record<string, number>; ratingDistribution: Record<string, number>; recommendations: string[];
};
export type HistoryItem = { id: string; name: string; status: string; createdAt: string; fileCount: number; instrumentCount: number; totalMarketValue: number; dataQualityScore: number; decision: string };

async function call<T>(path: string, init: RequestInit = {}, token?: string): Promise<T> {
  const isForm = init.body instanceof FormData;
  const response = await fetch(base + path, {
    ...init,
    headers: {
      ...(!isForm ? { 'Content-Type': 'application/json' } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init.headers,
    },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { message?: string } | null;
    throw new Error(payload?.message ?? (response.status === 401 ? 'Your session has expired' : `Request failed (${response.status})`));
  }
  return response.json() as Promise<T>;
}

export const api = {
  login: (username: string, password: string) => call<Session>('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }),
  bonds: (token: string) => call<Bond[]>('/bonds', {}, token),
  history: (token: string) => call<HistoryItem[]>('/analyses', {}, token),
  analysis: (id: string, token: string) => call<AnalysisResult>(`/analyses/${id}`, {}, token),
  runAnalysis: (name: string, files: File[], token: string) => {
    const form = new FormData();
    if (name.trim()) form.append('name', name.trim());
    files.forEach(file => form.append('files', file));
    return call<AnalysisResult>('/analyses', { method: 'POST', body: form }, token);
  },
  downloadTemplate: async (token: string) => {
    const response = await fetch(`${base}/analyses/template.csv`, { headers: { Authorization: `Bearer ${token}` } });
    if (!response.ok) throw new Error('Could not download template');
    return response.blob();
  },
};
