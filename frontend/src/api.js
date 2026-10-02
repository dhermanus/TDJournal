import axios from 'axios';
import { consumeBrainStream, bodyChunks } from './components/brainStream';

// Defaults to the local backend. REACT_APP_API_URL can point the frontend at
// another origin (a second instance, a container, a LAN machine).
export const API_BASE = (process.env.REACT_APP_API_URL ?? 'http://localhost:8010').replace(/\/+$/, '');

const api = axios.create({ baseURL: API_BASE });

export const accountsApi = {
  list: () => api.get('/api/accounts'),
  create: (data) => api.post('/api/accounts', data),
  update: (id, data) => api.put(`/api/accounts/${id}`, data),
};

export const tradesApi = {
  list: (params) => api.get('/api/trades', { params }),
  create: (data) => api.post('/api/trades', data),
  update: (id, data) => api.put(`/api/trades/${id}`, data),
  delete: (id) => api.delete(`/api/trades/${id}`),
  getAnalysis: (group) => api.get(`/api/trades/${encodeURIComponent(group)}/analysis`),
  getAnalysisOptions: () => api.get('/api/analysis-options'),
  updateAnalysis: (group, data) => api.patch(`/api/trades/${encodeURIComponent(group)}/analysis`, data),
  addTag: (group, data) => api.post(`/api/trades/${encodeURIComponent(group)}/tags`, data),
  deleteTag: (tagId) => api.delete(`/api/trade-tags/${tagId}`),
  setSetup: (id, setup, note) => api.patch(`/api/trades/${id}/setup`, { setup, note }),
  listCustomSetups: () => api.get('/api/setups/custom'),
  createCustomSetup: (data) => api.post('/api/setups/custom', data),
  deleteCustomSetup: (id) => api.delete(`/api/setups/custom/${id}`),
  addExecution: (id, data) => api.post(`/api/trades/${id}/executions`, data),
  updateExecution: (id, idx, data) => api.put(`/api/trades/${id}/executions/${idx}`, data),
  deleteExecution: (id, idx) => api.delete(`/api/trades/${id}/executions/${idx}`),
};

// Files attached to a trade's review (screenshots, statements, notes).
export const attachmentsApi = {
  list: (group) => api.get(`/api/trades/${encodeURIComponent(group)}/attachments`),
  upload: (group, formData) => api.post(
    `/api/trades/${encodeURIComponent(group)}/attachments`, formData,
    { headers: { 'Content-Type': 'multipart/form-data' } },
  ),
  remove: (id) => api.delete(`/api/attachments/${id}`),
  // A download, not an axios call: it has to leave the page so the browser
  // saves the file rather than handing the bytes to JS. No `inline` here on
  // purpose — this is the save-a-copy path.
  downloadUrl: (id) => `${API_BASE}/api/attachments/${id}/download`,
  // The preview box reads from the same guarded route with `inline=true`.
  // The server ignores the flag for anything a browser cannot render.
  previewUrl: (id) => `${API_BASE}/api/attachments/${id}/download?inline=true`,
};

export const importApi = {
  importCsv: (formData) => api.post('/api/import-csv', formData, {
    headers: { 'Content-Type': 'multipart/form-data' }
  }),
  importBars: (formData) => api.post('/api/import-bars', formData, {
    headers: { 'Content-Type': 'multipart/form-data' }
  }),
  uploadDiary: (formData) => api.post('/api/upload-diary', formData, {
    headers: { 'Content-Type': 'multipart/form-data' }
  }),
};

export const kpisApi = {
  get: (params) => api.get('/api/kpis', { params }),
};

export const diaryApi = {
  list: (params) => api.get('/api/diary', { params }),
  delete: (id) => api.delete(`/api/diary/${id}`),
  deleteByDate: (date, accountId) => api.delete(`/api/diary/by-date/${date}`, { params: accountId != null ? { account_id: accountId } : {} }),
};

export const chartApi = {
  get: (ticker, date, timeframe = '1Min', daysBack = 1) =>
    api.get(`/api/chart/${encodeURIComponent(ticker)}/${date}`, { params: { timeframe, days_back: daysBack } }),
};

export const calendarApi = {
  get: (params) => api.get('/api/calendar', { params }),
};

export const brainApi = {
  chat: (messages, accountId) =>
    api.post('/api/brain', { messages, account_id: accountId }),

  /**
   * Stream one Brain turn, calling `onDelta(text)` as each piece arrives.
   *
   * Plain `fetch` rather than axios: the response is newline-delimited JSON
   * read line by line, and `signal` is what the Stop button passes so the
   * request is actually torn down instead of leaving the model to finish into
   * a socket nobody is reading.
   *
   * Resolves to { text, ai_usage }. Rejects with { status, detail } so the
   * caller can show the server's own words for a rate limit or a bad key.
   */
  stream: async (messages, accountId, { signal, onDelta } = {}) => {
    const res = await fetch(`${API_BASE}/api/brain/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages, account_id: accountId }),
      signal,
    });

    if (!res.ok) {
      let detail = `Request failed (${res.status})`;
      try {
        const body = await res.json();
        if (typeof body?.detail === 'string' && body.detail) detail = body.detail;
      } catch (e) { /* keep the status message */ }
      throw { status: res.status, detail };
    }

    const chunks = bodyChunks(res);
    if (!chunks) throw { status: 500, detail: 'This browser cannot stream the response.' };
    const { text, ai_usage: usage, failure } = await consumeBrainStream(chunks, { onDelta });
    if (failure && !text.trim()) throw failure;
    return { text, ai_usage: usage, ...(failure ? { failure } : {}) };
  },
};

export const dailySummaryApi = {
  get: (params) => api.get('/api/daily-summary', { params }),
};

export const goalsApi = {
  get: (params) => api.get('/api/goals', { params }),
  put: (data) => api.put('/api/goals', data),
};

export const reportsApi = {
  get: (params) => api.get('/api/reports', { params }),
};

// Settings > Library: strategy names, sources and tags. `item` is
// { kind: 'strategy' | 'source' | 'tag', tag_type?, name, ... }.
export const libraryApi = {
  list: () => api.get('/api/library'),
  create: (item) => api.post('/api/library', item),
  update: (item) => api.put('/api/library', item),
  merge: (item) => api.post('/api/library/merge', item),
  remove: (item) => api.post('/api/library/delete', item),
};

export const mt5TimezoneApi = {
  get: () => api.get('/api/mt5/timezone'),
  put: (timezone) => api.put('/api/mt5/timezone', { timezone }),
};

// Settings > AI: which model runs, and which AI features are allowed to send.
// `features` are booleans; `feature_info` carries label + the "sends" copy, kept
// separate so the two payload shapes do not collide when rendering.
export const aiSettingsApi = {
  get: () => api.get('/api/ai-settings'),
  put: (patch) => api.put('/api/ai-settings', patch),
};

export const backupApi = {
  getDestination: () => api.get('/api/backup/destination'),
  setDestination: (folder) => api.put('/api/backup/destination', { folder }),
  list: () => api.get('/api/backup/archives'),
  create: () => api.post('/api/backup'),
  restore: (name) => api.post('/api/backup/restore', { name }),
  exportUrl: (format) => `${API_BASE}/api/export?fmt=${encodeURIComponent(format)}`,
};

export const edgeReportApi = {
  get: (params) => api.get('/api/edge-report', { params }),
};

export const weeklySummaryApi = {
  get: (params) => api.get('/api/weekly-summary', { params }),
};

export const yearlyKpisApi = {
  get: (params) => api.get('/api/yearly-kpis', { params }),
};
