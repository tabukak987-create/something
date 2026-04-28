/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   PF Trust â€” API Client Layer
   Handles all communication with the Unified Gateway at /api/v1
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

const API = (() => {
  const BASE = '/api/v1';
  const PRIMARY_ORIGIN = window.location.origin;
  const API_ORIGINS = [PRIMARY_ORIGIN];
  
  // Global placeholder for simulation snapshot (overridden by App)
  window.getLiveSimulationSnapshot = window.getLiveSimulationSnapshot || (() => null);
  let _token = localStorage.getItem('auth_token') || '';
  let _authExpiryHandledForToken = '';
  const DEFAULT_TRUST_ID = 'default';
  const BROKER_PROFILES_CACHE_KEY = 'pf.broker_profiles_cache.v1';
  let _brokerProfilesCache = null;
  let _brokerProfilesRequest = null;

  function firstDefined(...values) {
    for (const value of values) {
      if (value !== null && value !== undefined) return value;
    }
    return undefined;
  }

  function firstNonBlank(...values) {
    for (const value of values) {
      if (value === null || value === undefined) continue;
      if (typeof value === 'string' && value.trim() === '') continue;
      return value;
    }
    return null;
  }

  function toFiniteNumber(value, fallback = 0) {
    if (value === null || value === undefined) return fallback;
    if (typeof value === 'string' && value.trim() === '') return fallback;
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : fallback;
  }

  function getActiveTrustId() {
    try {
      const stored = localStorage.getItem('active_trust_id');
      const value = String(stored || '').trim();
      return value || DEFAULT_TRUST_ID;
    } catch (_) {
      return DEFAULT_TRUST_ID;
    }
  }

  function setActiveTrustId(trustId) {
    const value = String(trustId || '').trim() || DEFAULT_TRUST_ID;
    try {
      localStorage.setItem('active_trust_id', value);
    } catch (_) {}
    return value;
  }

  function normalizePortfolioHolding(h) {
    const quantity = toFiniteNumber(firstDefined(h.quantity, h.buy_quantity), 0);
    const faceValue = toFiniteNumber(firstDefined(h.face_value, h.nominal_value), 0);
    const bookValue = toFiniteNumber(firstDefined(h.book_value, h.investment_value), 0);
    const marketPrice = toFiniteNumber(firstDefined(h.market_price, h.clean_price, h.current_price), 100);
    const marketValue = toFiniteNumber(
      firstDefined(
        h.market_value,
        h.current_value,
        faceValue ? (marketPrice * faceValue) / 100 : undefined,
        bookValue,
      ),
      0,
    );
    const investmentValue = toFiniteNumber(
      firstDefined(
        h.investment_value,
        faceValue ? (toFiniteNumber(h.purchase_price, 0) * faceValue) / 100 : undefined,
        bookValue,
      ),
      0,
    );
    const gainPct = investmentValue ? ((marketValue - investmentValue) / investmentValue) * 100 : 0;
    return {
      ...h,
      quantity,
      face_value: faceValue,
      book_value: bookValue,
      market_price: marketPrice,
      market_value: marketValue,
      investment_value: investmentValue,
      gain_pct: h.gain_pct != null ? h.gain_pct : gainPct,
      rating: firstNonBlank(h.rating, h.credit_rating, h.rating_normalized),
      credit_rating: firstNonBlank(h.credit_rating, h.rating, h.rating_normalized),
      instrument_category: firstNonBlank(h.instrument_category, h.category, h.workbook_category, h.sheet_category, h.category_display),
      workbook_category: firstNonBlank(h.workbook_category, h.sheet_category, h.category),
      category: firstNonBlank(h.category, h.sector),
    };
  }

  function derivePortfolioSummary(holdings) {
    const totals = holdings.reduce((acc, h) => {
      acc.totalMarketValue += toFiniteNumber(h.market_value, 0);
      acc.totalInvested += toFiniteNumber(h.investment_value, 0);
      acc.holdingsCount += 1;
      return acc;
    }, { totalMarketValue: 0, totalInvested: 0, holdingsCount: 0 });
    const unrealized = totals.totalMarketValue - totals.totalInvested;
    return {
      total_market_value: totals.totalMarketValue,
      total_invested: totals.totalInvested,
      unrealized_gain: unrealized,
      unrealized_gain_pct: totals.totalInvested ? (unrealized / totals.totalInvested) * 100 : 0,
      holdings_count: totals.holdingsCount,
      aum_crores: totals.totalMarketValue / 1e7,
      invested_crores: totals.totalInvested / 1e7,
    };
  }

  function normalizeDataState(raw) {
    if (raw && typeof raw === 'object' && ['ok', 'no_data', 'error', 'restricted'].includes(String(raw.status || '').toLowerCase())) {
      return {
        status: String(raw.status).toLowerCase(),
        data: (raw.data && typeof raw.data === 'object') ? raw.data : {},
        message: raw.message || null,
      };
    }
    if (raw && typeof raw === 'object' && typeof raw.error === 'string' && raw.error.trim()) {
      const text = raw.error.trim();
      if (/no portfolio data|no holdings|no data/i.test(text)) {
        return { status: 'no_data', data: {}, message: text };
      }
      return { status: 'error', data: {}, message: text };
    }
    return { status: 'ok', data: (raw && typeof raw === 'object') ? raw : {}, message: null };
  }

  function stateFromError(err, fallbackMessage) {
    const statusCode = Number(err?.status || 0);
    const message = String(err?.message || fallbackMessage || 'Request failed').trim() || fallbackMessage || 'Request failed';
    if (statusCode === 401 || statusCode === 403) {
      return { status: 'restricted', data: {}, message: message || 'Access restricted' };
    }
    if (/no portfolio data|no holdings|no data/i.test(message)) {
      return { status: 'no_data', data: {}, message };
    }
    return { status: 'error', data: {}, message };
  }

  function stateToLegacyData(raw) {
    return normalizeDataState(raw).data;
  }

  function buildUrl(path, origin) {
    if (/^https?:\/\//i.test(path)) return path;
    const resolvedPath = path.startsWith('/api/') ? path : `${BASE}${path}`;
    return new URL(resolvedPath, origin).toString();
  }

  async function readResponseData(resp) {
    const contentType = resp.headers.get('content-type') || '';
    const text = await resp.text();
    if (!text) return null;
    if (contentType.includes('application/json')) {
      try { return JSON.parse(text); } catch (_) { return text; }
    }
    const trimmed = text.trim();
    if ((trimmed.startsWith('{') || trimmed.startsWith('['))) {
      try { return JSON.parse(trimmed); } catch (_) {}
    }
    return trimmed;
  }

  function isRetryableNetworkError(err) {
    const msg = String(err?.message || err || '');
    return err instanceof TypeError || /Failed to fetch|NetworkError|ERR_CONNECTION_REFUSED|ECONNREFUSED/i.test(msg);
  }

  function shouldRetryHttp(status) {
    return [404, 500, 502, 503, 504].includes(Number(status));
  }

  function buildErrorMessage(data, fallback) {
    if (typeof data === 'string') return data.slice(0, 500) || fallback;
    if (data && typeof data === 'object') return data.detail || data.message || data.error || fallback;
    return fallback;
  }

  function isPublicRequestPath(path) {
    const normalized = String(path || '');
    return normalized === '/auth/token' || normalized.startsWith('/auth/token?') || normalized === '/health' || normalized.startsWith('/health?');
  }

  function isExplicitErrorPayload(data) {
    if (!data || typeof data !== 'object') return false;
    if (data.success === false || data.ok === false) return true;
    return String(data.status || '').trim().toLowerCase() === 'error';
  }

  function isAuthError(err) {
    const status = Number(err?.status || 0);
    const message = String(err?.message || '');
    return status === 401 || /token validation failed|token has been revoked|session expired|expired/i.test(message);
  }

  function isTokenExpired(token = _token) {
    if (!token) return false;
    const parts = String(token).split('.');
    if (parts.length !== 3) return false;
    try {
      const payloadJson = atob(parts[1].replace(/-/g, '+').replace(/_/g, '/'));
      const payload = JSON.parse(payloadJson);
      if (!payload || typeof payload.exp !== 'number') return false;
      return Date.now() >= payload.exp * 1000;
    } catch (_) {
      return false;
    }
  }

  function setToken(t) {
    _token = t;
    _authExpiryHandledForToken = '';
    localStorage.setItem('auth_token', t);
  }

  function normalizeBrokerProfiles(payload) {
    if (Array.isArray(payload)) {
      return { profiles: payload, total: payload.length, source: 'live' };
    }
    if (payload && typeof payload === 'object') {
      if (Array.isArray(payload.profiles)) {
        return {
          ...payload,
          total: Number(payload.total ?? payload.profiles.length ?? 0),
          source: payload.source || 'live',
        };
      }
      if (Array.isArray(payload.items)) {
        return {
          ...payload,
          profiles: payload.items,
          total: Number(payload.total ?? payload.items.length ?? 0),
          source: payload.source || 'live',
        };
      }
    }
    return { profiles: [], total: 0, source: 'live' };
  }

  function readBrokerProfilesCache() {
    if (_brokerProfilesCache) return _brokerProfilesCache;
    try {
      const raw = localStorage.getItem(BROKER_PROFILES_CACHE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      const payload = parsed && parsed.payload ? parsed.payload : null;
      if (!payload) return null;
      _brokerProfilesCache = normalizeBrokerProfiles(payload);
      return _brokerProfilesCache;
    } catch (_) {
      return null;
    }
  }

  function writeBrokerProfilesCache(payload) {
    _brokerProfilesCache = normalizeBrokerProfiles(payload);
    try {
      localStorage.setItem(BROKER_PROFILES_CACHE_KEY, JSON.stringify({
        ts: Date.now(),
        payload: _brokerProfilesCache,
      }));
    } catch (_) {}
    return _brokerProfilesCache;
  }

  async function fetchBrokerProfilesWithCache(options = {}) {
    const forceRefresh = !!options.forceRefresh;
    if (!forceRefresh) {
      const cached = readBrokerProfilesCache();
      if (cached) return cached;
      if (_brokerProfilesRequest) return _brokerProfilesRequest;
    }

    _brokerProfilesRequest = get('/quotes/broker-profiles')
      .then((payload) => writeBrokerProfilesCache(payload))
      .catch((err) => {
        const cached = readBrokerProfilesCache();
        if (cached) return cached;
        throw err;
      })
      .finally(() => {
        _brokerProfilesRequest = null;
      });

    return _brokerProfilesRequest;
  }
  function clearToken() {
    _token = '';
    _authExpiryHandledForToken = '';
    localStorage.removeItem('auth_token');
  }
  function getToken() { return _token; }
  function authHeaders(includeContentType = true) {
    const h = {};
    if (includeContentType) h['Content-Type'] = 'application/json';
    return h;
  }

  async function refreshSession() {
    try {
      const resp = await fetch(buildUrl('/auth/refresh'), {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json',
        },
      });
      if (!resp.ok) return false;
      const data = await readResponseData(resp).catch(() => null);
      if (data && typeof data === 'object' && data.access_token) {
        setToken(String(data.access_token || ''));
      }
      return true;
    } catch (_) {
      return false;
    }
  }

  async function request(method, path, body = null, opts = {}) {
    const upperMethod = String(method || 'GET').toUpperCase();
    const headers = { ...(opts.headers || {}) };
    const token = getToken();
    if (token && !isPublicRequestPath(path)) {
      headers.Authorization = `Bearer ${token}`;
    }
    const config = { method: upperMethod, ...opts, headers, credentials: opts.credentials || 'include' };
    if (body != null && upperMethod !== 'GET') {
      if (body instanceof FormData) {
        delete config.headers['Content-Type'];
        config.body = body;
      } else if (body instanceof URLSearchParams) {
        delete config.headers['Content-Type'];
        config.body = body;
      } else if (typeof body === 'string') {
        config.body = body;
      } else {
        if (!Object.keys(config.headers).some((key) => key.toLowerCase() === 'content-type')) {
          config.headers['Content-Type'] = 'application/json';
        }
        config.body = JSON.stringify(body);
      }
    }

    const isAbsolute = /^https?:\/\//i.test(path);
    const origins = isAbsolute ? [null] : API_ORIGINS;
    let lastError = null;
    let lastStatus = null;
    const requestToken = _token;
    const canRefreshSession = !opts._skipSessionRefresh
      && !String(path).startsWith('/auth/token')
      && !String(path).startsWith('/auth/refresh');

    for (let i = 0; i < origins.length; i += 1) {
      const url = isAbsolute ? path : buildUrl(path, origins[i]);
      let resp;
      try {
        resp = await fetch(url, config);
      } catch (err) {
        lastError = err;
        if (i < origins.length - 1 && isRetryableNetworkError(err)) {
          continue;
        }
        throw err;
      }

      if (resp.status === 401) {
        lastStatus = 401;
        if (canRefreshSession) {
          const refreshed = await refreshSession();
          if (refreshed) {
            return request(method, path, body, { ...opts, _skipSessionRefresh: true });
          }
        }
      }

      if (!resp.ok) {
        lastStatus = resp.status;
        if (i < origins.length - 1 && shouldRetryHttp(resp.status)) {
          lastError = new Error(`HTTP ${resp.status}`);
          continue;
        }
        const errData = await readResponseData(resp).catch(() => null);
        const message = buildErrorMessage(errData, resp.statusText || `HTTP ${resp.status}`);
        const ex = new Error(message || `HTTP ${resp.status}`);
        ex.status = resp.status;
        if (resp.status === 401 && requestToken && requestToken === _token) {
          if (_authExpiryHandledForToken !== requestToken) {
            _authExpiryHandledForToken = requestToken;
            if (typeof App !== 'undefined' && App.handleSessionExpired) {
              App.handleSessionExpired(ex);
            } else if (typeof App !== 'undefined' && App.logout) {
              App.logout();
            }
          }
        }
        throw ex;
      }

      const data = await readResponseData(resp);
      if (isExplicitErrorPayload(data)) {
        const message = buildErrorMessage(data, resp.statusText || `HTTP ${resp.status}`);
        const ex = new Error(message || `HTTP ${resp.status}`);
        ex.status = resp.status;
        ex.payload = data;
        throw ex;
      }

      return data;
    }

    if (lastStatus === 401) {
      const authError = lastError instanceof Error ? lastError : new Error('Session expired. Please sign in again.');
      authError.status = 401;
      const expiredSession = typeof App !== 'undefined' && App.hasExpiredSession && App.hasExpiredSession();
      if (expiredSession && typeof App !== 'undefined' && App.handleSessionExpired) {
        App.handleSessionExpired(authError);
      } else if (typeof App !== 'undefined' && App.handleAuthError) {
        App.handleAuthError(new Error('Sign in to continue.'), 'Sign in to continue.');
      } else if (typeof App !== 'undefined' && App.logout) {
        App.logout();
      }
      throw authError;
    }

    throw lastError || new Error('Request failed');
  }

  const get  = (p, o) => request('GET', p, null, o);
  const post = (p, b, o) => request('POST', p, b, o);
  const put  = (p, b, o) => request('PUT', p, b, o);
  const del  = (p, o) => request('DELETE', p, null, o);

  return {
    setToken, clearToken, getToken, authHeaders, get, post, isAuthError, isTokenExpired, refreshSession,

    // â”€â”€ Auth â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    login: (username, password) => {
      const form = new URLSearchParams();
      form.append('username', username);
      form.append('password', password);
      return request('POST', '/auth/token', form, {
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      }).then((data) => {
        if (data && data.access_token) {
          setToken(String(data.access_token || ''));
        }
        return data;
      });
    },
    authMe: () => get('/auth/me'),
    listUsers: () => get('/auth/users'),
    registerUser: (payload) => post('/auth/register', payload),

    // â”€â”€ Health â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    health: () => get('/health'),
    healthLog: (n = 50) => get(`/health/log?last_n=${n}`),

    // â”€â”€ Intelligence / Bonds â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    listBonds: (params = {}) => {
      const q = new URLSearchParams();
      if (params.issuer_type) q.set('issuer_type', params.issuer_type);
      if (params.credit_rating) q.set('credit_rating', params.credit_rating);
      if (params.sector) q.set('sector', params.sector);
      if (params.keyword) q.set('keyword', params.keyword);
      if (params.pf_eligible !== undefined) q.set('pf_eligible', params.pf_eligible);
      if (params.invested_only !== undefined) q.set('invested_only', params.invested_only);
      q.set('limit', params.limit || 100);
      if (params.offset !== undefined && params.offset !== null) q.set('offset', params.offset);
      return get(`/intelligence/bonds?${q}`);
    },
    getBond: (isin) => get(`/intelligence/bonds/${isin}`),
    getInstitutionalAnalysis: (isin, investmentAmountCr = 10, rateShockBps = 100) => {
      const q = new URLSearchParams();
      q.set('investment_amount_cr', investmentAmountCr);
      q.set('rate_shock_bps', rateShockBps);
      return get(`/intelligence/analysis/${encodeURIComponent(isin)}?${q}`);
    },
    getInstitutionalAnalysisAudit: (isin, limit = 10) => {
      const q = new URLSearchParams();
      q.set('limit', limit);
      return get(`/intelligence/analysis/${encodeURIComponent(isin)}/audit?${q}`);
    },
    getIntelligenceOpsObservability: (liveLimit = 25) => {
      const q = new URLSearchParams();
      q.set('live_limit', liveLimit);
      return get(`/intelligence/ops/observability?${q}`);
    },
    getIntelligenceOpsDrilldown: (isin, limit = 20) => {
      const q = new URLSearchParams();
      q.set('limit', limit);
      return get(`/intelligence/ops/observability/${encodeURIComponent(isin)}?${q}`);
    },
    getIntelligencePredictiveWatchlist: (top = 10) => {
      const q = new URLSearchParams();
      q.set('top', top);
      return get(`/intelligence/ops/predictive?${q}`);
    },
    getIntelligenceWeeklyImprovementReport: (days = 7) => {
      const q = new URLSearchParams();
      q.set('days', days);
      return get(`/intelligence/ops/weekly-improvement-report?${q}`);
    },
    submitIntelligenceFeedback: (payload) => post('/intelligence/ops/feedback', payload),
    listIntelligenceFeedback: (days = 30, limit = 100) => {
      const q = new URLSearchParams();
      q.set('days', days);
      q.set('limit', limit);
      return get(`/intelligence/ops/feedback?${q}`);
    },
    getMarketData: (isin, days = 30) => get(`/intelligence/market-data/${isin}?days=${days}`),
    getTopBonds: (limit = 20) => get(`/intelligence/top-bonds?limit=${limit}`),
    getRegistryStats: () => get('/intelligence/registry/stats'),
    exportBonds: (format = 'csv', issuerType) => {
      const q = new URLSearchParams({ format });
      if (issuerType) q.set('issuer_type', issuerType);
      return get(`/intelligence/bonds/export?${q}`);
    },
    
    // â”€â”€ Portfolio â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    getActiveTrustId,
    setActiveTrustId,
    listTrusts: (params = {}) => {
      const q = new URLSearchParams();
      q.set('limit', params.limit != null ? params.limit : 25);
      return get(`/portfolio/trusts?${q}`);
    },

    getPortfolio: async (params = {}) => {
      const trustId = params.trust_id || getActiveTrustId();
      const primary = await get(`/portfolio/?trust_id=${encodeURIComponent(trustId)}`);
      const primaryHoldings = Array.isArray(primary?.holdings)
        ? primary.holdings
        : Array.isArray(primary?.primary_table)
          ? primary.primary_table
          : [];
      const normalizedHoldings = primaryHoldings.map(normalizePortfolioHolding);
      primary.holdings = normalizedHoldings;

      const derivedSummary = derivePortfolioSummary(normalizedHoldings);
      const summary = (primary && typeof primary.summary === 'object' && primary.summary) ? { ...primary.summary } : {};
      if (summary.total_market_value == null) summary.total_market_value = derivedSummary.total_market_value;
      if (summary.total_invested == null) summary.total_invested = derivedSummary.total_invested;
      if (summary.unrealized_gain == null) summary.unrealized_gain = derivedSummary.unrealized_gain;
      if (summary.unrealized_gain_pct == null) summary.unrealized_gain_pct = derivedSummary.unrealized_gain_pct;
      if (summary.holdings_count == null) summary.holdings_count = derivedSummary.holdings_count;
      if (summary.aum_crores == null) summary.aum_crores = derivedSummary.aum_crores;
      if (summary.invested_crores == null) summary.invested_crores = derivedSummary.invested_crores;
      primary.summary = summary;
      return primary;
    },
    getPortfolioAnalytics: (params = {}) => {
      const trustId = params.trust_id || getActiveTrustId();
      return get(`/portfolio/analytics?trust_id=${encodeURIComponent(trustId)}`).then((resp) => {
        const snapshot = getLiveSimulationSnapshot();
        if (!snapshot) return resp;
        return {
          ...(resp || {}),
          summary: overlaySummaryWithSnapshot(resp?.summary || {}, snapshot),
          simulation_overlay: true,
          simulation_session_id: snapshot.session_id || null,
          simulation_tick_date: snapshot.timeline?.date || snapshot.date || null,
        };
      });
    },
    getPortfolioSectorBreakdown: (params = {}) => {
      const trustId = params.trust_id || getActiveTrustId();
      return get(`/portfolio/sector-breakdown?trust_id=${encodeURIComponent(trustId)}`);
    },
    getPortfolioConcentration: (params = {}) => {
      const trustId = params.trust_id || getActiveTrustId();
      return get(`/portfolio/concentration?trust_id=${encodeURIComponent(trustId)}`).then(stateToLegacyData);
    },
    getPortfolioConcentrationState: (params = {}) => {
      const trustId = params.trust_id || getActiveTrustId();
      return get(`/portfolio/concentration?trust_id=${encodeURIComponent(trustId)}`)
        .then(normalizeDataState)
        .catch((err) => stateFromError(err, 'Concentration data unavailable'));
    },
    getPortfolioHealth: (params = {}) => {
      const trustId = params.trust_id || getActiveTrustId();
      return get(`/portfolio/health?trust_id=${encodeURIComponent(trustId)}`).then((resp) => {
        const snapshot = getLiveSimulationSnapshot();
        if (!snapshot) return resp;
        return {
          ...(resp || {}),
          summary: overlaySummaryWithSnapshot(resp?.summary || {}, snapshot),
          simulation_overlay: true,
          simulation_session_id: snapshot.session_id || null,
          simulation_tick_date: snapshot.timeline?.date || snapshot.date || null,
        };
      });
    },
    getMaturityLadder: (params = {}) => {
      const trustId = params.trust_id || getActiveTrustId();
      return get(`/portfolio/maturity-ladder?trust_id=${encodeURIComponent(trustId)}`);
    },
    getCashFlows: (params = {}) => {
      const trustId = params.trust_id || getActiveTrustId();
      return get(`/portfolio/cash-flows?trust_id=${encodeURIComponent(trustId)}`);
    },
    getBondHealth: (params = {}) => {
      const trustId = params.trust_id || getActiveTrustId();
      return get(`/portfolio/bond-health?trust_id=${encodeURIComponent(trustId)}`);
    },
    getTradeHistory: (limit = 100, params = {}) => {
      const trustId = params.trust_id || getActiveTrustId();
      return get(`/portfolio/trades?limit=${limit}&trust_id=${encodeURIComponent(trustId)}`);
    },
    executeTrade: (trade) => post('/portfolio/trade', trade),
    createSimulationSession: (payload = {}) => {
      const body = {
        trust_id: payload.trust_id || getActiveTrustId(),
        start_date: payload.start_date || null,
        end_date: payload.end_date || null,
        step_mode: payload.step_mode || 'trading_day',
        intraday_steps: payload.intraday_steps != null ? payload.intraday_steps : 1,
        speed_multiplier: payload.speed_multiplier != null ? payload.speed_multiplier : 1,
        hybrid_mode: payload.hybrid_mode !== false,
        noise_level: payload.noise_level != null ? payload.noise_level : 1,
        scenario_mode: payload.scenario_mode || 'normal',
      };
      return post('/simulation/session', body);
    },
    listSimulationSessions: (params = {}) => {
      const q = new URLSearchParams();
      if (params.trust_id) q.set('trust_id', params.trust_id);
      const qs = q.toString();
      return get(`/simulation/sessions${qs ? `?${qs}` : ''}`);
    },
    getSimulationSession: (sessionId) => get(`/simulation/${encodeURIComponent(sessionId)}`),
    getSimulationSnapshot: (sessionId) => get(`/simulation/${encodeURIComponent(sessionId)}/snapshot`),
    stepSimulationSession: (sessionId, steps = 1) => {
      const body = { steps: steps != null ? steps : 1 };
      return post(`/simulation/${encodeURIComponent(sessionId)}/step`, body);
    },
    controlSimulationSession: (sessionId, payload = {}) => {
      return post(`/simulation/${encodeURIComponent(sessionId)}/control`, payload);
    },
    getSimulationTimeline: (sessionId) => get(`/simulation/${encodeURIComponent(sessionId)}/timeline`),
    getSimulationLogs: (sessionId, limit = 200) => get(`/simulation/${encodeURIComponent(sessionId)}/logs?limit=${limit}`),
    closeSimulationSession: (sessionId) => del(`/simulation/${encodeURIComponent(sessionId)}`),
    getInvestmentRecommendation: async (portfolioId, investmentAmount, investmentDate) => {
      const payload = {
        portfolio_id: portfolioId || getActiveTrustId(),
        investment_amount: investmentAmount,
      };
      if (investmentDate) payload.investment_date = investmentDate;
      try {
        // Primary route mounted under /api/v1.
        return await post('/portfolio/investment-recommendation', payload);
      } catch (err) {
        if (err && err.status && err.status !== 404 && err.status !== 405) {
          throw err;
        }
        const msg = String(err?.message || '');
        // Backward-compatible alias for legacy clients only when primary route is unavailable.
        if (!/404|not found/i.test(msg)) throw err;
        return request('POST', '/api/portfolio/investment-recommendation', payload);
      }
    },

    // â”€â”€ Risk & Analysis â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    getRiskAnalytics: () => get('/risk/'),
    getRateSensitivity: () => get('/risk/rate-sensitivity').then(stateToLegacyData),
    getSpreadSimulation: () => get('/risk/spread-simulation').then(stateToLegacyData),
    getScenarios: () => get('/risk/scenarios').then(stateToLegacyData),
    getVaR: () => get('/risk/var').then(stateToLegacyData),
    getYieldCurve: () => get('/risk/yield-curve').then(stateToLegacyData),
    getRateSensitivityState: () => get('/risk/rate-sensitivity').then(normalizeDataState).catch((err) => stateFromError(err, 'Rate sensitivity unavailable')),
    getSpreadSimulationState: () => get('/risk/spread-simulation').then(normalizeDataState).catch((err) => stateFromError(err, 'Spread simulation unavailable')),
    getScenariosState: () => get('/risk/scenarios').then(normalizeDataState).catch((err) => stateFromError(err, 'Scenario analysis unavailable')),
    getVaRState: () => get('/risk/var').then(normalizeDataState).catch((err) => stateFromError(err, 'VaR unavailable')),
    getYieldCurveState: () => get('/risk/yield-curve').then(normalizeDataState).catch((err) => stateFromError(err, 'Yield curve unavailable')),

    refreshMarketData: (max = 500) => post(`/market-data/refresh-all?max_bonds=${max}`),
    seedImData: () => post('/market-data/im/seed-missing'),
    refreshBondData: (isin) => post(`/market-data/refresh/${isin}`),
    runScoring: (limit = 500) => post(`/intelligence/scoring/run?limit=${limit}`),
    getMarketReport: () => get('/intelligence/reports/market-analysis'),

    // â”€â”€ Compliance â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    preTradeCheck: (isin, faceValueCr = 10, overrideReason) => post('/compliance/pre-trade', {
      isin, face_value_cr: faceValueCr, override_reason: overrideReason
    }),
    complianceSummary: () => get('/compliance/summary').then(stateToLegacyData),
    complianceSummaryState: () => get('/compliance/summary').then(normalizeDataState).catch((err) => stateFromError(err, 'Compliance summary unavailable')),
    complianceRules: () => get('/compliance/rules').then(stateToLegacyData),
    complianceRulesState: () => get('/compliance/rules').then(normalizeDataState).catch((err) => stateFromError(err, 'Compliance rules unavailable')),
    complianceAudit: (limit = 50) => get(`/compliance/audit?limit=${limit}`),
    getComplianceDashboardSummary: (params = {}) => {
      const q = new URLSearchParams();
      q.set('trust_id', params.trust_id || getActiveTrustId());
      if (params.days != null) q.set('days', params.days);
      return get(`/compliance-dashboard/summary?${q}`);
    },
    getComplianceDashboardAlerts: (params = {}) => {
      const q = new URLSearchParams();
      if (params.severity) q.set('severity', params.severity);
      if (params.rule) q.set('rule', params.rule);
      if (params.status) q.set('status', params.status);
      if (params.limit != null) q.set('limit', params.limit);
      return get(`/compliance-dashboard/alerts?${q}`);
    },
    getComplianceDashboardAuditTrail: (params = {}) => {
      const q = new URLSearchParams();
      if (params.user_id) q.set('user_id', params.user_id);
      if (params.action_type) q.set('action_type', params.action_type);
      if (params.entity_type) q.set('entity_type', params.entity_type);
      if (params.start_date) q.set('start_date', params.start_date);
      if (params.end_date) q.set('end_date', params.end_date);
      if (params.limit != null) q.set('limit', params.limit);
      if (params.offset != null) q.set('offset', params.offset);
      return get(`/compliance-dashboard/audit-trail?${q}`);
    },
    getComplianceDashboardAuditEntry: (entryId) => get(`/compliance-dashboard/audit-trail/${entryId}`),
    explainComplianceNumber: (metric, params = {}) => {
      const q = new URLSearchParams();
      q.set('metric', metric || 'weighted_ytm');
      q.set('trust_id', params.trust_id || getActiveTrustId());
      return get(`/compliance-dashboard/explain-number?${q}`);
    },
    getComplianceDashboardVersions: (params = {}) => {
      const q = new URLSearchParams();
      q.set('trust_id', params.trust_id || getActiveTrustId());
      if (params.portfolio_id) q.set('portfolio_id', params.portfolio_id);
      if (params.limit != null) q.set('limit', params.limit);
      return get(`/compliance-dashboard/versions?${q}`);
    },
    compareComplianceDashboardVersions: (leftVersion, rightVersion, params = {}) => {
      const q = new URLSearchParams();
      q.set('left_version', leftVersion);
      q.set('right_version', rightVersion);
      q.set('trust_id', params.trust_id || getActiveTrustId());
      if (params.portfolio_id) q.set('portfolio_id', params.portfolio_id);
      return get(`/compliance-dashboard/versions/compare?${q}`);
    },
    getComplianceDashboardAccessControl: (params = {}) => {
      const q = new URLSearchParams();
      if (params.trust_id) q.set('trust_id', params.trust_id);
      if (params.limit != null) q.set('limit', params.limit);
      return get(`/compliance-dashboard/access-control?${q}`);
    },
    getComplianceDashboardBackupStatus: () => get('/compliance-dashboard/backup-status'),
    getComplianceDashboardExportCenter: (params = {}) => {
      const q = new URLSearchParams();
      if (params.trust_id) q.set('trust_id', params.trust_id);
      const qs = q.toString();
      return get(`/compliance-dashboard/export-center${qs ? `?${qs}` : ''}`);
    },
    getComplianceDashboardAuditReport: (params = {}) => {
      const q = new URLSearchParams();
      q.set('format', params.format || 'json');
      q.set('days', params.days != null ? params.days : 30);
      q.set('trust_id', params.trust_id || getActiveTrustId());
      return get(`/compliance-dashboard/export/audit-report?${q}`);
    },

    // â”€â”€ Decision Registry / Explainability â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    listDecisions: (params = {}) => {
      const q = new URLSearchParams();
      q.set('limit', params.limit != null ? params.limit : 100);
      if (params.decision_type) q.set('decision_type', params.decision_type);
      if (params.status) q.set('status', params.status);
      return get(`/decisions/?${q}`);
    },
    getDecision: (decisionId) => get(`/decisions/${encodeURIComponent(decisionId)}`),
    getDecisionEvidence: (decisionId) => get(`/decisions/${encodeURIComponent(decisionId)}/evidence`),
    getDecisionExplanation: (decisionId) => get(`/decisions/${encodeURIComponent(decisionId)}/explanation`),
    getDecisionReplayTimeline: (decisionId) => get(`/decisions/${encodeURIComponent(decisionId)}/replay`),
    executeDecisionReplay: (decisionId) => post(`/decisions/${encodeURIComponent(decisionId)}/replay/execute`, {}),
    getDecisionTrustPacket: (decisionId) => get(`/decisions/${encodeURIComponent(decisionId)}/trust-packet`),
    getDecisionAuditPack: (decisionId) => get(`/decisions/${encodeURIComponent(decisionId)}/audit-pack`),
    requestDecisionOverride: (payload) => post('/decisions/overrides/request', payload),
    approveDecisionOverride: (payload) => post('/decisions/overrides/approve', payload),
    getDecisionWorkbenchSummary: () => get('/decisions/workbench/summary'),
    getDecisionCommandCenter: (limit = 100) => get(`/decisions/command-center?limit=${limit}`),
    getPendingDecisionIntents: (limit = 100) => get(`/decisions/intents/pending?limit=${limit}`),
    reconcileDecisionIntents: (limit = 50) => post(`/decisions/intents/reconcile?limit=${limit}`, {}),
    explainDecision: (decisionId) => get(`/explain/decision/${encodeURIComponent(decisionId)}`),
    explainComplianceDecision: (decisionId) => get(`/explain/compliance/${encodeURIComponent(decisionId)}`),
    explainRiskDecision: (decisionId) => get(`/explain/risk/${encodeURIComponent(decisionId)}`),
    getTrustAuditIntegrity: (lastN = 500) => get(`/governance/audit-log/trust/integrity?last_n=${lastN}`),
    createTrustChainAnchor: (payload = {}) => post('/governance/audit-log/trust/anchor', payload),
    getLatestTrustChainAnchor: () => get('/governance/audit-log/trust/anchor/latest'),

    // â”€â”€ Quotes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    listQuotes: (params = {}) => {
      const q = new URLSearchParams();
      if (params.broker) q.set('broker', params.broker);
      if (params.date_from) q.set('date_from', params.date_from);
      q.set('limit', params.limit || 100);
      return get(`/quotes/?${q}`);
    },
    getQuotesForIsin: (isin, days = 7) => get(`/quotes/${isin}?days=${days}`),
    ingestQuote: (data) => post('/quotes/ingest', data),
    brokerStats: () => get('/quotes/stats/brokers'),
    getBestQuotes: (limit = 100, keyword, asOfDate) => {
      const q = new URLSearchParams({ limit });
      if (keyword) q.set('keyword', keyword);
      if (asOfDate) q.set('as_of_date', asOfDate);
      return get(`/quotes/best?${q}`);
    },
    getTodayQuotes: (broker, category, limit = 200) => {
      const q = new URLSearchParams({ limit });
      if (broker) q.set('broker', broker);
      if (category) q.set('category', category);
      return get(`/quotes/today?${q}`);
    },
    getBrokerProfiles: () => fetchBrokerProfilesWithCache(),
    refreshBrokerProfiles: () => post('/quotes/broker-profiles/refresh').then(() => fetchBrokerProfilesWithCache({ forceRefresh: true })),
    getBrokerDetail: (name) => get(`/quotes/broker/${encodeURIComponent(name)}`),
    compareISINBrokers: (isin, asOfDate) => {
      const q = new URLSearchParams();
      if (asOfDate) q.set('as_of_date', asOfDate);
      return get(`/quotes/compare/${encodeURIComponent(isin)}?${q}`);
    },
    getISINHistory: (isin, days = 30, broker) => {
      const q = new URLSearchParams({ days });
      if (broker) q.set('broker', broker);
      return get(`/quotes/history/${encodeURIComponent(isin)}?${q}`);
    },
    uploadBrokerQuoteFile: (file, brokerOverride) => {
      const fd = new FormData();
      fd.append('file', file);
      if (brokerOverride) fd.append('broker_override', brokerOverride);
      return request('POST', '/quotes/upload', fd, { headers: {} });
    },
    ingestWorkspaceFiles: () => post('/quotes/ingest-workspace'),
    getIngestLog: (limit = 50) => get(`/quotes/ingest-log?limit=${limit}`),
    triggerGmailIngest: () => post('/quotes/ingest/gmail'),
    getUnknownBrokers: () => get('/quotes/unknown-brokers'),
    renameBroker: (oldName, newName, sourceFile) => {
      const body = { old_name: oldName, new_name: newName };
      if (sourceFile) body.source_file = sourceFile;
      return request('PATCH', '/quotes/rename-broker', body, { headers: { 'Content-Type': 'application/json' } });
    },
    getPortfolioUploadBatch: (batchId) => get(`/datasources/portfolio/uploads/${encodeURIComponent(batchId)}`),

    // â”€â”€ Alerts â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    runComplianceScan: (params = {}) => {
      const q = new URLSearchParams();
      q.set('trust_id', params.trust_id || getActiveTrustId());
      return post(`/institutional/compliance/scan?${q.toString()}`);
    },
    listAlerts: (params = {}) => {
      const q = new URLSearchParams();
      q.set('trust_id', params.trust_id || getActiveTrustId());
      if (params.severity) q.set('severity', params.severity);
      if (params.alert_date) q.set('alert_date', params.alert_date);
      if (params.is_resolved !== undefined) q.set('is_resolved', params.is_resolved);
      q.set('limit', params.limit || 200);
      return get(`/alerts/?${q}`);
    },
    alertSummary: (params = {}) => {
      const q = new URLSearchParams();
      q.set('trust_id', params.trust_id || getActiveTrustId());
      return get(`/alerts/summary?${q}`).then(stateToLegacyData);
    },
    alertSummaryState: (params = {}) => {
      const q = new URLSearchParams();
      q.set('trust_id', params.trust_id || getActiveTrustId());
      return get(`/alerts/summary?${q}`)
        .then(normalizeDataState)
        .catch((err) => stateFromError(err, 'Alert summary unavailable'));
    },
    alertsForBond: (isin, params = {}) => {
      const q = new URLSearchParams();
      q.set('trust_id', params.trust_id || getActiveTrustId());
      return get(`/alerts/bond/${isin}?${q}`);
    },
    runAlertEngine: () => post('/alerts/run'),
    resolveAlert: (id) => {
      const q = new URLSearchParams({ resolved_by: 'user' });
      return request('PATCH', `/api/v1/alerts/${id}/resolve?${q}`, null, { headers: {} });
    },

    // â”€â”€ Agent / Recommendations â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    getRecommendations: () => get('/agent/recommendations').catch(() => ({ recommendations: [] })),
  };
})();
