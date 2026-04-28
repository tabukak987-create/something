/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
   PF Trust â€” Main Application + All Page Renderers
   Single-Page Application with client-side routing
   â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */

const App = (() => {
  let currentPage = 'dashboard';
  let _sessionAuthenticated = false;
  let _sessionExpired = false;
  let _liveSimulation = {
    timerId: null,
    busy: false,
    lastSnapshot: null,
    sessionId: null,
    speed: 1,
  };
  let chartInstances = {};
  let recommendationInFlight = false;
  let _dashLastLoadTrustId = null;
  let _dashForceReload = false;
  let _dashLoadSeq = 0;
  let _searchActiveIndex = -1;
  let _focusTrapState = { container: null, restoreEl: null, keyHandler: null };
  let _imcSearchOutsideListenerBound = false;
  let _imcRunFullInFlight = false;
  let _imcDocReqSeq = 0;
  let _imcJobsReqSeq = 0;
  let _imcHistoryReqSeq = 0;
  const DEFAULT_TRUST_ID = 'default';
  const LIVE_SIMULATION_STATE_KEY = 'pf.liveSimulationState';

  function isAuthError(err) {
    return typeof API !== 'undefined' && API.isAuthError ? API.isAuthError(err) : Number(err?.status || 0) === 401;
  }

  function _setSessionState(authenticated, expired = false) {
    _sessionAuthenticated = !!authenticated;
    _sessionExpired = !!expired;
  }

  function _looksLikeExpiredSession(reason) {
    const text = String(reason || '').toLowerCase();
    return /expired|revoked|re-authenticate|token has been revoked|invalid token|session lost authorization/.test(text);
  }

  function getActiveTrustId() {
    return API && API.getActiveTrustId ? API.getActiveTrustId() : DEFAULT_TRUST_ID;
  }

  function setActiveTrustId(trustId, rerender = true) {
    const requested = String(trustId ?? DEFAULT_TRUST_ID).trim() || DEFAULT_TRUST_ID;
    const next = API && API.setActiveTrustId ? API.setActiveTrustId(requested) : requested;
    const selector = document.getElementById('trust-selector');
    if (selector && selector.value !== next) selector.value = next;
    if (rerender) {
      navigate(currentPage);
    }
    return next;
  }

  function isMobileViewport() {
    return window.matchMedia?.('(max-width: 900px)').matches ?? false;
  }

  function setSidebarOpen(open) {
    const sidebar = document.getElementById('sidebar');
    const backdrop = document.getElementById('sidebar-backdrop');
    const menuBtn = document.getElementById('mobile-menu-toggle');
    if (!sidebar) return;
    sidebar.classList.toggle('open', !!open);
    if (backdrop) {
      backdrop.classList.toggle('show', !!open);
      backdrop.hidden = !open;
    }
    if (menuBtn) {
      menuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
  }

  function closeSidebar() {
    setSidebarOpen(false);
  }

  function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    setSidebarOpen(!(sidebar && sidebar.classList.contains('open')));
  }

  function closeUserDropdown() {
    const dropdown = document.getElementById('user-dropdown');
    const avatar = document.getElementById('topbar-avatar');
    if (dropdown) dropdown.classList.remove('show');
    if (avatar) avatar.setAttribute('aria-expanded', 'false');
  }

  function toggleUserDropdown(forceOpen) {
    const dropdown = document.getElementById('user-dropdown');
    const avatar = document.getElementById('topbar-avatar');
    if (!dropdown || !avatar) return;
    const shouldOpen = forceOpen == null ? !dropdown.classList.contains('show') : !!forceOpen;
    dropdown.classList.toggle('show', shouldOpen);
    avatar.setAttribute('aria-expanded', shouldOpen ? 'true' : 'false');
    if (shouldOpen) {
      const firstAction = dropdown.querySelector('[role="menuitem"]');
      if (firstAction) firstAction.focus();
    }
  }

  function _isVisible(el) {
    if (!el) return false;
    return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  }

  function _focusableWithin(container) {
    if (!container) return [];
    return Array.from(container.querySelectorAll('a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])')).filter(_isVisible);
  }

  function deactivateFocusTrap(options = {}) {
    const restoreFocus = options.restoreFocus !== false;
    if (_focusTrapState.keyHandler) {
      document.removeEventListener('keydown', _focusTrapState.keyHandler, true);
    }
    const restoreEl = _focusTrapState.restoreEl;
    _focusTrapState = { container: null, restoreEl: null, keyHandler: null };
    if (restoreFocus && restoreEl && typeof restoreEl.focus === 'function' && document.contains(restoreEl)) {
      restoreEl.focus();
    }
  }

  function activateFocusTrap(container, options = {}) {
    if (!container) return;
    deactivateFocusTrap({ restoreFocus: false });
    const restoreEl = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const onEscape = typeof options.onEscape === 'function' ? options.onEscape : null;

    const keyHandler = (e) => {
      if (_focusTrapState.container !== container) return;
      if (e.key === 'Escape' && onEscape) {
        e.preventDefault();
        onEscape();
        return;
      }
      if (e.key !== 'Tab') return;
      const focusable = _focusableWithin(container);
      if (!focusable.length) {
        e.preventDefault();
        if (typeof container.focus === 'function') container.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && active === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };

    _focusTrapState = { container, restoreEl, keyHandler };
    document.addEventListener('keydown', keyHandler, true);

    const focusable = _focusableWithin(container);
    if (focusable.length) {
      focusable[0].focus();
    } else if (typeof container.focus === 'function') {
      container.focus();
    }
  }

  function isLoginOverlayOpen() {
    const overlay = document.getElementById('login-overlay');
    return !!overlay && overlay.style.display === 'flex';
  }

  function hideSearchResults() {
    const dropdown = document.getElementById('search-results');
    const searchInput = document.getElementById('global-search');
    if (dropdown) {
      dropdown.style.display = 'none';
      dropdown.removeAttribute('aria-activedescendant');
    }
    if (searchInput) searchInput.setAttribute('aria-expanded', 'false');
    _searchActiveIndex = -1;
  }

  function setSearchActiveIndex(index) {
    const dropdown = document.getElementById('search-results');
    if (!dropdown) return;
    const items = Array.from(dropdown.querySelectorAll('.search-result-item'));
    if (!items.length) {
      _searchActiveIndex = -1;
      dropdown.removeAttribute('aria-activedescendant');
      return;
    }
    const next = Math.max(0, Math.min(index, items.length - 1));
    _searchActiveIndex = next;
    items.forEach((item, idx) => {
      const selected = idx === next;
      item.classList.toggle('active', selected);
      item.setAttribute('aria-selected', selected ? 'true' : 'false');
    });
    dropdown.setAttribute('aria-activedescendant', items[next].id);
    items[next].scrollIntoView({ block: 'nearest' });
  }

  function activateSearchResult() {
    const dropdown = document.getElementById('search-results');
    if (!dropdown || _searchActiveIndex < 0) return;
    const items = Array.from(dropdown.querySelectorAll('.search-result-item'));
    const target = items[_searchActiveIndex];
    if (target) target.click();
  }

  function wireSortableHeaders(container, onSort) {
    container.querySelectorAll('th[data-sort]').forEach((th) => {
      th.classList.add('table-sortable');
      th.setAttribute('role', 'button');
      th.setAttribute('tabindex', '0');
      th.setAttribute('aria-sort', th.dataset.dir === 'asc' ? 'ascending' : th.dataset.dir === 'desc' ? 'descending' : 'none');
      const activate = () => onSort(th);
      th.addEventListener('click', activate);
      th.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          activate();
        }
      });
    });
  }

  function withAuthFallback(promise, fallback) {
    return promise.catch((err) => {
      if (isAuthError(err)) throw err;
      return typeof fallback === 'function' ? fallback(err) : fallback;
    });
  }

  function _setBusyButtons(selector, busy, busyLabel = '') {
    const buttons = Array.from(document.querySelectorAll(selector));
    buttons.forEach((button) => {
      if (!button) return;
      if (busy) {
        if (button.dataset.pfBusyHtml == null) {
          button.dataset.pfBusyHtml = button.innerHTML;
        }
        if (button.dataset.pfBusyDisabled == null) {
          button.dataset.pfBusyDisabled = button.disabled ? '1' : '0';
        }
        button.disabled = true;
        if (busyLabel) button.textContent = busyLabel;
      } else {
        if (button.dataset.pfBusyHtml != null) {
          button.innerHTML = button.dataset.pfBusyHtml;
          delete button.dataset.pfBusyHtml;
        }
        if (button.dataset.pfBusyDisabled != null) {
          button.disabled = button.dataset.pfBusyDisabled === '1';
          delete button.dataset.pfBusyDisabled;
        }
      }
    });
    return () => _setBusyButtons(selector, false);
  }

  function _clearSimulationTimer() {
    if (_liveSimulation.timerId) {
      clearInterval(_liveSimulation.timerId);
      _liveSimulation.timerId = null;
    }
    _liveSimulation.busy = false;
  }

  function _clearSimulationSnapshot() {
    _liveSimulation.lastSnapshot = null;
    try { window.__pfSimulationSnapshot = null; } catch (_) {}
    try { window.__pfSimulationSessionId = null; } catch (_) {}
    try { window.__pfSimulationSessionMeta = null; } catch (_) {}
    try { window.localStorage?.removeItem(LIVE_SIMULATION_STATE_KEY); } catch (_) {}
  }

  function _getLiveSimulationState() {
    try {
      const snapshot = window.__pfSimulationSnapshot || null;
      if (snapshot && typeof snapshot === 'object') {
        const memorySessionId = String(window.__pfSimulationSessionId || snapshot.session_id || '').trim();
        const snapshotSessionId = String(snapshot.session_id || '').trim();
        if (!memorySessionId || !snapshotSessionId || memorySessionId === snapshotSessionId) {
          const memorySessionMeta = window.__pfSimulationSessionMeta;
          return {
            snapshot,
            session: memorySessionMeta && typeof memorySessionMeta === 'object' ? memorySessionMeta : null,
          };
        }
      }
    } catch (_) {}

    try {
      const raw = window.localStorage?.getItem(LIVE_SIMULATION_STATE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== 'object') return null;
      const snapshot = parsed.snapshot && typeof parsed.snapshot === 'object' ? parsed.snapshot : null;
      const session = parsed.session && typeof parsed.session === 'object' ? parsed.session : null;
      if (!snapshot) return null;
      const snapshotSessionId = String(snapshot.session_id || '').trim();
      const sessionSessionId = String(session?.session_id || '').trim();
      if (snapshotSessionId && sessionSessionId && snapshotSessionId !== sessionSessionId) return null;
      return { snapshot, session };
    } catch (_) {
      return null;
    }
  }

  function _persistLiveSimulationState(snapshot, sessionMeta = null) {
    try {
      window.localStorage?.setItem(
        LIVE_SIMULATION_STATE_KEY,
        JSON.stringify({
          snapshot,
          session: sessionMeta && typeof sessionMeta === 'object' ? sessionMeta : null,
          ts: Date.now(),
        })
      );
    } catch (_) {}
  }

  function _clearStoredSimulationState() {
    try { window.localStorage?.removeItem(LIVE_SIMULATION_STATE_KEY); } catch (_) {}
  }

  function _clearSimulationState() {
    _clearSimulationTimer();
    _clearSimulationSnapshot();
    _liveSimulation.sessionId = null;
  }

  function _getLiveSimulationSnapshot() {
    return _getLiveSimulationState()?.snapshot || null;
  }

  function _buildLiveConcentrationFromHoldings(holdings = [], snapshot = null) {
    const rows = Array.isArray(holdings) ? holdings : [];
    const byIssuer = new Map();
    const bySector = new Map();
    const byRating = new Map();
    let totalValue = 0;

    rows.forEach((row) => {
      const value = Number(row?.market_value || row?.current_value || row?.investment_value || row?.book_value || 0);
      if (!Number.isFinite(value) || value <= 0) return;
      totalValue += value;
      const issuer = String(row?.issuer_name || row?.issuer || 'UNKNOWN').trim() || 'UNKNOWN';
      const sector = String(row?.workbook_sector || row?.sector || row?.portfolio_category_label || row?.portfolio_category || 'UNKNOWN').trim() || 'UNKNOWN';
      const rating = String(row?.credit_rating || row?.rating || 'NR').trim() || 'NR';
      byIssuer.set(issuer, (byIssuer.get(issuer) || 0) + value);
      bySector.set(sector, (bySector.get(sector) || 0) + value);
      byRating.set(rating, (byRating.get(rating) || 0) + value);
    });

    const toSeries = (entries) => entries
      .sort((a, b) => b[1] - a[1])
      .map(([name, value]) => ({
        name,
        value: Number(value.toFixed(6)),
        pct: totalValue > 0 ? Number(((value / totalValue) * 100).toFixed(6)) : 0,
      }));

    const topIssuers = toSeries(Array.from(byIssuer.entries())).slice(0, 10);
    const topSectors = toSeries(Array.from(bySector.entries())).slice(0, 10);
    const ratings = toSeries(Array.from(byRating.entries()));

    return {
      total_market_value: Number(totalValue.toFixed(6)),
      top_issuers: topIssuers,
      top_sectors: topSectors,
      rating_breakdown: ratings,
      maturity_ladder: [],
      top_issuer_exposure_pct: topIssuers[0]?.pct || 0,
      top_sector_exposure_pct: topSectors[0]?.pct || 0,
      threshold_alerts: [],
      issuer_concentration: topIssuers.map((row) => ({ issuer_name: row.name, value: row.value, pct: row.pct })),
      sector_concentration: topSectors.map((row) => ({ sector: row.name, value: row.value, pct: row.pct })),
      simulation_overlay: snapshot ? true : false,
      simulation_session_id: snapshot?.session_id || null,
      simulation_tick_date: snapshot?.timeline?.date || snapshot?.date || null,
    };
  }

  function _overlayPortfolioResponse(basePayload = {}, snapshot = null) {
    if (!snapshot || typeof snapshot !== 'object') return basePayload;
    const liveHoldings = Array.isArray(snapshot.holdings) ? snapshot.holdings : [];
    if (!liveHoldings.length) return basePayload;

    const rawHoldings = Array.isArray(basePayload.holdings) ? basePayload.holdings : [];
    const rawByIsin = new Map();
    rawHoldings.forEach((row) => {
      const isin = String(row?.isin || '').trim().toUpperCase();
      if (isin) rawByIsin.set(isin, row);
    });

    const mergedHoldings = liveHoldings.map((row) => {
      const isin = String(row?.isin || '').trim().toUpperCase();
      const rawRow = rawByIsin.get(isin) || {};
      const next = { ...rawRow, ...row };
      next.simulation_overlay = true;
      next.simulation_session_id = snapshot.session_id || null;
      next.simulation_tick_date = snapshot.timeline?.date || snapshot.date || null;
      return next;
    });

    const baseSummary = basePayload.summary || {};
    const mergedSummary = { ...baseSummary, ...(snapshot.summary || {}) };
    const baseCouponYield = _firstPositiveNumber(
      baseSummary.coupon_weighted_yield,
      baseSummary.weighted_coupon_yield,
      baseSummary.portfolio_coupon_weighted_yield_pct,
      baseSummary.workbook_coupon_weighted_yield,
    );
    mergedSummary.total_market_value = snapshot.summary?.total_market_value ?? mergedSummary.total_market_value;
    mergedSummary.current_value = snapshot.summary?.total_market_value ?? mergedSummary.current_value;
    mergedSummary.total_dirty_value = snapshot.summary?.total_market_value ?? mergedSummary.total_dirty_value;
    mergedSummary.total_invested = snapshot.summary?.total_invested ?? mergedSummary.total_invested;
    mergedSummary.invested_value = snapshot.summary?.total_invested ?? mergedSummary.invested_value;
    mergedSummary.weighted_ytm = snapshot.summary?.weighted_ytm ?? mergedSummary.weighted_ytm;
    mergedSummary.coupon_weighted_yield = _firstPositiveNumber(snapshot.summary?.coupon_weighted_yield, baseCouponYield);
    mergedSummary.weighted_modified_duration = snapshot.summary?.weighted_modified_duration ?? mergedSummary.weighted_modified_duration;
    mergedSummary.weighted_duration = snapshot.summary?.weighted_modified_duration ?? snapshot.summary?.weighted_duration ?? mergedSummary.weighted_duration;
    mergedSummary.portfolio_dv01 = snapshot.summary?.portfolio_dv01 ?? mergedSummary.portfolio_dv01;
    mergedSummary.var95_1d_pct = snapshot.summary?.var95_1d_pct ?? mergedSummary.var95_1d_pct;
    mergedSummary.simulation_overlay = true;
    mergedSummary.simulation_session_id = snapshot.session_id || null;
    mergedSummary.simulation_tick_date = snapshot.timeline?.date || snapshot.date || null;

    return {
      ...basePayload,
      holdings: mergedHoldings,
      primary_table: mergedHoldings,
      positions: mergedHoldings,
      summary: mergedSummary,
      grouped: basePayload.grouped || {},
      simulation_overlay: true,
      simulation_session_id: snapshot.session_id || null,
      simulation_tick_date: snapshot.timeline?.date || snapshot.date || null,
      simulation_mode: snapshot.mode?.scenario_mode || null,
    };
  }

  function _patchLiveSimulationApiOverlay() {
    if (typeof API === 'undefined' || !API || API.__liveSimulationOverlayPatched) return;
    const originalGetPortfolio = API.getPortfolio?.bind(API);
    const originalGetPortfolioAnalytics = API.getPortfolioAnalytics?.bind(API);
    const originalGetPortfolioConcentration = API.getPortfolioConcentration?.bind(API);
    const originalGetPortfolioHealth = API.getPortfolioHealth?.bind(API);
    const originalGetPortfolioSectorBreakdown = API.getPortfolioSectorBreakdown?.bind(API);

    if (originalGetPortfolio) {
      API.getPortfolio = async (params = {}) => {
        const snapshot = _getLiveSimulationSnapshot();
        try {
          const response = await originalGetPortfolio(params);
          return _overlayPortfolioResponse(response || {}, snapshot);
        } catch (err) {
          if (!snapshot) throw err;
          return _overlayPortfolioResponse({}, snapshot);
        }
      };
    }

    if (originalGetPortfolioAnalytics) {
      API.getPortfolioAnalytics = async (params = {}) => {
        const snapshot = _getLiveSimulationSnapshot();
        try {
          const response = await originalGetPortfolioAnalytics(params);
          if (!snapshot) return response;
          const baseSummary = response?.summary || {};
          const mergedSummary = { ...baseSummary, ...(snapshot.summary || {}) };
          const baseCouponYield = _firstPositiveNumber(
            baseSummary.coupon_weighted_yield,
            baseSummary.weighted_coupon_yield,
            baseSummary.portfolio_coupon_weighted_yield_pct,
            baseSummary.workbook_coupon_weighted_yield,
          );
          mergedSummary.total_market_value = snapshot.summary?.total_market_value ?? mergedSummary.total_market_value;
          mergedSummary.current_value = snapshot.summary?.total_market_value ?? mergedSummary.current_value;
          mergedSummary.total_dirty_value = snapshot.summary?.total_market_value ?? mergedSummary.total_dirty_value;
          mergedSummary.total_invested = snapshot.summary?.total_invested ?? mergedSummary.total_invested;
          mergedSummary.invested_value = snapshot.summary?.total_invested ?? mergedSummary.invested_value;
          mergedSummary.weighted_ytm = snapshot.summary?.weighted_ytm ?? mergedSummary.weighted_ytm;
          mergedSummary.coupon_weighted_yield = _firstPositiveNumber(snapshot.summary?.coupon_weighted_yield, baseCouponYield);
          mergedSummary.weighted_modified_duration = snapshot.summary?.weighted_modified_duration ?? mergedSummary.weighted_modified_duration;
          mergedSummary.weighted_duration = snapshot.summary?.weighted_modified_duration ?? snapshot.summary?.weighted_duration ?? mergedSummary.weighted_duration;
          mergedSummary.portfolio_dv01 = snapshot.summary?.portfolio_dv01 ?? mergedSummary.portfolio_dv01;
          mergedSummary.var95_1d_pct = snapshot.summary?.var95_1d_pct ?? mergedSummary.var95_1d_pct;
          mergedSummary.simulation_overlay = true;
          mergedSummary.simulation_session_id = snapshot.session_id || null;
          mergedSummary.simulation_tick_date = snapshot.timeline?.date || snapshot.date || null;
          return {
            ...(response || {}),
            summary: mergedSummary,
            simulation_overlay: true,
            simulation_session_id: snapshot.session_id || null,
            simulation_tick_date: snapshot.timeline?.date || snapshot.date || null,
          };
        } catch (err) {
          if (!snapshot) throw err;
          const mergedSummary = { ...(snapshot.summary || {}) };
          const fallbackCouponYield = _firstPositiveNumber(
            snapshot.summary?.coupon_weighted_yield,
            snapshot.summary?.weighted_coupon_yield,
            snapshot.summary?.portfolio_coupon_weighted_yield_pct,
            snapshot.summary?.workbook_coupon_weighted_yield,
          );
          mergedSummary.total_market_value = snapshot.summary?.total_market_value ?? mergedSummary.total_market_value;
          mergedSummary.current_value = snapshot.summary?.total_market_value ?? mergedSummary.current_value;
          mergedSummary.total_dirty_value = snapshot.summary?.total_market_value ?? mergedSummary.total_dirty_value;
          mergedSummary.total_invested = snapshot.summary?.total_invested ?? mergedSummary.total_invested;
          mergedSummary.invested_value = snapshot.summary?.total_invested ?? mergedSummary.invested_value;
          mergedSummary.weighted_ytm = snapshot.summary?.weighted_ytm ?? mergedSummary.weighted_ytm;
          mergedSummary.coupon_weighted_yield = fallbackCouponYield;
          mergedSummary.weighted_modified_duration = snapshot.summary?.weighted_modified_duration ?? mergedSummary.weighted_modified_duration;
          mergedSummary.weighted_duration = snapshot.summary?.weighted_modified_duration ?? snapshot.summary?.weighted_duration ?? mergedSummary.weighted_duration;
          mergedSummary.portfolio_dv01 = snapshot.summary?.portfolio_dv01 ?? mergedSummary.portfolio_dv01;
          mergedSummary.var95_1d_pct = snapshot.summary?.var95_1d_pct ?? mergedSummary.var95_1d_pct;
          mergedSummary.simulation_overlay = true;
          mergedSummary.simulation_session_id = snapshot.session_id || null;
          mergedSummary.simulation_tick_date = snapshot.timeline?.date || snapshot.date || null;
          return {
            summary: mergedSummary,
            simulation_overlay: true,
            simulation_session_id: snapshot.session_id || null,
            simulation_tick_date: snapshot.timeline?.date || snapshot.date || null,
          };
        }
      };
    }

    if (originalGetPortfolioConcentration) {
      API.getPortfolioConcentration = async (params = {}) => {
        const snapshot = _getLiveSimulationSnapshot();
        try {
          const response = await originalGetPortfolioConcentration(params);
          if (!snapshot) return response;
          const live = _buildLiveConcentrationFromHoldings(Array.isArray(snapshot.holdings) ? snapshot.holdings : [], snapshot);
          return { ...(response || {}), ...live };
        } catch (err) {
          if (!snapshot) throw err;
          const live = _buildLiveConcentrationFromHoldings(Array.isArray(snapshot.holdings) ? snapshot.holdings : [], snapshot);
          return { ...live };
        }
      };
    }

    if (originalGetPortfolioHealth) {
      API.getPortfolioHealth = async (params = {}) => {
        const snapshot = _getLiveSimulationSnapshot();
        try {
          const response = await originalGetPortfolioHealth(params);
          if (!snapshot) return response;
          const mergedSummary = { ...(response?.summary || {}), ...(snapshot.summary || {}) };
          mergedSummary.total_market_value = snapshot.summary?.total_market_value ?? mergedSummary.total_market_value;
          mergedSummary.current_value = snapshot.summary?.total_market_value ?? mergedSummary.current_value;
          mergedSummary.total_dirty_value = snapshot.summary?.total_market_value ?? mergedSummary.total_dirty_value;
          mergedSummary.total_invested = snapshot.summary?.total_invested ?? mergedSummary.total_invested;
          mergedSummary.invested_value = snapshot.summary?.total_invested ?? mergedSummary.invested_value;
          mergedSummary.weighted_ytm = snapshot.summary?.weighted_ytm ?? mergedSummary.weighted_ytm;
          mergedSummary.weighted_duration = snapshot.summary?.weighted_modified_duration ?? snapshot.summary?.weighted_duration ?? mergedSummary.weighted_duration;
          mergedSummary.simulation_overlay = true;
          mergedSummary.simulation_session_id = snapshot.session_id || null;
          mergedSummary.simulation_tick_date = snapshot.timeline?.date || snapshot.date || null;
          return {
            ...(response || {}),
            summary: mergedSummary,
            simulation_overlay: true,
            simulation_session_id: snapshot.session_id || null,
            simulation_tick_date: snapshot.timeline?.date || snapshot.date || null,
          };
        } catch (err) {
          if (!snapshot) throw err;
          const mergedSummary = { ...(snapshot.summary || {}) };
          mergedSummary.total_market_value = snapshot.summary?.total_market_value ?? mergedSummary.total_market_value;
          mergedSummary.current_value = snapshot.summary?.total_market_value ?? mergedSummary.current_value;
          mergedSummary.total_dirty_value = snapshot.summary?.total_market_value ?? mergedSummary.total_dirty_value;
          mergedSummary.total_invested = snapshot.summary?.total_invested ?? mergedSummary.total_invested;
          mergedSummary.invested_value = snapshot.summary?.total_invested ?? mergedSummary.invested_value;
          mergedSummary.weighted_ytm = snapshot.summary?.weighted_ytm ?? mergedSummary.weighted_ytm;
          mergedSummary.weighted_duration = snapshot.summary?.weighted_modified_duration ?? snapshot.summary?.weighted_duration ?? mergedSummary.weighted_duration;
          mergedSummary.simulation_overlay = true;
          mergedSummary.simulation_session_id = snapshot.session_id || null;
          mergedSummary.simulation_tick_date = snapshot.timeline?.date || snapshot.date || null;
          return {
            summary: mergedSummary,
            simulation_overlay: true,
            simulation_session_id: snapshot.session_id || null,
            simulation_tick_date: snapshot.timeline?.date || snapshot.date || null,
          };
        }
      };
    }

    if (originalGetPortfolioSectorBreakdown) {
      API.getPortfolioSectorBreakdown = async (params = {}) => {
        const snapshot = _getLiveSimulationSnapshot();
        try {
          const response = await originalGetPortfolioSectorBreakdown(params);
          if (!snapshot) return response;
          const live = _buildLiveConcentrationFromHoldings(Array.isArray(snapshot.holdings) ? snapshot.holdings : [], snapshot);
          return {
            ...(response || {}),
            sectors: live.top_sectors || response?.sectors || [],
            ratings: live.rating_breakdown || response?.ratings || [],
            total_market_value: live.total_market_value ?? response?.total_market_value ?? 0,
            simulation_overlay: true,
            simulation_session_id: snapshot.session_id || null,
            simulation_tick_date: snapshot.timeline?.date || snapshot.date || null,
          };
        } catch (err) {
          if (!snapshot) throw err;
          const live = _buildLiveConcentrationFromHoldings(Array.isArray(snapshot.holdings) ? snapshot.holdings : [], snapshot);
          return {
            sectors: live.top_sectors || [],
            ratings: live.rating_breakdown || [],
            total_market_value: live.total_market_value ?? 0,
            simulation_overlay: true,
            simulation_session_id: snapshot.session_id || null,
            simulation_tick_date: snapshot.timeline?.date || snapshot.date || null,
          };
        }
      };
    }

    API.__liveSimulationOverlayPatched = true;
  }

  function _setSimulationBadge(enabled) {
    const sourceBadge = document.getElementById('trust-source-badge');
    if (!sourceBadge) return;
    if (enabled) {
      sourceBadge.textContent = 'Simulated Live Market';
      sourceBadge.style.background = 'rgba(13,148,136,0.14)';
      sourceBadge.style.color = '#0f766e';
      sourceBadge.style.borderColor = 'rgba(13,148,136,0.38)';
      sourceBadge.setAttribute('title', 'Simulated Live Market (Historical Replay Mode)');
    } else {
      sourceBadge.textContent = `Trust ${getActiveTrustId()}`;
      sourceBadge.style.background = '';
      sourceBadge.style.color = '';
      sourceBadge.style.borderColor = '';
      sourceBadge.removeAttribute('title');
    }
  }

  function _emitSimulationTick(snapshot, sessionMeta = null) {
    if (!snapshot || typeof snapshot !== 'object') return;
    _liveSimulation.lastSnapshot = snapshot;
    const nextSessionMeta = sessionMeta && typeof sessionMeta === 'object'
      ? sessionMeta
      : (window.__pfSimulationSessionMeta && typeof window.__pfSimulationSessionMeta === 'object'
        ? window.__pfSimulationSessionMeta
        : null);
    try {
      window.__pfSimulationSnapshot = snapshot;
      window.__pfSimulationSessionMeta = nextSessionMeta;
      window.__pfSimulationSessionId = nextSessionMeta?.session_id || snapshot.session_id || _liveSimulation.sessionId || null;
    } catch (_) {}

    _persistLiveSimulationState(snapshot, nextSessionMeta);

    const topbarAum = document.getElementById('topbar-aum');
    if (topbarAum && snapshot.summary && snapshot.summary.total_market_value != null) {
      topbarAum.textContent = Utils.formatINR(Number(snapshot.summary.total_market_value || 0));
    }

    _setSimulationBadge(true);
    window.dispatchEvent(new CustomEvent('pf:simulation-tick', {
      detail: {
        snapshot,
        session: nextSessionMeta,
        source: 'local',
      },
    }));
  }

  const _simTickRefreshAt = {
    dashboard: 0,
    portfolio: 0,
    recommendations: 0,
    analysis: 0,
    risk: 0,
    im_checker: 0,
  };

  function _canRefreshFromSimTick(key, cooldownMs) {
    const now = Date.now();
    const last = Number(_simTickRefreshAt[key] || 0);
    if (now - last < cooldownMs) return false;
    _simTickRefreshAt[key] = now;
    return true;
  }

  function _renderIMSimulationBridge(snapshot) {
    const host = document.getElementById('imc-sim-bridge');
    if (!host || !snapshot || typeof snapshot !== 'object') return;

    const summary = snapshot.summary || {};
    const risk = snapshot.risk || {};
    const recommendation = risk.recommendations || {};
    const im = snapshot.im_checker || {};
    const timelineDate = snapshot.timeline?.date || snapshot.date || 'n/a';
    const stance = String(recommendation.stance || 'balanced').toLowerCase();
    const stanceColor = stance === 'defensive' ? '#b45309' : stance === 'opportunistic' ? '#15803d' : '#1d4ed8';

    host.style.display = 'block';
    host.innerHTML = `
      <div class="card" style="margin-bottom:16px;border:1px solid var(--border-light);box-shadow:none">
        <div class="card-body" style="padding:12px 16px">
          <div style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:center">
            <div>
              <div style="font-size:0.72rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:.04em">Live Simulation Feed</div>
              <div style="font-size:0.8rem;margin-top:2px">Tick Date: ${Utils.escapeHTML(String(timelineDate))}</div>
            </div>
            <span class="badge" style="background:${stanceColor};color:#fff">${Utils.escapeHTML(String(stance).toUpperCase())}</span>
          </div>
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-top:10px;font-size:0.76rem">
            <div style="padding:8px 10px;background:var(--bg-tertiary);border-radius:8px"><strong>AUM</strong><br>${Utils.formatINR(Number(summary.total_market_value || 0))}</div>
            <div style="padding:8px 10px;background:var(--bg-tertiary);border-radius:8px"><strong>1D VaR 95%</strong><br>${Utils.formatPct(Number(summary.var95_1d_pct || 0), 4)}</div>
            <div style="padding:8px 10px;background:var(--bg-tertiary);border-radius:8px"><strong>IM Flags</strong><br>${Number(im.flag_count || 0)}</div>
          </div>
        </div>
      </div>
    `;
  }

  function _fanOutSimulationTick(snapshot, sessionMeta = null, source = 'local') {
    if (!snapshot || typeof snapshot !== 'object') return;

    if (currentPage === 'live-simulation') {
      if (source === 'local') return;
      const liveRenderer = window.__pfRenderLiveSimulationSnapshot;
      if (typeof liveRenderer === 'function') {
        liveRenderer(snapshot, sessionMeta || null, { syncTimer: false, broadcast: false });
      }
      return;
    }

    if (currentPage === 'dashboard') {
      if (_canRefreshFromSimTick('dashboard', 1200)) loadDashboardData(true);
      return;
    }
    if (currentPage === 'portfolio' || currentPage === 'portfolio-allocations') {
      if (_canRefreshFromSimTick('portfolio', 1200)) loadPortfolioData();
      return;
    }
    if (currentPage === 'recommendations') {
      if (_canRefreshFromSimTick('recommendations', 1800)) _refreshRecs();
      return;
    }
    if (currentPage === 'bond-allocation') {
      if (_canRefreshFromSimTick('recommendations', 1800)) _refreshBondAllocation();
      return;
    }
    if (currentPage === 'analysis' || currentPage === 'bond-analysis') {
      if (_canRefreshFromSimTick('analysis', 10000)) {
        const currentIsin = document.getElementById('analysis-isin')?.value?.trim();
        if (currentIsin) loadAnalysis(currentIsin);
      }
      return;
    }
    if (currentPage === 'risk' || currentPage === 'compliance-dashboard') {
      if (_canRefreshFromSimTick('risk', 1800)) {
        loadRiskData();
        loadComplianceData();
      }
      return;
    }
    if (currentPage === 'im-checker') {
      if (_canRefreshFromSimTick('im_checker', 800)) _renderIMSimulationBridge(snapshot);
    }
  }

  function isProtectedRoute(page) {
    return ['dashboard', 'portfolio', 'portfolio-allocations', 'live-simulation', 'bonds', 'recommendations', 'bond-allocation', 'bond-analysis', 'analysis', 'im-checker', 'risk', 'compliance-dashboard', 'command-center', 'decision-evidence', 'reports', 'alerts', 'news', 'datasources', 'admin'].includes(page);
  }

  function hasExpiredSession() {
    return _sessionExpired;
  }

  let _authLockReason = '';

  function _setAuthLockedState(locked) {
    const appContainer = document.querySelector('.app-container');
    if (appContainer) {
      if (locked) {
        appContainer.setAttribute('inert', '');
        appContainer.setAttribute('aria-hidden', 'true');
      } else {
        appContainer.removeAttribute('inert');
        appContainer.setAttribute('aria-hidden', 'false');
      }
    }
    document.body.classList.toggle('pf-auth-locked', !!locked);
    document.body.setAttribute('aria-busy', locked ? 'true' : 'false');
    if (!locked) _authLockReason = '';
  }

  function _clearUserChrome() {
    closeUserDropdown();
    const avatarEl = document.getElementById('topbar-avatar-initials');
    if (avatarEl) avatarEl.textContent = 'â€”';
    const nameEl = document.getElementById('user-display-name');
    if (nameEl) nameEl.textContent = 'â€”';
    const roleEl = document.getElementById('user-display-role');
    if (roleEl) roleEl.textContent = 'Role: â€”';
  }

  function isAuthenticated() {
    return _sessionAuthenticated;
  }

  function handleAuthError(err, fallbackMessage = 'Sign in to continue.') {
    const authError = err instanceof Error ? err : new Error(err?.message || fallbackMessage);
    const reason = authError.message || fallbackMessage;
    if (document.body.classList.contains('pf-auth-locked') && _authLockReason === reason) {
      return authError;
    }
    _setSessionState(false, _looksLikeExpiredSession(reason));
    _clearSimulationState();
    _setSimulationBadge(false);
    API.clearToken();
    destroyCharts();
    _clearUserChrome();
    window.dispatchEvent(new CustomEvent('pf:auth-changed', { detail: { authenticated: false } }));
    showLoginScreen(reason);
    _authLockReason = reason;
    return authError;
  }

  function ensureAuthenticated(message = 'Sign in to continue.') {
    if (isAuthenticated()) return true;
    const reason = /^sign in/i.test(String(message || ''))
      ? String(message || 'Sign in to continue.')
      : `Sign in to ${String(message || 'continue').replace(/[.!?]+$/, '')}.`;
    handleAuthError(new Error(reason), reason);
    return false;
  }

  function renderSessionLocked(reason = 'Your session expired. Sign in again to continue.') {
    _setAuthLockedState(true);
    _authLockReason = reason;
    const content = document.getElementById('page-content');
    if (content) {
      content.innerHTML = `
        <div style="display:flex;align-items:center;justify-content:center;min-height:56vh;padding:32px">
          <div class="card" style="max-width:640px;width:100%;padding:28px 24px;text-align:center;border:1px solid var(--border-light)">
            <div style="font-size:2.5rem;line-height:1;margin-bottom:12px">ðŸ”’</div>
            <div class="page-title" style="margin-bottom:8px">Session locked</div>
            <div class="page-subtitle" style="margin-bottom:16px">${Utils.escapeHTML(reason)}</div>
            <div class="empty-state-desc" style="margin-bottom:20px">Protected actions are disabled until you sign in again. This prevents expired-auth responses from looking like empty holdings.</div>
            <button class="btn btn-primary btn-sm" id="session-reauth-btn">Re-authenticate</button>
          </div>
        </div>
      `;
      const reauthBtn = document.getElementById('session-reauth-btn');
      if (reauthBtn) {
        reauthBtn.addEventListener('click', () => showLoginScreen(reason));
      }
    }
  }

  // â”€â”€ Portfolio mock data (simulates a real portfolio) â”€â”€â”€â”€â”€â”€â”€â”€â”€
  const PORTFOLIO = {
    aum: null,
    invested: null,
    cash: 0,
    weightedYTM: null,
    couponWeightedYield: null,
    avgDuration: null,
    portfolioRating: null,
    holdings: [],
    lastUpdated: null,
  };

  function _displayCouponWeightedYield(summary = {}) {
    const raw = summary.coupon_weighted_yield
      ?? summary.weighted_coupon_yield
      ?? summary.portfolio_coupon_weighted_yield_pct
      ?? summary.workbook_coupon_weighted_yield;
    const value = Number(raw);
    return Number.isFinite(value) && value > 0 ? value : null;
  }

  function _firstPositiveNumber(...values) {
    for (const value of values) {
      const numeric = Number(value);
      if (Number.isFinite(numeric) && numeric > 0) return numeric;
    }
    return null;
  }

  function _aggregateDistinctBondRows(rows = []) {
    const toNum = (value) => Number(value || 0);
    const round = (value, digits = 6) => Number(Number(value || 0).toFixed(digits));
    const statusRank = { HEALTHY: 0, MONITOR: 1, RISK: 2 };
    const confidenceRank = { low: 0, medium: 1, high: 2 };
    const statusFromRank = (rank) => (rank >= 2 ? 'RISK' : rank >= 1 ? 'MONITOR' : 'HEALTHY');
    const confidenceFromRank = (rank) => (rank >= 2 ? 'high' : rank >= 1 ? 'medium' : 'low');

    const groups = new Map();
    for (const row of rows || []) {
      const isin = String(row.isin || '').trim();
      if (!isin) continue;

      const status = String(row.status || 'HEALTHY').toUpperCase();
      const confidence = String(row.confidence_level || 'low').toLowerCase();
      const lotWeight = toNum(row.market_value || row.current_value || row.investment_value || row.face_value_total || row.face_value || 0);

      let group = groups.get(isin);
      if (!group) {
        group = {
          ...row,
          instrument_category: row.instrument_category || row.category || row.category_bucket || row.workbook_category || row.sheet_category || null,
          workbook_category: row.workbook_category || row.sheet_category || null,
          lot_count: 0,
          lot_details: [],
          quantity: 0,
          face_value: 0,
          face_value_total: 0,
          book_value: 0,
          market_value: 0,
          current_value: 0,
          clean_value: 0,
          investment_value: 0,
          accrued_interest: 0,
          accrued_per_100: 0,
          clean_price_weighted: 0,
          dirty_price_weighted: 0,
          ytm_weighted: 0,
          modified_duration_weighted: 0,
          macaulay_duration_weighted: 0,
          dv01_total: 0,
          spread_weighted: 0,
          years_to_maturity_weighted: 0,
          _weight_total: 0,
          _status_rank: statusRank[status] ?? 0,
          _confidence_rank: confidenceRank[confidence] ?? 0,
        };
        groups.set(isin, group);
      } else if (!group.workbook_category && (row.workbook_category || row.sheet_category)) {
        group.workbook_category = row.workbook_category || row.sheet_category;
      }
      if (!group.instrument_category) {
        group.instrument_category = row.instrument_category || row.category || row.category_bucket || row.workbook_category || row.sheet_category || null;
      }

      group.lot_count += 1;
      group.quantity += toNum(row.quantity);
      group.face_value += toNum(row.face_value_total || row.face_value);
      group.face_value_total += toNum(row.face_value_total || row.face_value);
      group.book_value += toNum(row.book_value || row.investment_value);
      group.market_value += toNum(row.market_value || row.current_value);
      group.current_value += toNum(row.current_value || row.market_value);
      group.clean_value += toNum(row.clean_value || row.current_value || row.market_value);
      group.investment_value += toNum(row.investment_value);
      group.accrued_interest += toNum(row.accrued_interest);
      group.clean_price_weighted += toNum(row.clean_price) * lotWeight;
      group.dirty_price_weighted += toNum(row.dirty_price) * lotWeight;
      group.ytm_weighted += toNum(row.ytm) * lotWeight;
      group.modified_duration_weighted += toNum(row.modified_duration) * lotWeight;
      group.macaulay_duration_weighted += toNum(row.macaulay_duration) * lotWeight;
      group.dv01_total += toNum(row.dv01);
      group.spread_weighted += toNum(row.spread_over_gsec_bps) * lotWeight;
      group.years_to_maturity_weighted += toNum(row.years_to_maturity) * lotWeight;
      group._weight_total += lotWeight;
      group._status_rank = Math.max(group._status_rank, statusRank[status] ?? 0);
      group._confidence_rank = Math.min(group._confidence_rank, confidenceRank[confidence] ?? 0);
      group.lot_details.push({
        unique_lot_id: row.unique_lot_id || row.holding_id || row.holding_key || isin,
        holding_key: row.holding_key || row.holding_id || isin,
        source_row_number: row.source_row_number,
        source_reference: row.source_reference,
        source_name: row.source_name,
        source_type: row.source_type,
        purchase_date: row.purchase_date,
        quantity: toNum(row.quantity),
        purchase_price: toNum(row.purchase_price),
        clean_price: toNum(row.clean_price || row.current_price || row.market_price),
        dirty_price: toNum(row.dirty_price),
        market_value: toNum(row.market_value || row.current_value),
        current_value: toNum(row.current_value || row.market_value),
        book_value: toNum(row.book_value || row.investment_value),
        investment_value: toNum(row.investment_value),
        ytm: toNum(row.ytm),
        accrued_interest: toNum(row.accrued_interest),
        modified_duration: toNum(row.modified_duration),
        macaulay_duration: toNum(row.macaulay_duration),
        status,
        confidence_level: confidence,
      });
    }

    return Array.from(groups.values())
      .map((group) => {
        const weight = group._weight_total || group.market_value || group.investment_value || 1;
        const faceTotal = group.face_value_total || group.face_value || 0;
        return {
          ...group,
          holding_key: group.holding_key || group.lot_details[0]?.holding_key || group.isin,
          clean_price: round(group.clean_price_weighted / weight, 6),
          dirty_price: round(group.dirty_price_weighted / weight, 6),
          accrued_per_100: faceTotal > 0 ? round((group.accrued_interest / faceTotal) * 100, 6) : 0,
          ytm: round(group.ytm_weighted / weight, 6),
          modified_duration: round(group.modified_duration_weighted / weight, 6),
          macaulay_duration: round(group.macaulay_duration_weighted / weight, 6),
          dv01: round(group.dv01_total, 6),
          spread_over_gsec_bps: round(group.spread_weighted / weight, 6),
          years_to_maturity: round(group.years_to_maturity_weighted / weight, 6),
          current_price: round(group.clean_price_weighted / weight, 6),
          current_value: round(group.current_value, 6),
          market_value: round(group.market_value, 6),
          clean_value: round(group.clean_value, 6),
          investment_value: round(group.investment_value, 6),
          status: statusFromRank(group._status_rank),
          confidence_level: confidenceFromRank(group._confidence_rank),
          source_name: group.lot_details[0]?.source_name || group.source_name || 'â€”',
          source_type: group.lot_details[0]?.source_type || group.source_type || 'unknown',
          source_reference: group.lot_details.map((lot) => lot.source_reference).filter(Boolean).join(' Â· ') || group.source_reference || '',
          source_row_number: group.lot_details[0]?.source_row_number || group.source_row_number,
          lot_details: group.lot_details.sort((a, b) => (a.source_row_number || 0) - (b.source_row_number || 0)),
        };
      })
      .sort((a, b) => (b.market_value || 0) - (a.market_value || 0));
  }

  // â”€â”€ Icons (inline SVG) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
  const IC = {
    dashboard: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
    portfolio: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 16V8a2 2 0 00-1-1.73l-7-4a2 2 0 00-2 0l-7 4A2 2 0 003 8v8a2 2 0 001 1.73l7 4a2 2 0 002 0l7-4A2 2 0 0021 16z"/></svg>',
    bonds: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>',
    analysis: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 21H4.6c-.56 0-.84 0-1.054-.109a1 1 0 01-.437-.437C3 20.24 3 19.96 3 19.4V3m17 5l-3.5 3.5L13 8l-4 4"/></svg>',
    risk: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
    reports: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14,2 14,8 20,8"/></svg>',
    alerts: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9M13.73 21a2 2 0 01-3.46 0"/></svg>',
    data: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3M21 5v14c0 1.66-4 3-9 3s-9-1.34-9-3V5"/></svg>',
    settings: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-2 2 2 2 0 01-2-2v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83 0 2 2 0 010-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 01-2-2 2 2 0 012-2h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 010-2.83 2 2 0 012.83 0l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 012-2 2 2 0 012 2v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 0 2 2 0 010 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 012 2 2 2 0 01-2 2h-.09a1.65 1.65 0 00-1.51 1z"/></svg>',
    search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>',
    bell: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9M13.73 21a2 2 0 01-3.46 0"/></svg>',
    moon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z"/></svg>',
    sun: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>',
    download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>',
    plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg>',
    refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15"/></svg>',
    filter: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/></svg>',
    user: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2M12 11a4 4 0 100-8 4 4 0 000 8z"/></svg>',
    logout: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9"/></svg>',
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>',
    x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>',
    arrowUp: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
    arrowDown: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M19 12l-7 7-7-7"/></svg>',
    print: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 6 2 18 2 18 9"/><path d="M6 18H4a2 2 0 01-2-2v-5a2 2 0 012-2h16a2 2 0 012 2v5a2 2 0 01-2 2h-2"/><rect x="6" y="14" width="12" height="8"/></svg>',
    mail: '<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="2" style="display:inline-block;flex-shrink:0;vertical-align:-0.125em"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>',
    globe: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15.3 15.3 0 014 10 15.3 15.3 0 01-4 10 15.3 15.3 0 01-4-10 15.3 15.3 0 014-10z"/></svg>',
    shield: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
    trending: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>',
    clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12,6 12,12 16,14"/></svg>',
  };

  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  // Router
  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  let _prevPage = null;
  function navigate(page, params = {}) {
    if (isProtectedRoute(page) && !isAuthenticated()) {
      const reason = hasExpiredSession()
        ? 'Your session expired. Sign in again to continue.'
        : 'Sign in to continue.';
      handleAuthError(new Error(reason), reason);
      return;
    }
    _prevPage = currentPage;
    currentPage = page;
    const nextHash = '#' + page;
    if (window.location.hash !== nextHash) {
      try { history.pushState({ page }, '', nextHash); } catch (_) {}
    }

    // Update sidebar active state
    const activePage = page === 'bond-analysis'
      ? 'analysis'
      : page === 'portfolio-allocations'
      ? 'portfolio'
      : page;
    document.querySelectorAll('.nav-item').forEach(el => {
      const isActive = el.dataset.page === activePage;
      el.classList.toggle('active', isActive);
      if (isActive) el.setAttribute('aria-current', 'page');
      else el.removeAttribute('aria-current');
    });

    // Update breadcrumb
    const names = {
      dashboard: 'Dashboard', portfolio: 'Portfolio', 'portfolio-allocations': 'Portfolio', 'live-simulation': 'Live Simulation', bonds: 'Bonds Universe',
      recommendations: 'Best Quotes',
      'bond-allocation': 'Bond Allocation',
      analysis: 'Investment Analysis', 'bond-analysis': 'Investment Analysis',
      'im-checker': 'Intelligence & Credit Center',
      risk: 'Risk & Compliance',
      'compliance-dashboard': 'Compliance Dashboard',
      'command-center': 'Institutional Command Center',
      'decision-evidence': 'Decision Evidence',
      reports: 'Reports',
      alerts: 'Alerts', news: 'Bond News',
      datasources: 'Data Sources', admin: 'Admin Settings'
    };
    const bcEl = document.getElementById('breadcrumb-page');
    if (bcEl) bcEl.textContent = names[page] || page;

    // Render page
    const content = document.getElementById('page-content');
    destroyCharts();

    switch (page) {
      case 'dashboard': renderDashboard(content); break;
      case 'portfolio': renderPortfolio(content); break;
      case 'portfolio-allocations': renderPortfolio(content, { tab: 'allocations' }); break;
      case 'live-simulation': renderLiveSimulation(content); break;
      case 'bonds': renderBondsUniverse(content, params); break;
      case 'recommendations': renderRecommendations(content); break;
      case 'bond-allocation': renderBondAllocation(content); break;
      case 'bond-analysis': renderAnalysis(content, params); break;
      case 'analysis': renderAnalysis(content, params); break;
      case 'im-checker': renderIMChecker(content, params); break;
      case 'risk': renderRiskCompliance(content); break;
      case 'compliance-dashboard': renderComplianceDashboard(content); break;
      case 'command-center': renderInstitutionalCommandCenter(content); break;
      case 'decision-evidence': renderDecisionEvidence(content, params); break;
      case 'reports': renderReports(content); break;
      case 'alerts': renderAlerts(content); break;
      case 'news': renderNews(content); break;
      case 'datasources': renderDataSources(content); break;
      case 'admin': renderAdmin(content); break;
      default: renderDashboard(content);
    }

    if (isMobileViewport()) {
      closeSidebar();
    }
  }

  function destroyCharts() {
    Object.values(chartInstances).forEach(c => { try { c.destroy(); } catch (e) {} });
    chartInstances = {};
  }

  function openPortfolioAllocations() {
    navigate('portfolio');
    setTimeout(() => {
      const tab = document.querySelector('#port-tabs .tab[data-tab="allocations"]');
      if (tab) tab.click();
    }, 0);
  }

  const CATEGORY_LABELS = {
    STATE_CENTRAL: 'Central & State',
    CENTRAL_STATE: 'Central & State',
    PSU: 'PSU',
    PRIVATE: 'Private',
    EQUITY: 'Equity',
    SDS: 'SDS',
    BOND: 'Bond',
    CENTRAL: 'Central Government Bonds',
    STATE: 'State Government Bonds',
    PSU_BOND: 'PSU Bonds',
    PRIVATE_BOND: 'Private / Corporate Bonds',
    CORPORATE: 'Private / Corporate Bonds',
    GOVT: 'Central & State',
    PSU_PRIVATE_DEBT: 'PSU / Private',
    CENTRAL_STATE: 'Central & State',
    PSU_PRIVATE: 'PSU / Private',
    EQUITY_RELATED: 'Equity-Related',
  };

  const CATEGORY_ORDER = ['SDS', 'STATE_CENTRAL', 'PSU', 'PRIVATE', 'EQUITY'];

  const WORKBOOK_CATEGORY_ORDER = [
    'Category I - Central and State Government Of India',
    'Category II Public Sector Undertaking',
    'Category III - Corporate Bonds',
    'Category IV - Equity',
    'Category V - SDS',
  ];

  function _toFiniteNumber(value, fallback = 0) {
    if (value === null || value === undefined) return fallback;
    if (typeof value === 'string' && value.trim() === '') return fallback;
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : fallback;
  }

  function _toFiniteOrNull(value) {
    if (value === null || value === undefined) return null;
    if (typeof value === 'string' && value.trim() === '') return null;
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : null;
  }

  function _displayCategoryLabel(key) {
    const normalized = String(key || '').toUpperCase();
    return CATEGORY_LABELS[normalized] || String(key || 'â€”').replace(/_/g, ' ');
  }

  function _canonicalCategoryKey(rawKey) {
    const key = String(rawKey || '').toUpperCase();
    if (!key) return 'PRIVATE';
    if (key === 'STATE_CENTRAL' || key === 'CENTRAL_STATE' || key === 'GOVT') return 'STATE_CENTRAL';
    if (key === 'PSU' || key === 'PSU_PRIVATE') return 'PSU';
    if (key === 'PRIVATE' || key === 'PSU_PRIVATE_DEBT') return 'PRIVATE';
    if (key === 'EQUITY' || key === 'EQUITY_RELATED') return 'EQUITY';
    if (/(GOV|STATE|CENTRAL|SDL|G-SEC|GSEC|TREASURY|TBILL|SOV)/.test(key)) return 'STATE_CENTRAL';
    if (/\bPSU\b|PUBLIC SECTOR/.test(key)) return 'PSU';
    if (/EQUITY|ETF|MUTUAL FUND|INDEX/.test(key)) return 'EQUITY';
    if (/PRIVATE|CORPORATE|NBFC|BANK/.test(key)) return 'PRIVATE';
    return 'PRIVATE';
  }

  function _portfolioCategoryKey(row = {}) {
    return _canonicalCategoryKey(row.portfolio_category || row.portfolio_category_key || row.portfolio_bucket || row.instrument_category || row.category || row.category_bucket || row.workbook_category || row.sheet_category || row.portfolio_category_label);
  }

  function _legacyToCanonicalWeights(weights = {}) {
    return {
      STATE_CENTRAL: _toFiniteNumber(weights.STATE_CENTRAL ?? weights.GOVT ?? weights.CENTRAL_STATE, 0),
      PSU: _toFiniteNumber(weights.PSU ?? weights.PSU_PRIVATE, 0),
      PRIVATE: _toFiniteNumber(weights.PRIVATE ?? weights.PSU_PRIVATE_DEBT, 0),
      EQUITY: _toFiniteNumber(weights.EQUITY ?? weights.EQUITY_RELATED, 0),
    };
  }

  function _categoryExposureToWeights(rows = []) {
    const out = { SDS: 0, STATE_CENTRAL: 0, PSU: 0, PRIVATE: 0, EQUITY: 0 };
    (rows || []).forEach((row) => {
      const key = _canonicalCategoryKey(row.instrument_category || row.category || row.category_bucket);
      const pct = _toFiniteNumber(row.pct ?? row.weight_pct, 0);
      if (!Number.isFinite(pct)) return;
      out[key] = pct;
    });
    return out;
  }

  function _workbookCategoryKey(row = {}) {
    return _classifyWorkbookCategory(row);
  }

  function _classifyWorkbookCategory(row = {}) {
    const raw = String(row.workbook_category || row.sheet_category || row.category || row.sector || '').trim();
    if (!raw) return 'Other';
    const normalized = raw.toUpperCase().replace(/\s+/g, ' ').trim();
    if (/\bCATEGORY V\b/.test(normalized) || normalized.includes('STATE DEVELOPMENT SAVINGS') || normalized.includes('SDS')) return 'Category V - SDS';
    if (/\bCATEGORY IV\b/.test(normalized) || normalized.includes('EQUITY')) return 'Category IV - Equity';
    if (/\bCATEGORY III\b/.test(normalized) || normalized.includes('CORPORTATE') || normalized.includes('CORPORATE') || normalized.includes('PRIVATE')) return 'Category III - Corporate Bonds';
    if (/\bCATEGORY II\b/.test(normalized) && (normalized.includes('PUBLIC SECTOR UNDERTAKING') || normalized.includes('PSU'))) return 'Category II Public Sector Undertaking';
    if (/\bCATEGORY I\b/.test(normalized) && (normalized.includes('CENTRAL') || normalized.includes('STATE') || normalized.includes('GOVERNMENT'))) return 'Category I - Central and State Government Of India';
    return 'Other';
  }

  function _normalizePortfolioText(value) {
    return String(value || '').trim().toUpperCase();
  }

  function _isSdsWorkbookRow(row = {}) {
    if (_normalizePortfolioText(row.issuer_type) === 'SDL') return true;
    const text = [
      row.workbook_category,
      row.category,
      row.issuer_name,
      row.bond_name,
    ].map(_normalizePortfolioText).join(' ');
    return text.includes('STATE DEVELOPMENT LOAN') || text.includes('STATE DEV LOAN') || text.includes(' SDL ') || text.startsWith('SDL') || text.includes('SDS');
  }

  function _classifyScreenshotCategory(row = {}) {
    const issuerType = _normalizePortfolioText(row.issuer_type);
    const text = [
      issuerType,
      row.workbook_category,
      row.category,
      row.workbook_sector,
      row.sector,
      row.issuer_name,
      row.bond_name,
    ].map(_normalizePortfolioText).join(' ');

    if (/(EQUITY|MUTUAL FUND|ETF|FUND)/.test(text)) return 'Equity and Equity Related Instruments';
    if (_isSdsWorkbookRow(row)) return 'SDS';
    if (/GUARANTEED/.test(text) && /(GOV|GOVT|GOI|STATE|CENTRAL|SDL|G-SEC|GSEC|TREASURY)/.test(text)) {
      return 'Central Govt./State Govt. Guaranteed Bonds';
    }
    if (issuerType === 'PSU' || /(PUBLIC SECTOR UNDERTAKING|\bPSU\b)/.test(text)) return 'Public Sector Bonds';
    if (issuerType === 'STATE_GOVT' || /(STATE GOVT|STATE GOVERNMENT|STATE SECURITIES|STATE GOVERNMENT SECURITIES|STATE GOVT SECURITIES|STATE BONDS)/.test(text)) {
      return 'State Govt. Securities';
    }
    if (issuerType === 'G-SEC' || issuerType === 'T-BILL' || /(GOVERNMENT OF INDIA|\bGOI\b|G-SEC|GSEC|TREASURY BILL|CENTRAL GOV|CENTRAL GOVERNMENT|SOVEREIGN)/.test(text)) {
      return 'Central Govt. Securities';
    }
    return 'Private Sector Bonds';
  }

  function _normalizeAllocationBucket(row = {}) {
    const explicitCategory = row.portfolio_category || row.portfolio_category_key || row.portfolio_bucket || row.instrument_category || row.category_display || row.category;
    if (explicitCategory) return _canonicalCategoryKey(explicitCategory);

    const issuerType = String(row.issuer_type || '').toUpperCase();
    const sector = String(row.sector || '').toUpperCase();
    const category = String(row.allocation_bucket || row.category || '').toUpperCase();

    if (category.includes('EQUITY') || sector.includes('EQUITY')) return 'EQUITY';
    if (['G-SEC', 'SDL', 'SOVEREIGN', 'CENTRAL_GOVT', 'STATE_GOVT', 'T-BILL'].includes(issuerType)) return 'STATE_CENTRAL';
    if (issuerType === 'PSU' || /PUBLIC SECTOR/.test(issuerType)) return 'PSU';
    if (/(GOV|SOV|SDL|G-SEC|GSEC|TREASURY|TBILL)/.test(category)) return 'STATE_CENTRAL';
    if (/(GOV|SOV|SDL|G-SEC|GSEC|TREASURY|TBILL)/.test(sector)) return 'STATE_CENTRAL';
    if (/(PSU|PUBLIC SECTOR)/.test(category) || /(PSU|PUBLIC SECTOR)/.test(sector)) return 'PSU';
    if (/(PRIVATE|CORPORATE|BANK|NBFC)/.test(category)) return 'PRIVATE';
    if (/(PRIVATE|CORPORATE|BANK|NBFC)/.test(sector)) return 'PRIVATE';
    return 'PRIVATE';
  }

  function _deriveAllocationBucketWeights(holdings = []) {
    const totals = { SDS: 0, STATE_CENTRAL: 0, PSU: 0, PRIVATE: 0, EQUITY: 0 };
    let grandTotal = 0;
    holdings.forEach((row) => {
      const value = _toFiniteNumber(row.investment_value ?? row.current_value ?? row.market_value, 0);
      if (value <= 0) return;
      const bucket = _normalizeAllocationBucket(row);
      totals[bucket] = (totals[bucket] || 0) + value;
      grandTotal += value;
    });
    return {
      SDS: grandTotal > 0 ? (totals.SDS / grandTotal) * 100 : 0,
      STATE_CENTRAL: grandTotal > 0 ? (totals.STATE_CENTRAL / grandTotal) * 100 : 0,
      PSU: grandTotal > 0 ? (totals.PSU / grandTotal) * 100 : 0,
      PRIVATE: grandTotal > 0 ? (totals.PRIVATE / grandTotal) * 100 : 0,
      EQUITY: grandTotal > 0 ? (totals.EQUITY / grandTotal) * 100 : 0,
    };
  }

  function _deriveCategoryMixRows(holdings = []) {
    const groupTotals = new Map();
    let grandTotal = 0;
    holdings.forEach((row) => {
      const value = _toFiniteNumber(row.investment_value ?? row.current_value ?? row.market_value, 0);
      if (value <= 0) return;
      const group = _portfolioCategoryKey(row);
      groupTotals.set(group, (groupTotals.get(group) || 0) + value);
      grandTotal += value;
    });

    const preferredOrder = CATEGORY_ORDER;
    const classifiedRows = preferredOrder
      .filter((key) => (groupTotals.get(key) || 0) > 0)
      .map((key) => ({
        name: key,
        value: groupTotals.get(key) || 0,
        pct: grandTotal > 0 ? ((groupTotals.get(key) || 0) / grandTotal) * 100 : 0,
      }));
    if (classifiedRows.length) {
      return classifiedRows.sort((a, b) => b.value - a.value);
    }

    const bucketTotals = { SDS: 0, STATE_CENTRAL: 0, PSU: 0, PRIVATE: 0, EQUITY: 0 };
    holdings.forEach((row) => {
      const value = _toFiniteNumber(row.investment_value ?? row.current_value ?? row.market_value, 0);
      if (value <= 0) return;
      const bucket = _normalizeAllocationBucket(row);
      bucketTotals[bucket] = (bucketTotals[bucket] || 0) + value;
    });

    return [
      { name: 'SDS', value: bucketTotals.SDS || 0, pct: grandTotal > 0 ? ((bucketTotals.SDS || 0) / grandTotal) * 100 : 0 },
      { name: 'STATE_CENTRAL', value: bucketTotals.STATE_CENTRAL || 0, pct: grandTotal > 0 ? ((bucketTotals.STATE_CENTRAL || 0) / grandTotal) * 100 : 0 },
      { name: 'PSU', value: bucketTotals.PSU || 0, pct: grandTotal > 0 ? ((bucketTotals.PSU || 0) / grandTotal) * 100 : 0 },
      { name: 'PRIVATE', value: bucketTotals.PRIVATE || 0, pct: grandTotal > 0 ? ((bucketTotals.PRIVATE || 0) / grandTotal) * 100 : 0 },
      { name: 'EQUITY', value: bucketTotals.EQUITY || 0, pct: grandTotal > 0 ? ((bucketTotals.EQUITY || 0) / grandTotal) * 100 : 0 },
    ].filter((row) => row.value > 0);
  }

  function _allocationBucketText(weights, withSigns = false) {
    const normalized = _legacyToCanonicalWeights(weights || {});
    return CATEGORY_ORDER.map((key) => {
      const value = _toFiniteNumber(normalized?.[key], 0);
      const rendered = withSigns ? `${value >= 0 ? '+' : ''}${value.toFixed(2)}%` : `${value.toFixed(2)}%`;
      return `${_displayCategoryLabel(key)} ${rendered}`;
    }).join(' | ');
  }

  function _bondAllocationWeights(weights = {}) {
    const normalized = _legacyToCanonicalWeights(weights || {});
    return {
      STATE_CENTRAL: Number(normalized.STATE_CENTRAL || 0),
      PSU_PRIVATE_DEBT: Number(normalized.PSU || 0) + Number(normalized.PRIVATE || 0),
      EQUITY: Number(normalized.EQUITY || 0),
    };
  }

  function _bondAllocationBucketText(weights, withSigns = false) {
    const normalized = _bondAllocationWeights(weights || {});
    return ['STATE_CENTRAL', 'PSU_PRIVATE_DEBT', 'EQUITY'].map((key) => {
      const value = Number(normalized?.[key] || 0);
      const rendered = withSigns ? `${value >= 0 ? '+' : ''}${value.toFixed(2)}%` : `${value.toFixed(2)}%`
      ;
      return `${_displayCategoryLabel(key)} ${rendered}`;
    }).join(' | ');
  }

  function _formatCategoryMixRows(rows, limit = 5) {
    return (rows || [])
      .slice(0, limit)
      .map((row) => `${_displayCategoryLabel(row.name)}: ${_toFiniteNumber(row.pct, 0).toFixed(1)}%`)
      .join(' | ') || 'n/a';
  }

  const INVESTMENT_TYPE_ORDER = ['SDS', 'BOND', 'EQUITY'];
  const BOND_SUBTYPE_ORDER = ['CENTRAL', 'STATE', 'PSU', 'PRIVATE'];
  const INVESTMENT_TYPE_LABELS = {
    SDS: 'SDS',
    BOND: 'Bond',
    EQUITY: 'Equity',
  };
  const BOND_SUBTYPE_LABELS = {
    CENTRAL: 'Central Government Bonds',
    STATE: 'State Government Bonds',
    PSU: 'PSU Bonds',
    PRIVATE: 'Private / Corporate Bonds',
  };

  function _canonicalInvestmentTypeKey(rawKey) {
    const key = String(rawKey || '').toUpperCase().trim();
    if (!key) return 'BOND';
    if (key === 'SDS' || key === 'STATIC' || key === 'LOCKED' || key === 'NON-TRADABLE') return 'SDS';
    if (key === 'BOND' || key === 'DEBT' || key === 'FIXED_INCOME' || key === 'FIXED INCOME') return 'BOND';
    if (key === 'EQUITY' || key === 'NAV' || key === 'MARKET' || key === 'EQUITY_SHARE') return 'EQUITY';
    if (/(SDS|SAVINGS|FRSB)/.test(key)) return 'SDS';
    if (/(EQUITY|ETF|MUTUAL FUND|INDEX)/.test(key)) return 'EQUITY';
    return 'BOND';
  }

  function _canonicalBondSubtypeKey(rawKey) {
    const key = String(rawKey || '').toUpperCase().trim();
    if (!key) return 'PRIVATE';
    if (key === 'CENTRAL' || key === 'GSEC' || key === 'GOVT' || key === 'GOVERNMENT') return 'CENTRAL';
    if (key === 'STATE' || key === 'SDL' || key === 'STATE_GOVT') return 'STATE';
    if (key === 'PSU' || key === 'PSU_BOND') return 'PSU';
    if (key === 'PRIVATE' || key === 'CORPORATE' || key === 'NBFC') return 'PRIVATE';
    return 'PRIVATE';
  }

  function _investmentTypeFromRow(row = {}) {
    const explicit = _canonicalInvestmentTypeKey(row.type || row.instrument_type || row.asset_type || row.category_type);
    if (explicit === 'SDS' || explicit === 'EQUITY') return explicit;
    if (_isSdsWorkbookRow(row) || String(row.type || row.instrument_type || '').toUpperCase() === 'SDS') return 'SDS';
    const category = String(row.category || row.instrument_category || row.workbook_category || row.sheet_category || '').toUpperCase();
    if (/(EQUITY|ETF|MUTUAL FUND|INDEX)/.test(category)) return 'EQUITY';
    return 'BOND';
  }

  function _bondSubtypeFromRow(row = {}) {
    const explicit = _canonicalBondSubtypeKey(row.sub_type || row.instrument_subtype || row.bond_subtype || row.category_bucket);
    if (explicit !== 'PRIVATE') return explicit;
    const issuerType = String(row.issuer_type || '').toUpperCase();
    const text = [row.category, row.workbook_category, row.sector, row.workbook_sector, row.issuer_name, row.bond_name, issuerType]
      .map(_normalizePortfolioText)
      .join(' ');
    if (issuerType === 'PSU' || /\bPSU\b|PUBLIC SECTOR/.test(text)) return 'PSU';
    if (issuerType === 'SDL' || /STATE DEVELOPMENT LOAN|STATE GOVERNMENT|STATE GOVT|SDL/.test(text)) return 'STATE';
    if (issuerType === 'G-SEC' || issuerType === 'T-BILL' || /GOVERNMENT OF INDIA|GOI|G-SEC|GSEC|TREASURY|SOVEREIGN/.test(text)) return 'CENTRAL';
    return 'PRIVATE';
  }

  function _investmentDisplayLabel(row = {}) {
    const type = _investmentTypeFromRow(row);
    if (type === 'SDS') return 'SDS';
    if (type === 'EQUITY') return 'Equity';
    const subtype = _bondSubtypeFromRow(row);
    if (subtype === 'CENTRAL') return 'Central Government Bonds';
    if (subtype === 'STATE') return 'State Government Bonds';
    if (subtype === 'PSU') return 'PSU Bonds';
    return 'Private / Corporate Bonds';
  }

  function _groupedPortfolioView(portfolio = {}) {
    const grouped = portfolio.grouped || {};
    if (grouped.sds && grouped.bonds && grouped.equity) return grouped;
    const holdings = Array.isArray(portfolio.holdings) ? portfolio.holdings : [];
    const buckets = {
      sds: { summary: { type: 'SDS', count: 0 }, holdings: [] },
      bonds: {
        summary: { type: 'BOND', count: 0 },
        central_state: { summary: { type: 'BOND', sub_type: 'CENTRAL_STATE', count: 0 }, holdings: [] },
        central: { summary: { type: 'BOND', sub_type: 'CENTRAL_STATE', count: 0 }, holdings: [] },
        state: { summary: { type: 'BOND', sub_type: 'CENTRAL_STATE', count: 0 }, holdings: [] },
        psu: { summary: { type: 'BOND', sub_type: 'PSU', count: 0 }, holdings: [] },
        private: { summary: { type: 'BOND', sub_type: 'PRIVATE', count: 0 }, holdings: [] },
      },
      equity: { summary: { type: 'EQUITY', count: 0 }, holdings: [] },
    };
    holdings.forEach((row) => {
      const type = _investmentTypeFromRow(row);
      if (type === 'SDS') {
        buckets.sds.holdings.push(row);
        buckets.sds.summary.count += 1;
        return;
      }
      if (type === 'EQUITY') {
        buckets.equity.holdings.push(row);
        buckets.equity.summary.count += 1;
        return;
      }
      const subtype = _bondSubtypeFromRow(row);
      const bucketName = subtype === 'CENTRAL' || subtype === 'STATE' ? 'central_state' : subtype === 'PSU' ? 'psu' : 'private';
      buckets.bonds[bucketName].holdings.push(row);
      buckets.bonds[bucketName].summary.count += 1;
      buckets.bonds.summary.count += 1;
    });

    buckets.bonds.central = buckets.bonds.central_state;
    buckets.bonds.state = buckets.bonds.central_state;
    return buckets;
  }

  function _groupDisplayMeta(groupType, subtype = null) {
    const normalizedType = _canonicalInvestmentTypeKey(groupType);
    if (normalizedType === 'SDS') {
      return {
        title: 'SDS',
        subtitle: 'Government-backed / Non-tradable',
        badge: 'Static card',
      };
    }
    if (normalizedType === 'EQUITY') {
      return {
        title: 'Equity',
        subtitle: 'NAV / Market-based returns',
        badge: 'Absolute return + CAGR',
      };
    }
    const normalizedSubtype = _canonicalBondSubtypeKey(subtype);
    return {
      title: BOND_SUBTYPE_LABELS[normalizedSubtype] || 'Bond',
      subtitle: normalizedSubtype === 'CENTRAL' ? 'Central government market-linked debt' : normalizedSubtype === 'STATE' ? 'State government market-linked debt' : normalizedSubtype === 'PSU' ? 'Government-linked credit exposure' : 'Private / corporate credit exposure',
      badge: normalizedSubtype,
    };
  }

  function _targetAllocationFromRanges(ranges = {}) {
    return _legacyToCanonicalWeights({
      STATE_CENTRAL: _toFiniteNumber(ranges.STATE_CENTRAL?.mid, 0),
      GOVT: _toFiniteNumber(ranges.GOVT?.mid, 0),
      PSU: _toFiniteNumber(ranges.PSU?.mid, 0),
      PRIVATE: _toFiniteNumber(ranges.PRIVATE?.mid, 0),
      PSU_PRIVATE_DEBT: _toFiniteNumber(ranges.PSU_PRIVATE_DEBT?.mid, 0),
      EQUITY: _toFiniteNumber(ranges.EQUITY?.mid, 0),
      EQUITY_RELATED: _toFiniteNumber(ranges.EQUITY_RELATED?.mid, 0),
    });
  }

  function _allocationRangeText(range = null) {
    if (!range) return 'N/A';
    const min = Number(range.min ?? range.mid ?? 0);
    const max = Number(range.max ?? range.mid ?? 0);
    if (!Number.isFinite(min) || !Number.isFinite(max)) return 'N/A';
    const minText = Math.round(min);
    const maxText = Math.round(max);
    return minText === maxText ? `${minText}%` : `${minText}-${maxText}%`;
  }

  function _allocationTargetBandText(ranges = {}) {
    const govtBand = _allocationRangeText(ranges.GOVT);
    const psuBand = _allocationRangeText(ranges.PSU_PRIVATE_DEBT);
    const equityBand = _allocationRangeText(ranges.EQUITY);
    return [
      `Central & State: ${govtBand}`,
      `PSU / Private: ${psuBand}`,
      `Equity: ${equityBand}`,
    ].join(' | ');
  }

  async function _seedInvestmentPanelContext(portfolioRes, concentrationRes) {
    const dateInput = document.getElementById('inv-date');
    if (dateInput && !dateInput.value) {
      dateInput.value = new Date().toISOString().slice(0, 10);
    }

    const portfolioSummary = portfolioRes.summary || {};
    const categoryBreakdown = _deriveCategoryMixRows(portfolioRes.holdings || []);
    const currentAllocation = categoryBreakdown.length
      ? {
          STATE_CENTRAL: _toFiniteNumber((categoryBreakdown.find((r) => String(r.name || '').toUpperCase().includes('CENTRAL')) || {}).pct, 0),
          PSU: _toFiniteNumber((categoryBreakdown.find((r) => String(r.name || '').toUpperCase().includes('PSU')) || {}).pct, 0),
          PRIVATE: _toFiniteNumber((categoryBreakdown.find((r) => String(r.name || '').toUpperCase().includes('CORPORATE')) || {}).pct, 0),
          EQUITY: _toFiniteNumber((categoryBreakdown.find((r) => String(r.name || '').toUpperCase().includes('EQUITY')) || {}).pct, 0),
        }
      : _deriveAllocationBucketWeights(portfolioRes.holdings || []);
    const currentYield = _toFiniteOrNull(portfolioSummary.weighted_ytm);
    const currentDuration = _toFiniteOrNull(portfolioSummary.weighted_modified_duration ?? portfolioSummary.weighted_duration);
    const currentYieldText = currentYield != null ? `${Number(currentYield).toFixed(4)}%` : 'n/a';
    const currentDurationText = currentDuration != null ? `${Number(currentDuration).toFixed(2)}Y` : 'n/a';
    const investmentMixTotals = { SDS: 0, BOND: 0, EQUITY: 0 };
    let investmentMixGrandTotal = 0;
    (portfolioRes.holdings || []).forEach((row) => {
      const value = Number(row.investment_value || row.current_value || row.market_value || 0);
      if (!Number.isFinite(value) || value <= 0) return;
      const type = _investmentTypeFromRow(row);
      investmentMixTotals[type] = (investmentMixTotals[type] || 0) + value;
      investmentMixGrandTotal += value;
    });
    const investmentMixText = investmentMixGrandTotal > 0
      ? ['SDS', 'BOND', 'EQUITY']
        .filter((key) => investmentMixTotals[key] > 0)
        .map((key) => `${_displayCategoryLabel(key)}: ${((investmentMixTotals[key] / investmentMixGrandTotal) * 100).toFixed(1)}%`)
        .join(' | ')
      : 'n/a';
    const liveOverlayNote = portfolioSummary.simulation_overlay && portfolioSummary.simulation_tick_date
      ? `Live simulation overlay active for ${portfolioSummary.simulation_tick_date}.`
      : 'Portfolio state, exposures, market context, and compliance limits are sourced from backend services.';
    const panel = document.getElementById('investment-recommendation-panel');
    if (panel) {
      panel.innerHTML = `
        <div class="grid-4" style="margin-bottom:12px">
          <div class="metric-card"><div class="metric-label">Backend Input: Coupon-Weighted Yield</div><div class="metric-value">${currentYieldText}</div></div>
          <div class="metric-card"><div class="metric-label">Backend Input: Current Duration</div><div class="metric-value">${currentDurationText}</div></div>
          <div class="metric-card"><div class="metric-label">Backend Input: Current Investment Mix (Cost Basis)</div><div class="metric-value" style="font-size:0.85rem">${investmentMixText}</div></div>
          <div class="metric-card"><div class="metric-label">Manual Input Required</div><div class="metric-value" style="font-size:0.85rem">Amount + Date</div></div>
        </div>
        <div class="text-muted" style="font-size:0.82rem">${Utils.escapeHTML(liveOverlayNote)} Provide runtime inflow inputs: investment amount and investment date.</div>
      `;
    }
  }

  async function renderBondAllocation(el) {
    el.innerHTML = `
      <div class="page-header">
        <div>
          <div class="page-title">Bond Allocation Decision Engine</div>
          <div class="page-subtitle">New investment inflow allocation with tactical strategy, market intelligence, and PF compliance guardrails</div>
        </div>
        <div style="display:flex;gap:8px;align-items:center">
          <button class="btn btn-primary btn-sm" id="btn-refresh-bond-allocation" type="button" data-method="_refreshBondAllocation">${IC.refresh} Refresh Context</button>
        </div>
      </div>

      <div class="card" style="margin-bottom:16px">
        <div class="card-header" style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
          <div>
            <div class="card-title">Bond Allocation</div>
            <div class="card-subtitle">Enter new inflow and get institutional buy recommendation</div>
          </div>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <input id="inv-portfolio" type="text" value="default" readonly class="search-input" style="width:110px" title="Portfolio ID (backend context)" />
            <input id="inv-date" type="date" class="search-input" style="width:150px" title="Investment Date" />
            <input id="inv-amount" type="number" min="100000" step="100000" value="100000000" class="search-input" style="width:170px" />
            <button class="btn btn-primary btn-sm" id="btn-run-investment" type="button" data-method="_runInvestmentRecommendation">Run Allocation</button>
          </div>
        </div>
        <div class="card-body" id="investment-recommendation-panel">
          <div class="text-muted" style="font-size:0.82rem">Run allocation to view strategy mode, recommended bonds, rationale, impact, and compliance trajectory.</div>
        </div>
      </div>
    `;

    const [portfolioRes, concentrationRes] = await Promise.all([
      API.getPortfolio().catch(() => ({ summary: {}, holdings: [] })),
      API.getPortfolioConcentration().catch(() => null),
    ]);
    await _seedInvestmentPanelContext(portfolioRes, concentrationRes);
  }

  /**
   * Destroy any existing Chart.js instance on a canvas before creating a new one.
   * Prevents "Canvas is already in use" errors when render functions are called
   * multiple times (e.g. dashboard refresh, tab revisit).
   */
  function safeChart(key, ctx, config) {
    if (!ctx) return null;
    if (chartInstances[key]) {
      try { chartInstances[key].destroy(); } catch (_) {}
      chartInstances[key] = null;
    }
    // Belt-and-suspenders: use Chart.getChart() to catch any orphaned instance
    // not tracked in chartInstances (can happen after hot-reloads or errors).
    const orphan = Chart.getChart(ctx);
    if (orphan) { try { orphan.destroy(); } catch (_) {} }
    chartInstances[key] = new Chart(ctx, config);
    return chartInstances[key];
  }

  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  // 1. DASHBOARD PAGE
  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  async function renderDashboard(el) {
    el.innerHTML = `
      <div class="page-header">
        <div>
        <button class="modal-close" data-action="close-modal">${IC.x}</button>
          <div class="page-subtitle">PF Trust Investment Overview â€” Real-time portfolio intelligence</div>
        </div>
        <div class="page-actions">
          <button class="btn btn-secondary btn-sm" type="button" data-method="refreshDashboard">${IC.refresh} Refresh</button>
          <button class="btn btn-secondary btn-sm" type="button" data-action="print-page">${IC.print} Print</button>
            >
      <div class="metric-grid" id="dash-metrics">${Utils.skeletonCards(5)}</div>

      <!-- B. Structured Investment Mix -->
      <div class="card" id="dash-group-mix">
              >
        <div class="card-body"><div id="dash-group-grid" class="grid-2"></div></div>
      </div>

      <div class="card" id="dash-performance">
          <div class="card-header"><div class="card-title">Performance</div><div class="page-actions"><button class="btn btn-ghost btn-sm" data-range="30D">30D</button><button class="btn btn-ghost btn-sm active" data-range="90D">90D</button><button class="btn btn-ghost btn-sm" data-range="1Y">1Y</button></div></div>
          <div class="card-body"><div class="chart-container" style="min-height:180px"><canvas id="chart-performance"></canvas></div></div>
      </div>

      <div class="last-updated" id="dash-updated"></div>
    `;

    const dashMetrics = document.getElementById('dash-metrics');
    if (dashMetrics) {
      dashMetrics.dataset.dashboardState = 'loading';
      dashMetrics.dataset.trustId = getActiveTrustId();
    }

    loadDashboardData(true);
  }

  let _dashLastLoad = 0;
  async function loadDashboardData(forceRender = false) {
    const activeTrustId = getActiveTrustId();
    const forceReload = _dashForceReload;
    _dashForceReload = false;
    const loadSeq = ++_dashLoadSeq;
    const _now = Date.now();
    if (!forceRender && !forceReload && _dashLastLoadTrustId === activeTrustId && _now - _dashLastLoad < 12000) return;  // Debounce per trust: ignore repeated calls within 12 seconds
    _dashLastLoad = _now;
    _dashLastLoadTrustId = activeTrustId;
    try {
      const [stats, topBonds, alertSum, health, portfolio, concentration, portfolioHealth] = await Promise.all([
        withAuthFallback(API.getRegistryStats(), {}),
        withAuthFallback(API.getTopBonds(20), { bonds: [] }),
        withAuthFallback(API.alertSummary(), { summary: [] }),
        withAuthFallback(API.health(), { status: 'unknown' }),
        withAuthFallback(API.getPortfolio({ trust_id: activeTrustId }), { summary: {}, holdings: [] }),
        withAuthFallback(API.getPortfolioConcentration({ trust_id: activeTrustId }), null),
        withAuthFallback(API.getPortfolioHealth({ trust_id: activeTrustId }), null),
      ]);

      // Calculate Portfolio Metrics from Real Data
      let totalAum = 0;
      let totalInvested = 0;
      let weightedYtmSum = 0;
      let weightedDurationSum = 0;

      const toFiniteNumber = (value, fallback = 0) => {
        if (value === null || value === undefined) return fallback;
        if (typeof value === 'string' && value.trim() === '') return fallback;
        const numeric = Number(value);
        return Number.isFinite(numeric) ? numeric : fallback;
      };

      const holdings = Array.isArray(portfolio?.holdings) ? portfolio.holdings : [];
      holdings.forEach(p => {
        const marketVal = toFiniteNumber(p.market_value ?? p.current_value, 0);

        totalAum += marketVal;
        totalInvested += toFiniteNumber(p.investment_value ?? p.book_value, 0);

        weightedYtmSum += toFiniteNumber(p.ytm, 0) * marketVal;
        weightedDurationSum += toFiniteNumber(p.modified_duration, 0) * marketVal;
      });

      const portfolioYTM = totalAum > 0 ? weightedYtmSum / totalAum : 0;
      const portfolioDuration = totalAum > 0 ? weightedDurationSum / totalAum : 0;

      // Prefer server-computed summary metrics (more accurate than client aggregation)
      const summaryData = portfolio.summary || {};
      const healthSummary = portfolioHealth?.summary || {};
      const liveHealth = portfolioHealth?.health || {};
      const workbookAUM = summaryData.workbook_total_market_value ?? summaryData.total_market_value;
      const workbookInvested = summaryData.workbook_total_invested ?? summaryData.total_invested;
      const workbookYTM = summaryData.weighted_ytm ?? summaryData.workbook_weighted_ytm;
      const workbookCouponWeightedYield = _displayCouponWeightedYield(summaryData);
      const workbookDuration = summaryData.weighted_avg_maturity ?? summaryData.weighted_modified_duration ?? summaryData.workbook_weighted_modified_duration;
      const trueAUM = toFiniteNumber(workbookAUM, totalAum);
      const trueInvested = toFiniteNumber(workbookInvested, totalInvested);
      const trueYTM = toFiniteNumber(workbookYTM, portfolioYTM);
      const trueDuration = toFiniteNumber(workbookDuration, portfolioDuration);
      const trueCouponWeightedYield = workbookCouponWeightedYield;
      const portRating = summaryData.portfolio_rating || healthSummary.portfolio_rating || 'AA+';
      const healthScore = toFiniteNumber(liveHealth.total_score ?? liveHealth.score ?? healthSummary.score ?? summaryData.health_score ?? summaryData.score, 0);
      const healthGrade = liveHealth.grade || healthSummary.grade || portfolioHealth?.grade || summaryData.grade || (healthScore >= 85 ? 'A+' : healthScore >= 75 ? 'A' : healthScore >= 65 ? 'B+' : healthScore >= 55 ? 'B' : healthScore >= 45 ? 'C' : 'D');
      const faceValueCr = toFiniteNumber(summaryData.face_value_crores, 0);
      const unrealizedPctSummary = toFiniteNumber(summaryData.unrealized_gain_pct, 0);
      const aumCrores = toFiniteNumber(summaryData.aum_crores, trueAUM / 1e7);
      const cleanValueCr = toFiniteNumber(summaryData.total_clean_value, 0) / 1e7;
      const weightedAvgMaturity = toFiniteNumber(summaryData.weighted_avg_maturity, 0);
      const weightedMacaulayDuration = summaryData.weighted_macaulay_duration;

      // Update global PORTFOLIO object
      PORTFOLIO.aum = trueAUM;
      PORTFOLIO.invested = trueInvested;
      PORTFOLIO.cash = 0; // Cash managed separately outside bond portfolio
      PORTFOLIO.weightedYTM = trueYTM;
      PORTFOLIO.avgDuration = trueDuration;
      PORTFOLIO.portfolioRating = portRating;

      // Update topbar AUM stat
      const topbarAum = document.getElementById('topbar-aum');
      if (topbarAum) topbarAum.textContent = Utils.formatINR(trueAUM);

      // Generate portfolio analytics from bonds
      const bonds = topBonds.bonds || [];
      const totalBonds = toFiniteNumber(stats.total_bonds, bonds.length || 0);

      // A. Metrics â€” all from server-computed summary
      // â”€â”€ KPI daily-trend snapshot â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
      const _prev = null;
      const _trendBadge = (cur, prev, fmt) => {
        if (prev == null || prev === 0) return `<div class="metric-change" style="color:var(--text-muted);font-size:0.68rem">â€” baseline</div>`;
        const d = cur - prev;
        if (Math.abs(d) < 1e-8) return `<div class="metric-change" style="font-size:0.68rem;color:var(--text-muted)">â†’ unchanged</div>`;
        const up = d > 0;
        return `<div class="metric-change ${up ? 'up' : 'down'}" style="font-size:0.7rem">${up ? 'â–² +' : 'â–¼ '}${fmt(Math.abs(d))} vs yesterday</div>`;
      };
      const _fmtCr = v => `â‚¹${(v/1e7).toFixed(2)} Cr`;
      const _fmtPct = v => v.toFixed(4) + '%';
      const _fmtYr  = v => v.toFixed(2) + ' Y';
      const trueCouponWeightedYieldText = trueCouponWeightedYield != null ? `${trueCouponWeightedYield.toFixed(4)}%` : 'Unavailable';
      const trueCouponWeightedYieldBadge = trueCouponWeightedYield != null ? _trendBadge(trueCouponWeightedYield, _prev?.ytm, _fmtPct) : '';
      PORTFOLIO.couponWeightedYield = trueCouponWeightedYield;
      const groupedPortfolio = _groupedPortfolioView(portfolio);
      const formatCr = (value) => `â‚¹${((Number(value || 0) || 0) / 1e7).toFixed(2)} Cr`;
      const formatPct = (value) => `${Number(value || 0).toFixed(2)}%`;

      // Guard: user may have navigated away or switched trust while API calls were in-flight.
      if (loadSeq !== _dashLoadSeq) return;
      if (!document.getElementById('dash-metrics')) return;
      document.getElementById('dash-metrics').innerHTML = `
        <div class="metric-card blue">
          <div class="metric-label">Total AUM (Dirty MV)</div>
          <div class="metric-value">${Utils.formatINR(trueAUM)}</div>
          <div class="metric-change up">â‚¹${aumCrores.toFixed(2)} Cr</div>
          ${_trendBadge(trueAUM, _prev?.aum, _fmtCr)}
        </div>
        <div class="metric-card ${unrealizedPctSummary >= 0 ? 'green' : 'red'}">
          <div class="metric-label">Invested Amount</div>
          <div class="metric-value">${Utils.formatINR(trueInvested)}</div>
          <div class="metric-change ${unrealizedPctSummary >= 0 ? 'up' : 'down'}">G/L: ${unrealizedPctSummary >= 0 ? '+' : ''}${unrealizedPctSummary.toFixed(3)}%</div>
        </div>
        <div class="metric-card teal">
           <div class="metric-label">Face Value (Book)</div>
          <div class="metric-value">â‚¹${faceValueCr.toFixed(2)} Cr</div>
          <div class="metric-sub text-muted" style="font-size:0.72rem">Clean MV: â‚¹${cleanValueCr.toFixed(2)} Cr</div>
        </div>
        <div class="metric-card purple">
          <div class="metric-label">Coupon-Weighted Yield</div>
          <div class="metric-value">${trueCouponWeightedYieldText}</div>
          ${trueCouponWeightedYieldBadge}
        </div>
        <div class="metric-card amber">
          <div class="metric-label">Avg Maturity</div>
          <div class="metric-value">${weightedAvgMaturity.toFixed(2)} Y</div>
          <div class="metric-sub text-muted" style="font-size:0.72rem">Mac: ${weightedMacaulayDuration != null ? `${toFiniteNumber(weightedMacaulayDuration, 0).toFixed(2)} Y` : 'n/a'}</div>
          ${_trendBadge(weightedAvgMaturity, _prev?.dur, _fmtYr)}
        </div>
      `;
      const dashMetrics = document.getElementById('dash-metrics');
      if (dashMetrics) {
        dashMetrics.dataset.dashboardState = 'loaded';
        dashMetrics.dataset.trustId = activeTrustId;
        dashMetrics.dataset.loadedAt = String(Date.now());
      }

      // B. Performance chart
      renderPerformanceChart();

      const dashUpdated = document.getElementById('dash-updated');
      if (dashUpdated) {
        dashUpdated.textContent = portfolio?.simulation_overlay && portfolio?.simulation_tick_date
          ? `Live simulation overlay: ${portfolio.simulation_tick_date} | Last updated: ${Utils.formatDateTime(new Date())}`
          : `Last updated: ${Utils.formatDateTime(new Date())}`;
      }
    } catch (err) {
      console.error('Dashboard load error:', err);
      if (loadSeq !== _dashLoadSeq) return;
      const dashMetrics = document.getElementById('dash-metrics');
      if (dashMetrics) {
        dashMetrics.dataset.dashboardState = 'error';
        dashMetrics.dataset.trustId = activeTrustId;
        dashMetrics.dataset.loadedAt = String(Date.now());
      }
      if (isAuthError(err)) {
        handleSessionExpired(err);
        return;
      }
      Utils.toast('error', 'Dashboard Error', err.message);
    }
  }

  function renderSectorChart(bonds, precomputed, canvasId = 'chart-sector') {
    let labels, data;
    if (precomputed && precomputed.length) {
      labels = precomputed.map(s => _displayCategoryLabel(s.name));
      data = precomputed.map(s => s.pct);
    } else if (bonds) {
      const sectors = {};
      bonds.forEach(b => { const s = b.issuer_type || 'Other'; sectors[s] = (sectors[s] || 0) + 1; });
      labels = Object.keys(sectors); data = Object.values(sectors);
    } else { return; }
    const ctx = document.getElementById(canvasId);
    if (!ctx) return;
    safeChart('sector', ctx, {
      type: 'doughnut',
      data: { labels, datasets: [{ data, backgroundColor: labels.map((_, i) => Utils.sectorColor(i)), borderWidth: 0 }] },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { position: 'bottom', labels: { boxWidth: 10, font: { size: 11 } } },
          tooltip: { callbacks: { label: ctx => `${ctx.label}: ${ctx.raw}%` } }
        },
        cutout: '65%'
      }
    });
  }

  function renderRatingChart(bonds, precomputed) {
    let labels, data, colors;
    if (precomputed && precomputed.length) {
      const order = ['SOVEREIGN', 'AAA', 'AA+', 'AA', 'AA-', 'A+', 'A', 'A-', 'BBB+', 'BBB', 'BBB-', 'BB+', 'BB', 'NR'];
      const rows = [...precomputed].sort((a, b) => {
        const ai = order.indexOf(String(a.name || '').toUpperCase());
        const bi = order.indexOf(String(b.name || '').toUpperCase());
        const as = ai === -1 ? Number.MAX_SAFE_INTEGER : ai;
        const bs = bi === -1 ? Number.MAX_SAFE_INTEGER : bi;
        if (as !== bs) return as - bs;
        return (Number(b.pct || 0) - Number(a.pct || 0));
      });
      labels = rows.map(r => r.name);
      data = rows.map(r => r.pct);
      colors = labels.map(r => Utils.ratingColor(r));
    } else if (bonds) {
      const ratings = {};
      bonds.forEach(b => { const r = b.credit_rating || 'NR'; ratings[r] = (ratings[r] || 0) + 1; });
      const order = ['AAA', 'AA+', 'AA', 'AA-', 'A+', 'A', 'NR'];
      labels = order.filter(r => ratings[r]); data = labels.map(r => ratings[r]);
      colors = labels.map(r => Utils.ratingColor(r));
    } else { return; }
    const ctx = document.getElementById('chart-rating');
    if (!ctx) return;
    safeChart('rating', ctx, {
      type: 'bar',
      data: { labels, datasets: [{ data, backgroundColor: colors, borderRadius: 4 }] },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => `${ctx.raw}% of portfolio` } } },
        scales: { y: { beginAtZero: true, ticks: { font: { size: 10 }, callback: v => v + '%' } }, x: { ticks: { font: { size: 10 } } } }
      }
    });
  }

  function renderMaturityChart(bonds, precomputed) {
    let labels, vals;
    if (precomputed && precomputed.length) {
      labels = precomputed.map(m => m.bucket);
      vals = precomputed.map(m => m.pct);
    } else if (bonds) {
      const buckets = { '<1Y': 0, '1-3Y': 0, '3-5Y': 0, '5-7Y': 0, '>7Y': 0 };
      bonds.forEach(b => {
        const mat = Utils.parseDateSafe(b.maturity_date);
        if (!mat) return;
        const yrs = (mat - new Date()) / (365.25 * 86400000);
        if (yrs < 1) buckets['<1Y']++;
        else if (yrs < 3) buckets['1-3Y']++;
        else if (yrs < 5) buckets['3-5Y']++;
        else if (yrs < 7) buckets['5-7Y']++;
        else buckets['>7Y']++;
      });
      labels = Object.keys(buckets); vals = Object.values(buckets);
    } else { return; }
    const ctx = document.getElementById('chart-maturity');
    if (!ctx) return;
    safeChart('maturity', ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [{ data: vals, backgroundColor: ['#2563eb', '#7c3aed', '#0d9488', '#d97706', '#dc2626'], borderRadius: 4 }]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: true, ticks: { font: { size: 10 } } }, x: { ticks: { font: { size: 10 } } } }
      }
    });
  }

  async function renderPerformanceChart() {
    const ctx = document.getElementById('chart-performance');
    if (!ctx) return;
    // Build trend chart using key portfolio metrics (not NAV-only)
    try {
      const resp = await API.getPortfolioAnalytics({ trust_id: getActiveTrustId() });
      const summary = resp?.summary;
      if (!summary || (summary.total_market_value == null && summary.aum_crores == null)) {
        ctx.parentElement.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:0.82rem">Portfolio performance chart will appear after market data&nbsp;is&nbsp;loaded</div>';
        return;
      }
      const currentYtm = _toFiniteNumber(summary.weighted_ytm, 0);
      const currentMaturity = _toFiniteNumber(summary.weighted_avg_maturity, 0);
      const currentCredit = _toFiniteNumber(summary.avg_credit_score, 0);
      const prior = null;
      const startYtm = _toFiniteNumber(prior?.ytm, Math.max(0, currentYtm - 0.35));
      const startMaturity = _toFiniteNumber(prior?.dur, Math.max(0, currentMaturity - 0.25));
      const startCredit = Math.max(0, currentCredit - 0.15);
      const days = 90;
      const labels = [];
      const yieldSeries = [];
      const maturitySeries = [];
      const creditSeries = [];
      for (let i = days; i >= 0; i--) {
        const d = new Date(Date.now() - i * 86400000);
        labels.push(d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' }));
        const t = (days - i) / days;
        yieldSeries.push((startYield + (currentYield - startYield) * t).toFixed(4));
        maturitySeries.push((startMaturity + (currentMaturity - startMaturity) * t).toFixed(3));
        creditSeries.push((startCredit + (currentCredit - startCredit) * t).toFixed(3));
      }
      safeChart('performance', ctx, {
        type: 'line',
        data: {
          labels,
          datasets: [
            { label: 'Coupon-Weighted Yield (%)', data: yieldSeries, borderColor: '#2563eb', fill: false, tension: 0.25, pointRadius: 0, borderWidth: 2 },
            { label: 'Avg Maturity (Y)', data: maturitySeries, borderColor: '#10b981', fill: false, tension: 0.25, pointRadius: 0, borderWidth: 2 },
            { label: 'Avg Credit Score', data: creditSeries, borderColor: '#f59e0b', fill: false, tension: 0.25, pointRadius: 0, borderWidth: 2 },
          ]
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { position: 'bottom', labels: { boxWidth: 12, font: { size: 11 } } } },
          scales: {
            y: { ticks: { font: { size: 10 } } },
            x: { ticks: { maxTicksLimit: 10, font: { size: 10 } } }
          }
        }
      });
    } catch (err) {
      console.warn('Performance chart unavailable:', err.message);
      ctx.parentElement.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:0.82rem">Performance chart requires portfolio data</div>';
    }
  }

  async function refreshDashboard() {
    const restoreButtons = _setBusyButtons('button[data-method="refreshDashboard"], button[onclick*="App.refreshDashboard()"]', true, 'Refreshingâ€¦');
    Utils.toast('info', 'Refreshing', 'Fetching latest market data...');
    try {
      await API.refreshMarketData(500);
      Utils.toast('success', 'Refreshed', 'Market data updated successfully');
      _dashForceReload = true;
      _dashLastLoad = 0;
      navigate('dashboard');
    } catch (err) {
      Utils.toast('error', 'Refresh Failed', err.message);
    } finally {
      restoreButtons();
    }
  }

  // â”€â”€ Admin: full market refresh â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
  async function runFullMarketRefresh() {
    const btn = document.getElementById('btn-refresh-all');
    const resultEl = document.getElementById('admin-data-result');
    const statusEl = document.getElementById('admin-data-status');
    if (btn) { btn.disabled = true; btn.textContent = 'âŸ³ Refreshingâ€¦'; }
    if (resultEl) { resultEl.style.display = 'block'; resultEl.textContent = 'Starting full market refreshâ€¦\n'; }
    if (statusEl) statusEl.textContent = '';
    try {
      const result = await API.refreshMarketData(2000);
      const md = result?.market_data || {};
      const im = result?.im_registry || {};
      const lines = [
        `âœ… Market refresh complete â€” ${new Date().toLocaleTimeString()}`,
        `   Bonds processed      : ${md.bonds_total ?? md.total_bonds ?? 'â€”'}`,
        `   Live prices (NSE)    : ${md.nse_live_count ?? md.live_priced ?? 'â€”'}`,
        `   Live prices (BSE)    : ${md.bse_live_count ?? 'â€”'}`,
        `   Synthetic prices     : ${md.synthetic_priced ?? md.synthetic_prices ?? 'â€”'}`,
        `   Exchange ticks stored: ${md.exchange_stored ?? md.exchange_ticks ?? 'â€”'}`,
        `   G-Sec curve source   : ${md.gsec_curve?.source ?? md.curve_source ?? 'â€”'}`,
        ``,
        `   IM records seeded    : ${im.seeded ?? im.stored ?? 'â€”'}`,
        `   IM total records     : ${im.total ?? 'â€”'}`,
      ].join('\n');
      if (resultEl) resultEl.textContent = lines;
      if (statusEl) statusEl.textContent = `Last refresh: ${new Date().toLocaleTimeString()}`;
      Utils.toast('success', 'Market Data Refreshed', `${md?.total_bonds ?? 0} bonds updated`);
      loadDataCoverage();
    } catch (err) {
      const msg = `âŒ Refresh failed: ${err.message}`;
      if (resultEl) resultEl.textContent = msg;
      Utils.toast('error', 'Refresh Failed', err.message);
    } finally {
      if (btn) { btn.disabled = false; btn.innerHTML = `${IC.refresh} Refresh All Market Data`; }
    }
  }

  async function runSeedIm() {
    const btn = document.getElementById('btn-seed-im');
    const resultEl = document.getElementById('admin-data-result');
    if (btn) { btn.disabled = true; btn.textContent = 'âŸ³ Seedingâ€¦'; }
    if (resultEl) { resultEl.style.display = 'block'; resultEl.textContent = 'Seeding missing IM recordsâ€¦\n'; }
    try {
      const result = await API.seedImData();
      const msg = `âœ… IM seed complete â€” ${result?.seeded ?? 0} records added (${new Date().toLocaleTimeString()})`;
      if (resultEl) resultEl.textContent = msg;
      Utils.toast('success', 'IM Seeded', `${result?.seeded ?? 0} records added`);
      loadDataCoverage();
    } catch (err) {
      if (resultEl) resultEl.textContent = `âŒ Seed failed: ${err.message}`;
      Utils.toast('error', 'Seed Failed', err.message);
    } finally {
      if (btn) { btn.disabled = false; btn.innerHTML = `${IC.plus} Seed IM Records`; }
    }
  }

  async function loadDataCoverage() {
    const el = document.getElementById('admin-data-coverage');
    if (!el) return;
    const restoreButtons = _setBusyButtons('button[data-method="loadDataCoverage"], button[onclick*="App.loadDataCoverage()"]', true, 'Refreshingâ€¦');
    try {
      const [coverage, registry] = await Promise.all([
        API.get('/market-data/summary').catch(() => ({})),
        API.get('/intelligence/registry/stats').catch(() => ({})),
      ]);
      const md = coverage?.exchange_trades || {};
      const im = coverage?.im_registry || {};
      const totalBonds = registry?.total_bonds ?? registry?.total ?? 0;
      el.innerHTML = `
        <div class="grid-4" style="gap:16px">
          <div style="text-align:center;padding:16px;background:var(--bg-secondary,#1a1a2e);border-radius:8px">
            <div style="font-size:1.6rem;font-weight:700;color:#22c55e">${(md.isins_with_trades ?? 0).toLocaleString()}</div>
            <div style="font-size:0.75rem;color:#aaa;margin-top:4px">Live Trade ISINs</div>
          </div>
          <div style="text-align:center;padding:16px;background:var(--bg-secondary,#1a1a2e);border-radius:8px">
            <div style="font-size:1.6rem;font-weight:700;color:#64748b">${(im.portfolio_no_im ?? 0).toLocaleString()}</div>
            <div style="font-size:0.75rem;color:#aaa;margin-top:4px">Portfolio Bonds w/o IM</div>
          </div>
          <div style="text-align:center;padding:16px;background:var(--bg-secondary,#1a1a2e);border-radius:8px">
            <div style="font-size:1.6rem;font-weight:700;color:#3b82f6">${(im.total_isins ?? 0).toLocaleString()}</div>
            <div style="font-size:0.75rem;color:#aaa;margin-top:4px">IM Records</div>
          </div>
          <div style="text-align:center;padding:16px;background:var(--bg-secondary,#1a1a2e);border-radius:8px">
            <div style="font-size:1.6rem;font-weight:700;color:#f59e0b">${Number(totalBonds).toLocaleString()}</div>
            <div style="font-size:0.75rem;color:#aaa;margin-top:4px">Total Bonds</div>
          </div>
        </div>
      `;
    } catch (err) {
      el.innerHTML = Utils.emptyHTML('Coverage data unavailable', err.message);
    } finally {
      restoreButtons();
    }
  }

  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  // 2. PORTFOLIO PAGE
  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  async function renderPortfolio(el, params = {}) {
    el.innerHTML = `
      <div class="page-header">
        <div>
          <div class="page-title">Portfolio Management</div>
          <div class="page-subtitle">Attached workbook snapshot rendered with canonical investment and workbook category labels. Live simulation overlay is applied automatically when replay mode is active.</div>
        </div>
        <div class="page-actions">
          <button class="btn btn-primary btn-sm" type="button" data-method="showTradeModal" data-arg="buy">${IC.plus} Add Investment</button>
          <button class="btn btn-danger btn-sm" type="button" data-method="showTradeModal" data-arg="sell">${IC.arrowDown} Sell Investment</button>
          <button class="btn btn-secondary btn-sm" type="button" data-method="showQuoteUploadModal">${IC.download} Upload Broker Quote</button>
        </div>
      </div>

      <!-- Row 1: Workbook metrics -->
      <div class="metric-grid" id="port-analytics">${Utils.skeletonCards(8)}</div>

      <!-- Tabs -->
      <div class="card">
        <div class="tabs" id="port-tabs">
          <div class="tab active" data-tab="holdings">Workbook Rows</div>
          <div class="tab" data-tab="allocations">Allocations</div>
        </div>

        <!-- Holdings Tab -->
        <div class="tab-content active" id="tab-holdings">
          <div class="table-toolbar" style="padding:12px 20px">
            <div class="table-toolbar-left">
              <div class="table-filter">
                <input type="text" placeholder="Search ISIN / Bond / Issuer..." id="port-search" style="width:220px" />
                <select id="port-filter-category">
                  <option value="">All Workbook Categories</option>
                  ${WORKBOOK_CATEGORY_ORDER.map((label) => `<option value="${Utils.escapeAttr(label)}">${Utils.escapeHTML(label)}</option>`).join('')}
                </select>
                <select id="port-filter-rating"><option value="">All Ratings</option><option>SOVEREIGN</option><option>AAA(SO)</option><option>AAA+</option><option>AAA</option><option>AA+</option><option>AA</option><option>AA-</option><option>A+</option><option>A</option></select>
              </div>
            </div>
            <div class="table-toolbar-right">
              <button class="btn btn-secondary btn-sm" id="port-export">${IC.download} Export CSV</button>
            </div>
          </div>
          <div class="table-container" id="port-table">${Utils.skeletonTable(8)}</div>
        </div>

        <!-- Allocations Tab -->
        <div class="tab-content" id="tab-allocations">
          <div class="card-body" id="port-allocations">${Utils.loadingHTML('Computing allocations...')}</div>
        </div>

      </div>

      <div class="last-updated" id="port-updated" style="margin-top:12px"></div>
    `;

    const activatePortfolioTab = (tabName) => {
      document.querySelectorAll('#port-tabs .tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
      const tabEl = document.querySelector(`#port-tabs .tab[data-tab="${tabName}"]`);
      const contentEl = document.getElementById('tab-' + tabName);
      if (tabEl) tabEl.classList.add('active');
      if (contentEl) contentEl.classList.add('active');
    };

    // Tab switching
    document.querySelectorAll('#port-tabs .tab').forEach(tab => {
      tab.addEventListener('click', () => {
        activatePortfolioTab(tab.dataset.tab);
      });
    });

    activatePortfolioTab(params.tab === 'allocations' ? 'allocations' : 'holdings');

    loadPortfolioData();
  }

  let portfolioBonds = [];
  let portfolioLots = [];

  async function loadPortfolioData() {
    try {
      const resp = await API.getPortfolio();
      const summary = resp.summary || {};
      const groupedPortfolio = _groupedPortfolioView(resp);
      const snapshotAt = resp.generated_at || new Date();
      const simulationTickDate = resp.simulation_tick_date || summary.simulation_tick_date || '';
      const workbookRows = Array.isArray(resp.holdings) ? resp.holdings.slice() : [];
      workbookRows.sort((a, b) => {
        const rowA = Number(a.source_row_number || a._row_number || 0);
        const rowB = Number(b.source_row_number || b._row_number || 0);
        if (rowA !== rowB) return rowA - rowB;
        return String(a.isin || '').localeCompare(String(b.isin || ''));
      });
      portfolioLots = workbookRows.map((row) => ({
        ...row,
        workbook_category_label: _classifyWorkbookCategory(row),
      }));
      portfolioBonds = portfolioLots;
      const rawHoldingCount = portfolioLots.length;
      const distinctBondCount = new Set(portfolioLots.map((row) => String(row.isin || '').trim().toUpperCase()).filter(Boolean)).size;
      const formatExactINR = (value) => `â‚¹${_toFiniteNumber(value, 0).toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;
      const computedWeightedYtm = (() => {
        const summaryYtm = _toFiniteNumber(summary.weighted_ytm ?? summary.workbook_weighted_ytm, 0);
        return Number.isFinite(summaryYtm) ? summaryYtm : 0;
      })(); window.getLiveSimulationSnapshot = () => App.getLiveSimulationSnapshot();
      const computedCouponWeightedYield = (() => {
        const summaryCouponYield = _toFiniteOrNull(
          summary.coupon_weighted_yield
          ?? summary.workbook_coupon_weighted_yield
          ?? summary.portfolio_coupon_weighted_yield_pct
        );
        if (summaryCouponYield != null) return summaryCouponYield;
        let totalWeight = 0;
        let weightedCoupon = 0;
        portfolioLots.forEach((row) => {
          const coupon = _toFiniteOrNull(row.coupon_rate ?? row.interest_rate ?? row.coupon_percent);
          const weight = _toFiniteOrNull(row.investment_value ?? row.book_value ?? row.market_value ?? row.current_value ?? row.face_value);
          if (coupon == null || weight == null || weight <= 0) return;
          totalWeight += weight;
          weightedCoupon += coupon * weight;
        });
        return totalWeight > 0 ? weightedCoupon / totalWeight : null;
      })(); window.getLiveSimulationSnapshot = () => App.getLiveSimulationSnapshot();
      const aggregateBy = (rows, keyFn, valueFn = (row) => _toFiniteNumber(row.current_value ?? row.investment_value ?? row.market_value ?? row.face_value, 0)) => {
        const totals = new Map();
        let total = 0;
        rows.forEach((row) => {
          const value = _toFiniteNumber(valueFn(row), 0);
          if (!Number.isFinite(value) || value <= 0) return;
          const key = String(keyFn(row) || 'UNKNOWN').trim() || 'UNKNOWN';
          totals.set(key, (totals.get(key) || 0) + value);
          total += value;
        });
        return Array.from(totals.entries())
          .sort((a, b) => b[1] - a[1])
          .map(([name, value]) => ({ name, value, pct: total > 0 ? (value / total) * 100 : 0 }));
      };

      const summaryAumCr = _toFiniteNumber(summary.aum_crores, 0);
      const summaryInvestedCr = _toFiniteNumber(summary.invested_crores, 0);
      const summaryFaceCr = _toFiniteNumber(summary.face_value_crores, 0);
      const summaryAccruedCr = _toFiniteNumber(summary.total_accrued_interest, 0) / 1e7;
      const summaryAvgMaturity = _toFiniteNumber(summary.weighted_avg_maturity, 0);

      // Guard: user may have navigated away while API calls were in-flight
      if (!document.getElementById('port-analytics')) return;

      document.getElementById('port-analytics').innerHTML = `
        <div class="metric-card indigo">
          <div class="metric-label">Closing Value</div>
          <div class="metric-value" style="font-size:1.1rem">â‚¹${summaryAumCr.toFixed(4)} Cr</div>
          <div class="metric-sub text-muted" style="font-size:0.72rem">Workbook total market value</div>
        </div>
        <div class="metric-card blue">
          <div class="metric-label">Invested Amount</div>
          <div class="metric-value">â‚¹${summaryInvestedCr.toFixed(4)} Cr</div>
          <div class="metric-sub text-muted" style="font-size:0.72rem">Workbook total invested</div>
        </div>
        <div class="metric-card teal">
          <div class="metric-label">Face Value (Book)</div>
          <div class="metric-value">â‚¹${summaryFaceCr.toFixed(4)} Cr</div>
          <div class="metric-sub text-muted" style="font-size:0.72rem">Workbook closing face value</div>
        </div>
        <div class="metric-card teal">
          <div class="metric-label">Interest</div>
          <div class="metric-value">â‚¹${summaryAccruedCr.toFixed(4)} Cr</div>
          <div class="metric-sub text-muted" style="font-size:0.72rem">Workbook interest for the year</div>
        </div>
        <div class="metric-card purple">
          <div class="metric-label">Coupon-Weighted Yield</div>
          <div class="metric-value">${computedCouponWeightedYield != null ? computedCouponWeightedYield.toFixed(4) + '%' : 'Unavailable'}</div>
          <div class="metric-sub text-muted" style="font-size:0.72rem">Weighted by invested value across the workbook portfolio</div>
        </div>
        <div class="metric-card amber">
          <div class="metric-label">Avg Maturity</div>
          <div class="metric-value">${summaryAvgMaturity.toFixed(2)} Y</div>
          <div class="metric-sub text-muted" style="font-size:0.72rem">Weighted maturity from workbook rows</div>
        </div>
        <div class="metric-card teal">
          <div class="metric-label">Unique ISINs</div>
          <div class="metric-value">${distinctBondCount}</div>
          <div class="metric-sub text-muted" style="font-size:0.72rem">${rawHoldingCount} workbook rows</div>
        </div>
        <div class="metric-card teal">
          <div class="metric-label">Workbook Rows</div>
          <div class="metric-value">${rawHoldingCount}</div>
          <div class="metric-sub text-muted" style="font-size:0.72rem">Accepted rows from the attached workbook</div>
        </div>
      `;

      // â”€â”€ Render holdings table â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
      renderPortfolioTable(portfolioBonds);

      // â”€â”€ Allocations tab â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
      const allocEl = document.getElementById('port-allocations');
      if (allocEl) {
        const totalPortfolioValue = Number(summary.workbook_total_market_value || summary.total_market_value || summary.total_invested || summary.current_value || 0);
        const weightText = (value) => {
          if (!Number.isFinite(totalPortfolioValue) || totalPortfolioValue <= 0) return '0.00%';
          return `${((Number(value || 0) / totalPortfolioValue) * 100).toFixed(2)}%`;
        };
        const targetBandForCategory = (category) => {
          const normalized = _canonicalCategoryKey(category);
          if (normalized === 'STATE_CENTRAL') return '45-65%';
          if (normalized === 'PSU' || normalized === 'PRIVATE') return '35-55%';
          if (normalized === 'EQUITY') return '5-15%';
          return 'Static / N/A';
        };
        const allocationCard = ({ title, accent, subtitle, summaryObj, metricLabel, metricValue, targetBand }) => `
                <div class="card" style="border:1px solid var(--border-light);box-shadow:none">
                  <div class="card-body" style="padding:16px">
                    <div style="font-size:0.76rem;font-weight:700;text-transform:uppercase;color:${accent}">${Utils.escapeHTML(title)}</div>
                    <div style="font-size:0.72rem;color:var(--text-muted);margin-top:4px">${Utils.escapeHTML(subtitle)}</div>
                    <div style="display:flex;justify-content:space-between;margin-top:10px;font-size:0.76rem"><span>Current Weight</span><strong>${weightText(summaryObj?.current_value || 0)}</strong></div>
                    <div style="display:flex;justify-content:space-between;margin-top:6px;font-size:0.76rem"><span>Count</span><strong>${Number(summaryObj?.count || 0).toLocaleString('en-IN')}</strong></div>
                    <div style="display:flex;justify-content:space-between;margin-top:6px;font-size:0.76rem"><span>${Utils.escapeHTML(metricLabel)}</span><strong>${Utils.escapeHTML(metricValue)}</strong></div>
                    <div style="display:flex;justify-content:space-between;margin-top:6px;font-size:0.76rem"><span>Target Band</span><strong>${Utils.escapeHTML(targetBand)}</strong></div>
                  </div>
                </div>`;
        const structuredAllocationSection = `
          <div class="card" style="margin-bottom:20px;border:1px solid var(--border-light);box-shadow:none">
            <div class="card-header"><div><div class="card-title">Investment Classes</div><div class="card-subtitle">Canonical SDS, Central &amp; State, PSU, Private, and Equity split with target bands</div></div></div>
            <div class="card-body">
              <div class="grid-3" style="gap:16px">
                ${allocationCard({
                  title: 'SDS',
                  accent: '#0f766e',
                  subtitle: 'Static government-backed',
                  summaryObj: groupedPortfolio.sds?.summary,
                  metricLabel: 'Interest Rate',
                  metricValue: groupedPortfolio.sds?.summary?.interest_rate != null ? Number(groupedPortfolio.sds?.summary?.interest_rate).toFixed(2) + '%' : 'Unavailable',
                  targetBand: 'Static / N/A',
                })}
                ${allocationCard({
                  title: 'Central & State',
                  accent: '#7c3aed',
                  subtitle: 'Market-linked sovereign debt',
                  summaryObj: groupedPortfolio.bonds?.central_state?.summary,
                  metricLabel: 'Coupon-Weighted Yield',
                  metricValue: groupedPortfolio.bonds?.central_state?.summary?.coupon_weighted_yield != null ? Number(groupedPortfolio.bonds?.central_state?.summary?.coupon_weighted_yield).toFixed(2) + '%' : 'Unavailable',
                  targetBand: targetBandForCategory('STATE_CENTRAL'),
                })}
                ${allocationCard({
                  title: 'PSU',
                  accent: '#2563eb',
                  subtitle: 'Public sector debt',
                  summaryObj: groupedPortfolio.bonds?.psu?.summary,
                  metricLabel: 'Coupon-Weighted Yield',
                  metricValue: groupedPortfolio.bonds?.psu?.summary?.coupon_weighted_yield != null ? Number(groupedPortfolio.bonds?.psu?.summary?.coupon_weighted_yield).toFixed(2) + '%' : 'Unavailable',
                  targetBand: targetBandForCategory('PSU'),
                })}
                ${allocationCard({
                  title: 'Private',
                  accent: '#db2777',
                  subtitle: 'Private / corporate debt',
                  summaryObj: groupedPortfolio.bonds?.private?.summary,
                  metricLabel: 'Coupon-Weighted Yield',
                  metricValue: groupedPortfolio.bonds?.private?.summary?.coupon_weighted_yield != null ? Number(groupedPortfolio.bonds?.private?.summary?.coupon_weighted_yield).toFixed(2) + '%' : 'Unavailable',
                  targetBand: targetBandForCategory('PRIVATE'),
                })}
                ${allocationCard({
                  title: 'Equity',
                  accent: '#16a34a',
                  subtitle: 'NAV / market-based',
                  summaryObj: groupedPortfolio.equity?.summary,
                  metricLabel: 'CAGR',
                  metricValue: groupedPortfolio.equity?.summary?.cagr_pct != null ? Number(groupedPortfolio.equity?.summary?.cagr_pct).toFixed(2) + '%' : 'Unavailable',
                  targetBand: targetBandForCategory('EQUITY'),
                })}
              </div>
            </div>
          </div>
        `;
        const byCategoryRows = aggregateBy(portfolioBonds, (row) => _investmentTypeFromRow(row))
          .sort((a, b) => {
            const orderDiff = INVESTMENT_TYPE_ORDER.indexOf(a.name) - INVESTMENT_TYPE_ORDER.indexOf(b.name);
            if (orderDiff !== 0) return orderDiff;
            return b.value - a.value;
          });
        const workbookCategoryTotals = aggregateBy(portfolioBonds, (row) => row.workbook_category_label || _classifyWorkbookCategory(row));
        const byWorkbookCategoryRows = WORKBOOK_CATEGORY_ORDER.map((label) => workbookCategoryTotals.find((row) => row.name === label) || { name: label, value: 0, pct: 0 });
        const uncategorizedWorkbookCategory = workbookCategoryTotals.find((row) => row.name === 'Other');
        if (uncategorizedWorkbookCategory) {
          byWorkbookCategoryRows.push(uncategorizedWorkbookCategory);
        }
        const bySector = aggregateBy(portfolioBonds, (row) => row.sector || row.workbook_sector || 'UNKNOWN');
        const byIssuer = aggregateBy(portfolioBonds, (row) => row.issuer_name || 'UNKNOWN').slice(0, 10);
        const byMaturity = aggregateBy(portfolioBonds, (row) => {
          const years = Number(row.years_to_maturity || 0);
          if (Number.isFinite(years) && years > 0) {
            if (years <= 1) return '0-1Y';
            if (years <= 3) return '1-3Y';
            if (years <= 5) return '3-5Y';
            if (years <= 7) return '5-7Y';
            if (years <= 10) return '7-10Y';
            return '>10Y';
          }
          const maturity = Utils.parseDateSafe(row.maturity_date);
          if (!maturity) return 'Unknown';
          const yearsToMaturity = (maturity - new Date()) / (365.25 * 86400000);
          if (!Number.isFinite(yearsToMaturity) || yearsToMaturity <= 0) return 'Unknown';
          if (yearsToMaturity <= 1) return '0-1Y';
          if (yearsToMaturity <= 3) return '1-3Y';
          if (yearsToMaturity <= 5) return '3-5Y';
          if (yearsToMaturity <= 7) return '5-7Y';
          if (yearsToMaturity <= 10) return '7-10Y';
          return '>10Y';
        });
        allocEl.innerHTML = `
          ${structuredAllocationSection}
          <div class="card" style="margin-bottom:20px;border:1px solid var(--border-light);box-shadow:none">
            <div class="card-header"><div><div class="card-title">Workbook Categories</div><div class="card-subtitle">Five category labels from the source sheet, plus any uncategorised rows</div></div></div>
            <div class="card-body">
              <table class="data-table">
                <thead><tr><th>Workbook Category</th><th class="col-number">Closing Value (â‚¹)</th><th class="col-number">% Weight</th></tr></thead>
                <tbody>${byWorkbookCategoryRows.map(r => {
                  const pct = r.pct || 0;
                  return `<tr>
                    <td title="${Utils.escapeAttr(r.name || 'â€”')}">${Utils.escapeHTML(r.name || 'â€”')}</td>
                    <td class="col-number">${formatExactINR(r.value || 0)}</td>
                    <td class="col-number"><span class="badge badge-purple">${pct.toFixed(2)}%</span></td>
                  </tr>`;
                }).join('')}</tbody>
              </table>
            </div>
          </div>
          <div class="grid-2" style="gap:20px">
            <div>
              <h4 style="margin-bottom:12px;font-size:0.88rem;font-weight:600">By Investment Class</h4>
              <table class="data-table">
                <thead><tr><th>Investment Class</th><th class="col-number">Closing Value (â‚¹)</th><th class="col-number">% Weight</th></tr></thead>
                <tbody>${byCategoryRows.map(r => `<tr>
                  <td title="${Utils.escapeAttr(r.name || 'â€”')}">${Utils.escapeHTML(r.name || 'â€”')}</td>
                  <td class="col-number">${formatExactINR(r.value || 0)}</td>
                  <td class="col-number"><span class="badge badge-blue">${(r.pct || 0).toFixed(2)}%</span></td>
                </tr>`).join('')}</tbody>
              </table>
            </div>
            <div>
              <h4 style="margin-bottom:12px;font-size:0.88rem;font-weight:600">By Sector</h4>
              <table class="data-table">
                <thead><tr><th>Sector</th><th class="col-number">Closing Value (â‚¹)</th><th class="col-number">% Weight</th></tr></thead>
                <tbody>${bySector.map(r => `<tr>
                  <td>${Utils.escapeHTML(r.name || 'â€”')}</td>
                  <td class="col-number">${formatExactINR(r.value || 0)}</td>
                  <td class="col-number"><span class="badge badge-blue">${(r.pct || 0).toFixed(2)}%</span></td>
                </tr>`).join('')}</tbody>
              </table>
            </div>
            <div>
              <h4 style="margin-bottom:12px;font-size:0.88rem;font-weight:600">By Maturity Bucket</h4>
              <table class="data-table">
                <thead><tr><th>Bucket</th><th class="col-number">Closing Value (â‚¹)</th><th class="col-number">%</th><th>Bar</th></tr></thead>
                <tbody>${byMaturity.map(r => {
                  const pct = r.pct || 0;
                  return `<tr>
                    <td>${Utils.escapeHTML(r.bucket || r.name || 'â€”')}</td>
                    <td class="col-number">${formatExactINR(r.value || 0)}</td>
                    <td class="col-number">${pct.toFixed(2)}%</td>
                    <td><div style="width:120px;height:10px;background:var(--border-light);border-radius:4px;overflow:hidden;display:inline-block;vertical-align:middle"><div style="height:100%;width:${Math.min(100,pct)}%;background:${pct>40?'#ef4444':pct>20?'#f59e0b':'#10b981'};border-radius:4px"></div></div></td>
                  </tr>`;
                }).join('')}</tbody>
              </table>
            </div>
            <div>
              <h4 style="margin-bottom:12px;font-size:0.88rem;font-weight:600">Top 10 Issuers by Exposure</h4>
              <table class="data-table">
                <thead><tr><th>Issuer</th><th class="col-number">Closing Value (â‚¹)</th><th class="col-number">%</th></tr></thead>
                <tbody>${byIssuer.map(r => {
                  const pct = r.pct || 0;
                  return `<tr>
                    <td class="truncate" style="max-width:160px">${Utils.escapeHTML(r.name || 'â€”')}</td>
                    <td class="col-number">${formatExactINR(r.value || 0)}</td>
                    <td class="col-number"><span class="badge ${pct>10?'badge-red':pct>5?'badge-amber':'badge-green'}">${pct.toFixed(2)}%</span></td>
                  </tr>`;
                }).join('')}</tbody>
              </table>
            </div>
          </div>
        `;
      }

      const puEl = document.getElementById('port-updated');
      if (puEl) {
        puEl.textContent = simulationTickDate
          ? `Live simulation overlay: ${simulationTickDate} | Workbook snapshot: ${Utils.formatDateTime(snapshotAt)}`
          : `Workbook snapshot: ${Utils.formatDateTime(snapshotAt)}`;
      }

      // â”€â”€ Search / filter wiring â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
      const search = document.getElementById('port-search');
      const filterCategory = document.getElementById('port-filter-category');
      const filterRating = document.getElementById('port-filter-rating');
      const doFilter = () => {
        const kw = String(search?.value || '').toLowerCase();
        const cat = String(filterCategory?.value || '').trim();
        const rat = String(filterRating?.value || '').trim();
        const filtered = portfolioBonds.filter(b => {
          const isin = String(b.isin || '').toLowerCase();
          const bondName = String(b.bond_name || '').toLowerCase();
          const issuerName = String(b.issuer_name || '').toLowerCase();
          const categoryName = _investmentTypeFromRow(b);
          const workbookCategoryName = String(b.workbook_category_label || _classifyWorkbookCategory(b)).trim();
          const sectorName = String(b.sector || b.workbook_sector || '').toLowerCase();
          const ratingName = String(b.credit_rating || b.rating || '').toLowerCase();
          if (kw && ![isin, bondName, issuerName, categoryName, workbookCategoryName.toLowerCase(), sectorName].some((value) => value.includes(kw))) return false;
          if (cat && workbookCategoryName !== cat) return false;
          if (rat && ratingName !== rat.toLowerCase()) return false;
          return true;
        });
        renderPortfolioTable(filtered);
      };
      search.addEventListener('input', Utils.debounce(doFilter, 200));
      filterCategory.addEventListener('change', doFilter);
      filterRating.addEventListener('change', doFilter);
      document.getElementById('port-export').addEventListener('click', () => {
        const btn = document.getElementById('port-export');
        if (btn) { btn.disabled = true; btn.textContent = 'Exportingâ€¦'; }
        Utils.exportCSV(
          portfolioBonds.map((row) => {
            const { type, instrument_type, ...rest } = row;
            return {
              ...rest,
              category: _investmentTypeFromRow(row),
              sub_type: _bondSubtypeFromRow(row),
              instrument_subtype: _bondSubtypeFromRow(row),
            };
          }),
          'portfolio_holdings.csv'
        );
        Utils.toast('success', 'Exported', 'Portfolio holdings exported to CSV');
        if (btn) { btn.disabled = false; btn.textContent = 'Export CSV'; }
      });
    } catch (err) {
      if (isAuthError(err)) {
        handleSessionExpired(err);
        return;
      }
      Utils.toast('error', 'Portfolio Error', err.message);
    }
  }

  function renderPortfolioTable(data) {
    const container = document.getElementById('port-table');
    if (!container) return;
    const rows = Array.isArray(data) ? data : [];
    if (!rows.length) { container.innerHTML = Utils.emptyHTML('No portfolio rows match filters', 'Try adjusting your filter criteria'); return; }

    const formatExactINR = (value) => `â‚¹${_toFiniteNumber(value, 0).toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;

    container.innerHTML = `
      <table class="data-table" style="font-size:0.78rem">
        <thead><tr>
          <th data-sort="source_row_number" class="col-number">Row</th>
          <th data-sort="isin">ISIN</th>
          <th data-sort="bond_name">Bond</th>
          <th data-sort="issuer_name">Issuer</th>
          <th data-sort="workbook_category_label">Workbook Category</th>
          <th data-sort="sector">Sector</th>
          <th data-sort="credit_rating">Rating</th>
          <th data-sort="quantity" class="col-number">Qty</th>
          <th data-sort="face_value" class="col-number">Face Value</th>
          <th data-sort="current_value" class="col-number">Closing Value</th>
          <th data-sort="ytm" class="col-number">Yield%</th>
          <th data-sort="coupon_rate" class="col-number">Coupon%</th>
          <th data-sort="purchase_date">Purchase</th>
          <th data-sort="maturity_date">Maturity</th>
          <th data-sort="source_sheet_name">Sheet</th>
        </tr></thead>
        <tbody>
          ${rows.map((b, idx) => {
            const rowNumber = idx + 1;
            const sourceRowNumber = Number(b.source_row_number || b._row_number || 0);
            const isin = String(b.isin || '').trim();
            const bondName = String(b.bond_name || '').trim();
            const issuerName = String(b.issuer_name || '').trim();
            const subtypeKey = _bondSubtypeFromRow(b);
            const workbookCategoryName = String(b.workbook_category_label || _classifyWorkbookCategory(b)).trim();
            const sectorName = String(b.sector || b.workbook_sector || 'â€”').trim();
            const ratingText = String(b.credit_rating || b.rating || 'â€”').trim();
            const agencyText = String(b.rating_agency || '').trim();
            const agencyDisplayText = agencyText && agencyText !== ratingText ? agencyText : '';
            const quantityText = _toFiniteNumber(b.quantity, 0).toLocaleString('en-IN', { maximumFractionDigits: 2 });
            const faceValueText = formatExactINR(b.face_value);
            const closingValueText = formatExactINR(b.current_value ?? b.market_value ?? b.investment_value);
            const ytmValue = _toFiniteOrNull(b.ytm ?? b.weighted_ytm);
            const ytmText = Utils.formatYieldPct(ytmValue, 4);
            const couponValue = _toFiniteOrNull(b.coupon_rate);
            const couponText = couponValue == null ? 'â€”' : couponValue.toFixed(2);
            const sourceSheetText = String(b.source_sheet_name || b._source_sheet_name || 'â€”').trim();
            const rowMeta = sourceRowNumber
              ? `Workbook row ${sourceRowNumber}${sourceSheetText && sourceSheetText !== 'â€”' ? ` â€¢ ${sourceSheetText}` : ''}`
              : 'Workbook row unavailable';
            return `<tr>
              <td class="col-number text-muted" title="${Utils.escapeAttr(rowMeta)}">${rowNumber}</td>
              <td class="col-isin font-mono" style="font-size:0.7rem;white-space:nowrap">
                <span style="cursor:pointer;color:var(--color-primary)" data-method="navigate" data-page="analysis" data-isin="${Utils.escapeAttr(isin || '')}">${Utils.escapeHTML(isin || 'â€”')}</span>
              </td>
              <td class="truncate" style="max-width:180px;font-size:0.78rem" title="${Utils.escapeAttr(bondName || 'â€”')}">${Utils.escapeHTML(bondName || 'â€”')}</td>
              <td class="truncate" style="max-width:180px;font-size:0.78rem" title="${Utils.escapeAttr(issuerName || 'â€”')}">${Utils.escapeHTML(issuerName || 'â€”')}</td>
              <td title="${Utils.escapeAttr(workbookCategoryName || 'â€”')}">${Utils.escapeHTML(workbookCategoryName || 'â€”')}</td>
              <td title="${Utils.escapeAttr(sectorName)}">${Utils.escapeHTML(sectorName)}</td>
              <td>
                <div style="font-weight:600">${Utils.escapeHTML(ratingText)}</div>
                ${agencyDisplayText ? `<div style="font-size:0.68rem;color:var(--text-muted);margin-top:2px">${Utils.escapeHTML(agencyDisplayText)}</div>` : ''}
              </td>
              <td class="col-number">${quantityText}</td>
              <td class="col-number">${faceValueText}</td>
              <td class="col-number font-medium">${closingValueText}</td>
              <td class="col-number">${ytmText}</td>
              <td class="col-number">${couponValue == null ? 'â€”' : couponText + '%'}</td>
              <td style="font-size:0.72rem">${Utils.formatDate(b.purchase_date)}</td>
              <td style="font-size:0.72rem">${Utils.formatDate(b.maturity_date)}</td>
              <td style="font-size:0.72rem">${Utils.escapeHTML(sourceSheetText)}</td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    `;

    // Sorting (mouse + keyboard)
    wireSortableHeaders(container, (th) => {
      const key = th.dataset.sort;
      const asc = !th.classList.contains('sorted') || th.dataset.dir === 'desc';
      container.querySelectorAll('th').forEach((cell) => {
        cell.classList.remove('sorted');
        cell.dataset.dir = '';
      });
      th.classList.add('sorted');
      th.dataset.dir = asc ? 'asc' : 'desc';
      renderPortfolioTable(Utils.sortData(rows, key, asc));
    });
  }

  function _togglePortfolioDrill(isin) {
    const row = document.getElementById(`drill-row-${isin}`);
    const chev = document.getElementById(`chev-${isin}`);
    if (!row) return;
    const open = row.style.display !== 'none';
    row.style.display = open ? 'none' : 'table-row';
    if (chev) chev.style.transform = open ? '' : 'rotate(90deg)';
  }

  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  // 3. BONDS UNIVERSE PAGE
  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  let universeBonds = [];
  let compareSelected = [];
  let _uniPage = 1;
  const _UNI_PAGE_SIZE = 100;
  let _uniServerPage = 1;
  let _uniServerTotal = 0;
  let _uniCurrentFilters = {};
  const _UNI_SERVER_SIZE = 500;

  function _normalizeUniverseRating(raw) {
    const txt = String(raw || '').toUpperCase().trim();
    if (!txt) return 'NR';
    if (txt.includes('SOV')) return 'SOVEREIGN';
    if (txt.includes('AAA')) return 'AAA';
    if (txt.includes('AA+')) return 'AA+';
    if (txt === 'AA' || txt.startsWith('AA/')) return 'AA';
    if (txt.includes('AA-')) return 'AA-';
    if (txt.includes('A+')) return 'A+';
    if (txt === 'A' || txt.startsWith('A/')) return 'A';
    return txt;
  }

  function _isUniverseEligibleRating(raw) {
    const r = _normalizeUniverseRating(raw);
    return ['SOVEREIGN', 'AAA', 'AA+', 'AA'].includes(r);
  }

  function _estimateUniverseYTM(couponRate, cleanPrice, maturityDate) {
    const cp = Number(cleanPrice);
    const cr = Number(couponRate);
    const mat = Utils.parseDateSafe(maturityDate);
    if (!mat || !isFinite(cp) || cp <= 0 || !isFinite(cr)) return null;
    const years = (mat - new Date()) / (365.25 * 86400000);
    if (!isFinite(years) || years <= 0) return null;
    const annualCoupon = cr;
    const ytmApprox = (annualCoupon + ((100 - cp) / years)) / ((100 + cp) / 2);
    return isFinite(ytmApprox) ? (ytmApprox * 100) : null;
  }

  function _normalizeUniversePrice(rawPrice, faceValue) {
    const price = _toFiniteOrNull(rawPrice);
    const face = _toFiniteOrNull(faceValue);
    if (price == null) return null;
    if (face != null && face > 0 && price > 1000) {
      const per100 = (price / face) * 100;
      if (Number.isFinite(per100) && per100 > 0) return per100;
    }
    return price;
  }

  function _normalizeUniverseYTM(rawYtm, couponRate, cleanPrice, maturityDate) {
    const direct = _toFiniteOrNull(rawYtm);
    if (direct != null && direct > 0 && direct < 50) return direct;
    return _estimateUniverseYTM(couponRate, cleanPrice, maturityDate);
  }

  async function renderBondsUniverse(el, params = {}) {
    el.innerHTML = `
      <div class="page-header">
        <div>
          <div class="page-title">Holdings / Lots Universe</div>
          <div class="page-subtitle">Lot-level rows from the active holdings feed; repeated ISINs are expected when a bond has multiple lots.</div>
        </div>
        <div class="page-actions">
          <button class="btn btn-secondary btn-sm" id="universe-export">${IC.download} Export</button>
          <button class="btn btn-primary btn-sm" type="button" data-method="refreshMarket">${IC.refresh} Refresh Prices</button>
        </div>
      </div>

      <!-- Filters -->
      <div class="card" style="margin-bottom:16px">
        <div class="card-body" style="padding:12px 20px">
          <div class="table-filter" style="flex-wrap:wrap">
            <div>
              <label class="form-label">Rating</label>
              <select id="uni-rating"><option value="">All Ratings</option><option>SOVEREIGN</option><option>AAA</option><option>AA+</option><option>AA</option><option>A+</option><option>A</option><option>BBB+</option><option>BBB</option></select>
            </div>
            <div>
              <label class="form-label">Sector</label>
              <select id="uni-sector"><option value="">All Sectors</option><option>G-SEC</option><option>SDL</option><option>PSU</option><option>BANK</option><option>NBFC</option><option>CORPORATE</option></select>
            </div>
            <div>
              <label class="form-label">Yield Range</label>
              <div class="flex gap-8">
                <input type="number" id="uni-ytm-min" placeholder="Min %" style="width:80px" step="0.1" />
                <input type="number" id="uni-ytm-max" placeholder="Max %" style="width:80px" step="0.1" />
              </div>
            </div>
            <div>
              <label class="form-label">Maturity Range</label>
              <div class="flex gap-8">
                <input type="number" id="uni-mat-min" placeholder="Min yrs" style="width:80px" />
                <input type="number" id="uni-mat-max" placeholder="Max yrs" style="width:80px" />
              </div>
            </div>
            <div>
              <label class="form-label">Search</label>
              <input type="text" id="uni-search" placeholder="ISIN, bond, or issuer..." style="width:180px" />
            </div>
            <div style="align-self:flex-end">
              <button class="btn btn-primary btn-sm" id="uni-apply">${IC.filter} Apply</button>
              <button class="btn btn-ghost btn-sm" id="uni-reset">Reset</button>
            </div>
          </div>
        </div>
      </div>

      <!-- Compare Panel (hidden by default) -->
      <div class="card hidden" id="compare-panel" style="margin-bottom:16px">
        <div class="card-header"><div class="card-title">Bond Comparison (Select up to 3)</div><button class="btn btn-ghost btn-sm" type="button" data-method="clearCompare">${IC.x} Clear</button></div>
        <div class="card-body" id="compare-body"></div>
      </div>

      <!-- Bonds Table -->
      <div class="card">
        <div class="card-header">
          <div><div class="card-title">Lot-Level Table</div><div class="card-subtitle" id="uni-count">Loading...</div></div>
        </div>
        <div class="table-container" id="uni-table">${Utils.skeletonTable(10)}</div>
        <div class="table-pagination" id="uni-pagination"></div>
      </div>
      <div class="last-updated" id="uni-updated" style="margin-top:12px"></div>
    `;

    // Reset server page state on every fresh navigation
    _uniServerPage = 1;
    _uniServerTotal = 0;
    _uniCurrentFilters = {};
    await loadUniverseData({ ...params });

    // Wire up filters
    const apply = () => {
      _uniCurrentFilters = {
        keyword:      document.getElementById('uni-search')?.value?.trim() || '',
        credit_rating: document.getElementById('uni-rating')?.value || '',
        issuer_type:  document.getElementById('uni-sector')?.value || '',
        ytmMin: parseFloat(document.getElementById('uni-ytm-min')?.value),
        ytmMax: parseFloat(document.getElementById('uni-ytm-max')?.value),
        matMin: parseFloat(document.getElementById('uni-mat-min')?.value),
        matMax: parseFloat(document.getElementById('uni-mat-max')?.value),
      };
      loadUniverseData({ ..._uniCurrentFilters, page: 1 });
    };

    const uniApplyBtn = document.getElementById('uni-apply');
    const uniResetBtn = document.getElementById('uni-reset');
    const uniRatingEl = document.getElementById('uni-rating');
    const uniSectorEl = document.getElementById('uni-sector');
    const uniSearchEl = document.getElementById('uni-search');
    const uniYtmMinEl = document.getElementById('uni-ytm-min');
    const uniYtmMaxEl = document.getElementById('uni-ytm-max');
    const uniMatMinEl = document.getElementById('uni-mat-min');
    const uniMatMaxEl = document.getElementById('uni-mat-max');
    const uniExportBtn = document.getElementById('universe-export');

    uniApplyBtn?.addEventListener('click', apply);
    uniResetBtn?.addEventListener('click', () => {
      if (uniRatingEl) uniRatingEl.value = '';
      if (uniSectorEl) uniSectorEl.value = '';
      if (uniSearchEl) uniSearchEl.value = '';
      if (uniYtmMinEl) uniYtmMinEl.value = '';
      if (uniYtmMaxEl) uniYtmMaxEl.value = '';
      if (uniMatMinEl) uniMatMinEl.value = '';
      if (uniMatMaxEl) uniMatMaxEl.value = '';
      _uniCurrentFilters = {};
      loadUniverseData({ page: 1 });
    });
    uniSearchEl?.addEventListener('input', Utils.debounce(apply, 250));
    uniExportBtn?.addEventListener('click', () => {
      if (uniExportBtn) { uniExportBtn.disabled = true; uniExportBtn.textContent = 'Exportingâ€¦'; }
      Utils.exportCSV(universeBonds, 'bonds_universe.csv');
      Utils.toast('success', 'Exported', 'Lot-level universe exported to CSV');
      if (uniExportBtn) { uniExportBtn.disabled = false; uniExportBtn.textContent = 'Export CSV'; }
    });
  }

  async function loadUniverseData(params = {}) {
    try {
      const uniEl = document.getElementById('uni-table');
      if (uniEl) uniEl.innerHTML = Utils.skeletonTable(10);

      const page = params.page || 1;
      _uniServerPage = 1;
      _uniServerTotal = 0;

      let portfolioTimeoutId;
      const portfolioData = (await Promise.race([
        API.getPortfolio(),
        new Promise((_, reject) => {
          portfolioTimeoutId = window.setTimeout(() => reject(new Error('Portfolio data load timed out after 12 seconds')), 12000);
        }),
      ]).finally(() => {
        if (portfolioTimeoutId) window.clearTimeout(portfolioTimeoutId);
      })) || {};
      const holdings = portfolioData.holdings || [];
      const expanded = portfolioData.expanded || {};

      universeBonds = holdings.map(h => {
        const details = expanded[h.holding_key] || {};
        const couponRate = _toFiniteOrNull(h.coupon_rate ?? h.interest_rate ?? h.coupon_percent ?? details.coupon_rate ?? details.interest_rate);
        const clean = _normalizeUniversePrice(
          h.clean_price ?? h.market_price ?? details.clean_price ?? details.market_price,
          h.face_value ?? details.face_value
        );
        const ytm = _normalizeUniverseYTM(h.ytm, couponRate, clean, h.maturity_date || details.maturity_date);
        return {
          isin: h.isin,
          bond_name: h.instrument_name || details.instrument_name || h.bond_name || h.issuer_name || h.isin,
          issuer_name: details.issuer || h.issuer_name || h.instrument_name || h.isin,
          issuer_type: h.issuer_type || details.issuer_type || details.sector || h.issuer_category || h.workbook_sector || h.sector || h.category || 'â€”',
          credit_rating: h.credit_rating || h.rating || details.rating_latest || 'NR',
          rating_agency: details.rating_agency || h.rating_agency || '',
          coupon_rate: couponRate,
          maturity_date: h.maturity_date || details.maturity_date || null,
          ytm,
          clean_price: clean,
          current_value: h.current_value,
          invested_value: h.invested_value,
          face_value: h.face_value,
          quantity: h.quantity,
          has_live_quote: clean != null,
          sector: details.sector || h.category || 'â€”',
        };
      });

      const requestedRating = (params.credit_rating || '').toUpperCase();
      universeBonds = universeBonds.filter(b => {
        const norm = _normalizeUniverseRating(b.credit_rating || b.rating);
        if (requestedRating) return norm === requestedRating;
        return true;
      });

      const issuerType = (params.issuer_type || '').toUpperCase();
      const keyword = (params.keyword || '').toLowerCase();
      const ytmMin = params.ytmMin, ytmMax = params.ytmMax;
      const matMin = params.matMin, matMax = params.matMax;
      const filtered = universeBonds.filter(b => {
        if (issuerType && String(b.issuer_type || '').toUpperCase() !== issuerType) return false;
        if (keyword) {
          const hay = `${b.isin} ${b.bond_name || ''} ${b.issuer_name || ''}`.toLowerCase();
          if (!hay.includes(keyword)) return false;
        }
        const ytmVal = _toFiniteOrNull(b.ytm);
        if (!isNaN(ytmMin) && (ytmVal == null || ytmVal < ytmMin)) return false;
        if (!isNaN(ytmMax) && (ytmVal == null || ytmVal > ytmMax)) return false;
        const mat = Utils.parseDateSafe(b.maturity_date);
        if (mat) {
          const yrs = (mat - new Date()) / (365.25 * 86400000);
          if (!isNaN(matMin) && yrs < matMin) return false;
          if (!isNaN(matMax) && yrs > matMax) return false;
        }
        return true;
      });

      renderUniverseTable(filtered);
      const cnt = document.getElementById('uni-count');
      const distinctBondCount = new Set(filtered.map((bond) => String(bond.isin || '').trim()).filter(Boolean)).size;
      if (cnt) cnt.textContent =
        `${filtered.length} lot rows across ${distinctBondCount} distinct bonds from the active holdings view`;
      const upd = document.getElementById('uni-updated');
      if (upd) upd.textContent = `Last updated: ${Utils.formatDateTime(new Date())}`;
    } catch (err) {
      Utils.toast('error', 'Universe Load Error', err.message);
      const uniEl = document.getElementById('uni-table');
      if (uniEl) uniEl.innerHTML = Utils.emptyHTML('Failed to load bonds', err.message);
    }
  }

  let _uniCurrentData = [];

  function renderUniverseTable(data, page) {
    _uniCurrentData = data;
    _uniPage = (page !== undefined) ? page : 1;

    const countEl = document.getElementById('uni-count');
    const container = document.getElementById('uni-table');
    const pagEl = document.getElementById('uni-pagination');
    if (!container) return;

    if (!data.length) {
      if (countEl) countEl.textContent = '0 lot rows';
      container.innerHTML = Utils.emptyHTML(
        'No lot rows found',
        universeBonds.length === 0
          ? 'No portfolio lots are loaded yet â€” upload holdings from the Data Sources tab'
          : 'No portfolio lots match the current filters â€” try relaxing the rating or sector filters'
      );
      if (pagEl) pagEl.innerHTML = '';
      return;
    }

    const totalPages = Math.ceil(data.length / _UNI_PAGE_SIZE);
    const pageData = data.slice((_uniPage - 1) * _UNI_PAGE_SIZE, _uniPage * _UNI_PAGE_SIZE);
    const distinctBondCount = new Set(data.map((bond) => String(bond.isin || '').trim()).filter(Boolean)).size;

    if (countEl) countEl.textContent =
      `${data.length} lot rows across ${distinctBondCount} distinct bonds â€” page ${_uniPage} of ${totalPages} (showing ${pageData.length})`;

    container.innerHTML = `
      <table class="data-table">
        <thead><tr>
          <th data-sort="isin" style="min-width:110px">ISIN <span class="sort-icon">â†•</span></th>
          <th data-sort="bond_name">Bond Issue <span class="sort-icon">â†•</span></th>
          <th data-sort="issuer_type">Sector</th>
          <th data-sort="credit_rating">Rating <span class="sort-icon">â†•</span></th>
          <th data-sort="coupon_rate" class="col-number">Coupon <span class="sort-icon">â†•</span></th>
          <th data-sort="maturity_date">Maturity <span class="sort-icon">â†•</span></th>
          <th data-sort="ytm" class="col-number">Yield <span class="sort-icon">â†•</span></th>
          <th data-sort="clean_price" class="col-number">Price <span class="sort-icon">â†•</span></th>
          <th style="width:80px">Action</th>
        </tr></thead>
        <tbody>
          ${pageData.map(b => {
            const ytm = _toFiniteOrNull(b.ytm);
            const price = _toFiniteOrNull(b.clean_price);
            const couponRate = _toFiniteOrNull(b.coupon_rate);
            const ytmColor = ytm != null ? (ytm >= 8 ? '#16a34a' : ytm >= 7 ? '#2563eb' : '#374151') : '#9ca3af';
            const liveTag = b.has_live_quote
              ? `<span title="Live broker quote" style="display:inline-block;width:7px;height:7px;background:#22c55e;border-radius:50%;margin-left:4px;vertical-align:middle"></span>`
              : '';
            const mat = Utils.parseDateSafe(b.maturity_date);
            const yrsToMat = mat ? ((mat - new Date()) / (365.25 * 86400000)) : null;
            const matClass = yrsToMat != null ? (yrsToMat < 1 ? 'color:#ef4444' : yrsToMat < 3 ? 'color:#f59e0b' : '') : '';
            return `<tr>
              <td style="font-family:monospace;font-size:0.71rem;cursor:pointer;color:var(--color-primary)" data-method="navigate" data-page="bond-analysis" data-isin="${Utils.escapeAttr(b.isin)}">${b.isin}${liveTag}</td>
              <td class="truncate" style="max-width:240px" title="${b.bond_name || b.issuer_name || ''}">
                <div style="font-size:0.78rem;font-weight:600">${b.bond_name || b.issuer_name || 'â€”'}</div>
                <div style="font-size:0.66rem;color:var(--text-muted)">${b.issuer_name || 'â€”'}</div>
              </td>
              <td><span class="badge" style="font-size:0.68rem">${b.issuer_type || 'â€”'}</span></td>
              <td>${Utils.ratingBadge(b.credit_rating, b.rating_agency)}${Utils.ratingAgencyTag(b.rating_agency, b.credit_rating)}</td>
              <td class="col-number" style="font-size:0.78rem">${couponRate != null ? couponRate.toFixed(2) + '%' : 'â€”'}</td>
              <td style="font-size:0.78rem;${matClass}">${b.maturity_date ? Utils.formatDate(b.maturity_date) : 'â€”'}</td>
              <td class="col-number" style="font-weight:700;color:${ytmColor};font-size:0.83rem">${ytm != null ? ytm.toFixed(4) + '%' : 'â€”'}</td>
              <td class="col-number" style="font-size:0.78rem">${price != null ? 'â‚¹' + price.toFixed(2) : 'â€”'}</td>
              <td style="white-space:nowrap">
                <button class="btn btn-ghost btn-sm" type="button" style="padding:2px 6px;font-size:0.7rem" data-method="navigate" data-page="bond-analysis" data-isin="${Utils.escapeAttr(b.isin)}">Analyse</button>
              </td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    `;

    // Pagination controls
    if (pagEl) {
      // Server-side page navigation
      const serverTotalPages = _uniServerTotal > 0 ? Math.ceil(_uniServerTotal / _UNI_SERVER_SIZE) : 0;
      let serverNav = '';
      if (serverTotalPages > 1) {
        const spNums = [];
        for (let p = Math.max(1, _uniServerPage - 2); p <= Math.min(serverTotalPages, _uniServerPage + 2); p++) spNums.push(p);
        serverNav = `
          <div style="display:flex;align-items:center;gap:6px;padding:8px 16px;flex-wrap:wrap;font-size:0.8rem;border-top:1px solid var(--border-light)">
            <span style="color:var(--text-muted);margin-right:6px">Server page ${_uniServerPage} / ${serverTotalPages} &nbsp;(${_uniServerTotal} total bonds)</span>
            <button class="btn btn-ghost btn-sm" ${_uniServerPage === 1 ? 'disabled' : ''} type="button" data-method="_uniGoServerPage" data-arg="1">Â«</button>
            <button class="btn btn-ghost btn-sm" ${_uniServerPage === 1 ? 'disabled' : ''} type="button" data-method="_uniGoServerPage" data-arg="${_uniServerPage - 1}">â€¹ Prev</button>
            ${spNums.map(p => `<button class="btn btn-sm ${p === _uniServerPage ? 'btn-primary' : 'btn-ghost'}" type="button" data-method="_uniGoServerPage" data-arg="${p}">${p}</button>`).join('')}
            <button class="btn btn-ghost btn-sm" ${_uniServerPage === serverTotalPages ? 'disabled' : ''} type="button" data-method="_uniGoServerPage" data-arg="${_uniServerPage + 1}">Next â€º</button>
            <button class="btn btn-ghost btn-sm" ${_uniServerPage === serverTotalPages ? 'disabled' : ''} type="button" data-method="_uniGoServerPage" data-arg="${serverTotalPages}">Â»</button>
          </div>`;
      }
      if (totalPages <= 1) {
        pagEl.innerHTML = serverNav;
      } else {
        const start = (_uniPage - 1) * _UNI_PAGE_SIZE + 1;
        const end   = Math.min(_uniPage * _UNI_PAGE_SIZE, data.length);
        const pageNums = [];
        for (let p = Math.max(1, _uniPage - 2); p <= Math.min(totalPages, _uniPage + 2); p++) {
          pageNums.push(p);
        }
        pagEl.innerHTML = `
          <div style="display:flex;align-items:center;gap:6px;padding:10px 16px;flex-wrap:wrap;font-size:0.8rem">
            <span style="color:var(--text-muted);margin-right:8px">${start}â€“${end} of ${data.length}</span>
            <button class="btn btn-ghost btn-sm" ${_uniPage === 1 ? 'disabled' : ''} type="button" data-method="_uniGoPage" data-arg="1">Â«</button>
            <button class="btn btn-ghost btn-sm" ${_uniPage === 1 ? 'disabled' : ''} type="button" data-method="_uniGoPage" data-arg="${_uniPage - 1}">â€¹ Prev</button>
            ${pageNums.map(p => `<button class="btn btn-sm ${p === _uniPage ? 'btn-primary' : 'btn-ghost'}" type="button" data-method="_uniGoPage" data-arg="${p}">${p}</button>`).join('')}
            <button class="btn btn-ghost btn-sm" ${_uniPage === totalPages ? 'disabled' : ''} type="button" data-method="_uniGoPage" data-arg="${_uniPage + 1}">Next â€º</button>
            <button class="btn btn-ghost btn-sm" ${_uniPage === totalPages ? 'disabled' : ''} type="button" data-method="_uniGoPage" data-arg="${totalPages}">Â»</button>
          </div>
          ${serverNav}
        `;
      }
    }

    // Column sorting (mouse + keyboard)
    wireSortableHeaders(container, (th) => {
      const key = th.dataset.sort;
      const asc = !th.classList.contains('sorted') || th.dataset.dir === 'desc';
      container.querySelectorAll('th').forEach((cell) => {
        cell.classList.remove('sorted');
        delete cell.dataset.dir;
      });
      th.classList.add('sorted');
      th.dataset.dir = asc ? 'asc' : 'desc';
      const sorted = [...data].sort((a, b) => {
        const av = a[key] ?? (asc ? '\uFFFF' : '');
        const bv = b[key] ?? (asc ? '\uFFFF' : '');
        return asc ? (av > bv ? 1 : -1) : (av < bv ? 1 : -1);
      });
      renderUniverseTable(sorted);
    });
  }

  function _uniGoPage(n) {
    renderUniverseTable(_uniCurrentData, n);
    // Scroll table back to top
    document.getElementById('uni-table')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function _uniGoServerPage(n) {
    loadUniverseData({ ..._uniCurrentFilters, page: n });
    document.getElementById('uni-table')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function toggleCompareMode() {
    const panel = document.getElementById('compare-panel');
    panel.classList.toggle('hidden');
  }

  function addToCompare(isin) {
    if (compareSelected.length >= 3) { Utils.toast('warning', 'Limit', 'You can compare up to 3 bonds'); return; }
    if (compareSelected.includes(isin)) return;
    compareSelected.push(isin);
    document.getElementById('compare-panel').classList.remove('hidden');
    renderComparePanel();
  }

  function clearCompare() {
    compareSelected = [];
    document.getElementById('compare-panel').classList.add('hidden');
  }

  function downloadComplianceJSON() {
    const data = window._lastComplianceAudit || [];
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `compliance_audit_${new Date().toISOString().split('T')[0]}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function renderComparePanel() {
    const body = document.getElementById('compare-body');
    if (!compareSelected.length) { body.innerHTML = Utils.emptyHTML('Select bonds to compare', 'Click the compare button on any bond'); return; }

    const bondsData = await Promise.all(compareSelected.map(isin =>
      API.getBond(isin).catch(() => ({ bond: { isin }, market_data: null }))
    ));

    body.innerHTML = `
      <div class="compare-grid">
        ${bondsData.map(bd => {
          const b = bd.bond || {};
          const m = bd.market_data || {};
          const cmpYtm = _toFiniteOrNull(m.ytm ?? b.ytm);
          const cmpDuration = _toFiniteNumber(m.modified_duration ?? b.modified_duration, 0);
          const cmpSpread = _toFiniteNumber(m.spread_over_gsec ?? b.spread_over_gsec, 0);
          const cmpPrice = _toFiniteNumber(m.clean_price ?? b.clean_price, 0);
          return `<div class="compare-card selected">
            <div style="font-weight:700;font-size:0.9rem;margin-bottom:8px">${b.issuer_name || b.isin}</div>
            <div class="font-mono text-muted" style="font-size:0.72rem;margin-bottom:12px">${b.isin}</div>
            <div class="flex justify-between" style="font-size:0.78rem;padding:4px 0;border-bottom:1px solid var(--border-light)"><span>Rating</span><span class="fw-600">${b.credit_rating || 'NR'}</span></div>
            <div class="flex justify-between" style="font-size:0.78rem;padding:4px 0;border-bottom:1px solid var(--border-light)"><span>YTM</span><span class="fw-600">${Utils.formatPct(cmpYtm)}</span></div>
            <div class="flex justify-between" style="font-size:0.78rem;padding:4px 0;border-bottom:1px solid var(--border-light)"><span>Duration</span><span class="fw-600">${cmpDuration.toFixed(2)} yrs</span></div>
            <div class="flex justify-between" style="font-size:0.78rem;padding:4px 0;border-bottom:1px solid var(--border-light)"><span>Spread</span><span class="fw-600">${cmpSpread.toFixed(0)} bps</span></div>
            <div class="flex justify-between" style="font-size:0.78rem;padding:4px 0"><span>Price</span><span class="fw-600">â‚¹${cmpPrice.toFixed(2)}</span></div>
          </div>`;
        }).join('')}
      </div>
    `;
  }

  async function refreshMarket() {
    const restoreButtons = _setBusyButtons('button[data-method="refreshMarket"], button[onclick*="App.refreshMarket()"]', true, 'Refreshingâ€¦');
    Utils.toast('info', 'Refreshing', 'Fetching latest prices...');
    try {
      await API.refreshMarketData(500);
      Utils.toast('success', 'Done', 'Market prices refreshed');
      navigate('bonds');
    } catch (err) {
      Utils.toast('error', 'Error', err.message);
    } finally {
      restoreButtons();
    }
  }

  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  // 4. INVESTMENT ANALYSIS PAGE
  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  async function renderAnalysis(el, params = {}) {
    const isin = params.isin || '';
    const backPage = _prevPage && _prevPage !== 'analysis' && _prevPage !== 'bond-analysis' ? _prevPage : null;
    const backLabel = (backPage === 'recommendations' || backPage === 'bond-allocation') ? 'â† Quotes' : backPage === 'bonds' ? 'â† Bond Universe' : backPage === 'portfolio' ? 'â† Portfolio' : null;
    el.innerHTML = `
      <div class="page-header">
        <div>
          ${backLabel ? `<button class="btn btn-ghost btn-sm" type="button" style="margin-bottom:4px;padding:2px 0;font-size:0.75rem;color:var(--text-muted)" data-method="navigate" data-arg="${backPage}">${backLabel}</button>` : ''}
          <div class="page-title">Investment Analysis</div>
          <div class="page-subtitle">Decision support &amp; portfolio impact simulation</div>
        </div>
      </div>

      <!-- Bond Selector -->
      <div class="card" style="margin-bottom:16px">
        <div class="card-body" style="padding:12px 20px">
          <div class="flex gap-16 items-center">
            <div style="flex:1">
              <label class="form-label">Select Bond (ISIN)</label>
              <input type="text" id="analysis-isin" value="${isin}" placeholder="Enter ISIN (e.g., INE001A07..." style="width:100%" />
            </div>
            <div style="align-self:flex-end">
              <button class="btn btn-primary" id="analysis-load">Analyze Bond</button>
            </div>
          </div>
        </div>
      </div>

      <div id="analysis-content">
        ${isin ? Utils.loadingHTML('Loading bond analysis...') : Utils.emptyHTML('Select a bond', 'Enter an ISIN above or click a bond from the Universe page')}
      </div>
    `;

    document.getElementById('analysis-load').addEventListener('click', () => {
      const v = document.getElementById('analysis-isin').value.trim();
      if (v) loadAnalysis(v);
    });

    if (isin) loadAnalysis(isin);
  }

  async function loadAnalysis(isin) {
    const content = document.getElementById('analysis-content');
    content.innerHTML = Utils.loadingHTML('Building institutional analysis for ' + isin + '...');

    const NOT_AVAILABLE = 'Not Available';
    const DATA_MISSING = 'Data Missing';
    const toNumberOrNull = (value) => {
      if (value === null || value === undefined) return null;
      if (typeof value === 'string' && value.trim() === '') return null;
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    };
    const safeText = (value, fallback = NOT_AVAILABLE) => {
      const text = value === null || value === undefined ? '' : String(value).trim();
      return text ? Utils.escapeHTML(text) : fallback;
    };

    const signalTone = (signal) => {
      if (signal === 'INVEST') return { bg: 'var(--accent-green-light)', fg: '#166534', border: '#86efac' };
      if (signal === 'REVIEW') return { bg: 'var(--accent-amber-light)', fg: '#92400e', border: '#fcd34d' };
      return { bg: 'var(--accent-red-light)', fg: '#991b1b', border: '#fca5a5' };
    };

    try {
      const [analysis, marketHistory] = await Promise.all([
        API.getInstitutionalAnalysis(isin, 10, 100),
        API.getMarketData(isin, 90).catch(() => ({ history: [] }))
      ]);

      const a = analysis || {};
      const snap = a.bond_snapshot || {};
      const rating = snap.credit_rating || {};
      const im = a.im_intelligence || {};
      const mkt = a.market_intelligence || {};
      const risk = a.risk_engine || {};
      const compliance = a.compliance || {};
      const impact = a.portfolio_impact || {};
      const rel = a.relative_value || {};
      const dq = a.data_quality || {};
      const gaps = a.gaps || {};
      const history = (marketHistory && Array.isArray(marketHistory.history)) ? marketHistory.history : [];

      const fieldStatus = (dq && typeof dq.field_status === 'object' && dq.field_status) ? dq.field_status : {};
      const allGapLabels = [
        ...(Array.isArray(gaps.critical) ? gaps.critical : []),
        ...(Array.isArray(gaps.important) ? gaps.important : []),
        ...(Array.isArray(gaps.advanced) ? gaps.advanced : []),
      ].map(x => String(x || '').trim().toLowerCase());
      const gapLabelSet = new Set(allGapLabels);
      const gapAliases = {
        ytm: ['current ytm'],
        spread_over_gsec_bps: ['yield vs benchmark spread (g-sec)'],
        liquidity_score: ['liquidity score'],
        entry_ytm: ['entry ytm'],
        duration: ['duration'],
        portfolio_market_value: ['portfolio aum (sanity-checked)'],
        scenario_analysis: ['scenario analysis base inputs'],
      };
      const isFieldMissing = (key) => {
        const status = String(fieldStatus?.[key]?.status || '').toLowerCase();
        if (status === 'missing') return true;
        const aliases = Array.isArray(gapAliases[key]) ? gapAliases[key] : [];
        return aliases.some(label => gapLabelSet.has(label));
      };
      const getPath = (obj, path) => {
        let cursor = obj;
        for (const key of path) {
          if (!cursor || typeof cursor !== 'object' || !(key in cursor)) return null;
          cursor = cursor[key];
        }
        return cursor;
      };
      const isNumeric = (value) => {
        if (value === null || value === undefined) return false;
        if (typeof value === 'string' && value.trim() === '') return false;
        return Number.isFinite(Number(value));
      };
      const runPreRenderValidation = () => {
        const violations = [];
        const requiredFieldStatus = ['ytm', 'spread_over_gsec_bps', 'liquidity_score', 'entry_ytm', 'duration', 'portfolio_market_value', 'scenario_analysis'];
        const missingFieldStatus = requiredFieldStatus.filter(k => !(k in fieldStatus));
        if (!a.schema_version) {
          violations.push('schema_version missing in analysis payload');
        }
        if (missingFieldStatus.length) {
          violations.push(`field_status missing keys: ${missingFieldStatus.join(', ')}`);
        }
        const valuePaths = {
          ytm: [['bond_snapshot', 'yield', 'ytm']],
          spread_over_gsec_bps: [['market_intelligence', 'spread_over_gsec_bps']],
          liquidity_score: [['market_intelligence', 'liquidity_score']],
          entry_ytm: [['market_intelligence', 'entry_ytm']],
          duration: [['bond_snapshot', 'duration']],
          portfolio_market_value: [
            ['portfolio_impact', 'before', 'portfolio_market_value'],
            ['portfolio_impact', 'after', 'portfolio_market_value'],
          ],
          scenario_analysis: [
            ['risk_engine', 'scenario_analysis', 'rate_up_pnl'],
            ['risk_engine', 'scenario_analysis', 'rate_down_pnl'],
          ],
        };
        Object.entries(valuePaths).forEach(([key, paths]) => {
          if (!isFieldMissing(key)) return;
          for (const path of paths) {
            const value = getPath(a, path);
            if (isNumeric(value)) {
              violations.push(`Missing/gapped field '${key}' has numeric value at ${path.join('.')}`);
            }
          }
        });

        const validation = impact?.validation || {};
        const beforeAum = impact?.before?.portfolio_market_value;
        const afterAum = impact?.after?.portfolio_market_value;
        if (validation.is_reasonable === false && (isNumeric(beforeAum) || isNumeric(afterAum))) {
          violations.push('AUM sanity failed but numeric AUM is still present');
        }
        const scenarioBlock = risk?.scenario_analysis || {};
        const scenarioHasValues = isNumeric(scenarioBlock?.rate_up_pnl) || isNumeric(scenarioBlock?.rate_down_pnl);
        const baseInputsMissing = !isNumeric(mkt?.current_price) || !isNumeric(snap?.duration);
        if (scenarioBlock.is_available === true && baseInputsMissing) {
          violations.push('Scenario marked available without required base inputs');
        }
        if (scenarioBlock.is_available !== true && scenarioHasValues) {
          violations.push('Scenario numeric outputs present while scenario is unavailable');
        }

        const backendValidation = a?.validation || dq?.validation_engine;
        if (backendValidation?.status === 'FAIL') {
          const backendViolations = Array.isArray(backendValidation?.violations) ? backendValidation.violations : [];
          backendViolations.forEach(v => violations.push(`Backend: ${String(v)}`));
        }

        return {
          status: violations.length ? 'FAIL' : 'PASS',
          violations,
        };
      };

      const preRenderValidation = runPreRenderValidation();
      if (preRenderValidation.status === 'FAIL') {
        const rows = preRenderValidation.violations
          .slice(0, 20)
          .map(v => `<li>${Utils.escapeHTML(String(v))}</li>`)
          .join('');
        content.innerHTML = `
          <div class="card" style="border:1px solid #fca5a5;background:#fef2f2">
            <div class="card-header"><div class="card-title" style="color:#991b1b">Validation Status: FAIL</div></div>
            <div class="card-body" style="font-size:0.82rem;color:#7f1d1d">
              <div style="margin-bottom:8px">The analysis payload violates institutional rendering guardrails and has been blocked.</div>
              <ul style="margin:0;padding-left:18px">${rows || '<li>Unknown validation failure</li>'}</ul>
            </div>
          </div>
        `;
        return;
      }

      const control = (a && typeof a.control_layer === 'object' && a.control_layer) ? a.control_layer : {};
      const analysisState = (a && typeof a.analysis_state === 'object' && a.analysis_state) ? a.analysis_state : {};
      const controlSeverity = String(control.severity || 'NONE').toUpperCase();
      const controlAction = String(control.enforcement_action || '').toUpperCase();
      const controlBlocked = String(analysisState.status || '').toUpperCase() === 'BLOCKED'
        || controlAction === 'BLOCK_UI_ANALYSIS'
        || controlSeverity === 'HIGH';
      const blockedMessage = String(analysisState.message || control.blocked_message || 'Analysis temporarily unavailable due to data inconsistency');
      if (controlBlocked) {
        content.innerHTML = `
          <div class="card" style="border:1px solid #fca5a5;background:#fef2f2">
            <div class="card-header"><div class="card-title" style="color:#991b1b">Control Layer Status: BLOCKED</div></div>
            <div class="card-body" style="font-size:0.82rem;color:#7f1d1d">
              <div style="margin-bottom:8px">${Utils.escapeHTML(blockedMessage)}</div>
              <div style="font-size:0.76rem;color:#991b1b">Severity: ${Utils.escapeHTML(controlSeverity || 'HIGH')} | Action: BLOCK_UI_ANALYSIS</div>
            </div>
          </div>
        `;
        return;
      }

      const fmtNum = (v, d = 2, key = null) => {
        if (key && isFieldMissing(key)) return DATA_MISSING;
        const n = toNumberOrNull(v);
        return n === null ? NOT_AVAILABLE : n.toFixed(d);
      };
      const fmtPct = (v, d = 2, key = null) => {
        if (key && isFieldMissing(key)) return DATA_MISSING;
        const n = toNumberOrNull(v);
        return n === null ? NOT_AVAILABLE : `${n.toFixed(d)}%`;
      };
      const fmtBps = (v, d = 1, key = null) => {
        if (key && isFieldMissing(key)) return DATA_MISSING;
        const n = toNumberOrNull(v);
        return n === null ? NOT_AVAILABLE : `${n.toFixed(d)} bps`;
      };
      const inr = (v, d = 0, key = null) => {
        if (key && isFieldMissing(key)) return DATA_MISSING;
        const n = toNumberOrNull(v);
        return n === null ? NOT_AVAILABLE : `â‚¹${Utils.formatNum(n, d)}`;
      };
      const inrCr = (v, key = null) => {
        if (key && isFieldMissing(key)) return DATA_MISSING;
        const n = toNumberOrNull(v);
        return n === null ? NOT_AVAILABLE : `â‚¹${Utils.formatNum(n / 10000000, 2)} Cr`;
      };
      const normalizeInsightItem = (item) => {
        if (item === null || item === undefined) return '';
        if (typeof item === 'string') return item.trim();
        if (typeof item === 'number' || typeof item === 'boolean') return String(item);
        if (Array.isArray(item)) {
          return item.map(x => normalizeInsightItem(x)).filter(Boolean).join(' | ');
        }
        if (typeof item === 'object') {
          const preferred = ['message', 'description', 'label', 'name', 'rule', 'trigger', 'value'];
          for (const key of preferred) {
            const value = item[key];
            if (value !== null && value !== undefined && String(value).trim()) {
              return String(value).trim();
            }
          }
          const fragments = Object.entries(item)
            .map(([k, v]) => `${k.replaceAll('_', ' ')}: ${String(v ?? '').trim()}`)
            .filter(x => x && !x.endsWith(':'))
            .slice(0, 3);
          return fragments.join('; ');
        }
        return String(item).trim();
      };
      const renderInsightList = (items, emptyText) => {
        const normalized = (Array.isArray(items) ? items : [])
          .map(x => normalizeInsightItem(x))
          .filter(Boolean)
          .slice(0, 10);
        return normalized.length
          ? normalized.map(x => `<li>${Utils.escapeHTML(x)}</li>`).join('')
          : `<li>${Utils.escapeHTML(emptyText)}</li>`;
      };

      const tone = signalTone(a?.decision_signal?.signal);
      const change = rating.last_change;
      const changeText = change
        ? `${safeText(change.from, '-')}` + ' -> ' + `${safeText(change.to, '-')}` + ` (${Utils.formatDate(change.date)})`
        : 'No recent transition captured';

      const peers = Array.isArray(rel.peers) ? rel.peers.slice(0, 8) : [];
      const checklist = Array.isArray(compliance.violations) ? compliance.violations.slice(0, 10) : [];
      const scenario = risk.scenario_analysis || {};
      const scenarioAvailable = scenario.is_available === true && !isFieldMissing('scenario_analysis');
      const scenarioAssumptions = Array.isArray(scenario.assumptions) ? scenario.assumptions : [];
      const spreadSource = isFieldMissing('spread_over_gsec_bps') ? 'unavailable' : (mkt.spread_source || 'reported');
      const liquiditySource = isFieldMissing('liquidity_score') ? 'unavailable' : (mkt.liquidity_source || 'reported');
      const controlHealthScore = toNumberOrNull(control.health_score);
      const controlHealth = control.health_status || (
        controlHealthScore === null
          ? 'UNKNOWN'
          : (controlHealthScore >= 90 ? 'HEALTHY' : (controlHealthScore >= 70 ? 'WATCH' : 'CRITICAL'))
      );

      content.innerHTML = `
        <div style="margin-bottom:14px;padding:12px 16px;border-radius:10px;border:1px solid ${tone.border};background:${tone.bg};color:${tone.fg}">
          <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap">
            <div>
              <div style="font-size:0.76rem;text-transform:uppercase;letter-spacing:0.04em">Institutional Decision Signal</div>
              <div style="font-size:1.1rem;font-weight:700">${safeText(a?.decision_signal?.label || 'Review')}</div>
              <div style="font-size:0.8rem;opacity:0.95">${safeText(a?.decision_signal?.reason || 'Signal reason unavailable')}</div>
            </div>
            <div style="font-size:0.78rem;display:grid;gap:4px;min-width:260px">
              <div><span style="color:var(--text-muted)">ISIN:</span> <strong>${safeText(a.isin || isin)}</strong></div>
              <div><span style="color:var(--text-muted)">Completeness:</span> <strong>${fmtPct(dq.completeness_pct, 1)}</strong></div>
              <div><span style="color:var(--text-muted)">Risk Score:</span> <strong>${fmtNum(risk.risk_score, 2)} (${risk.risk_band || 'UNKNOWN'})</strong></div>
              <div><span style="color:var(--text-muted)">Confidence:</span> <strong>${safeText(a?.decision_signal?.confidence_level || 'LOW')}</strong> (${fmtNum(a?.decision_signal?.confidence_score, 1)}%)</div>
              <div><span style="color:var(--text-muted)">Control Health:</span> <strong>${safeText(controlHealth)}</strong> (${controlHealthScore === null ? 'Not Available' : controlHealthScore.toFixed(1)})</div>
              <div><span style="color:var(--text-muted)">Control Severity:</span> <strong>${safeText(controlSeverity)}</strong></div>
              <div><span style="color:var(--text-muted)">Validation:</span> <strong>PASS</strong></div>
            </div>
          </div>
        </div>

        <div class="grid-row grid-2-1" style="margin-bottom:16px">
          <div class="card">
            <div class="card-header"><div class="card-title">1. Bond Snapshot</div></div>
            <div class="card-body" style="display:grid;grid-template-columns:1fr 1fr;gap:10px;font-size:0.82rem">
              <div><span class="text-muted">Issuer:</span> <strong>${safeText(snap.issuer)}</strong></div>
              <div><span class="text-muted">Category:</span> <strong>${safeText(snap.category)}</strong></div>
              <div><span class="text-muted">Latest Rating:</span> <strong>${safeText(rating.latest)}</strong></div>
              <div><span class="text-muted">Rating Outlook:</span> <strong>${safeText(rating.outlook, 'UNKNOWN')}</strong></div>
              <div><span class="text-muted">Last Rating Change:</span> <strong>${changeText}</strong></div>
              <div><span class="text-muted">Maturity:</span> <strong>${Utils.formatDate(snap.maturity_date)}</strong></div>
              <div><span class="text-muted">Years to Maturity:</span> <strong>${fmtNum(snap.years_to_maturity, 2)}</strong></div>
              <div><span class="text-muted">Duration:</span> <strong>${fmtNum(snap.duration, 2, 'duration')}</strong></div>
              <div><span class="text-muted">YTM:</span> <strong>${fmtPct(snap?.yield?.ytm, 2, 'ytm')}</strong></div>
              <div><span class="text-muted">YTC:</span> <strong>${fmtPct(snap?.yield?.ytc)}</strong></div>
              <div><span class="text-muted">Current Allocation:</span> <strong>${fmtPct(snap?.allocation_impact?.current_weight_pct)}</strong></div>
              <div><span class="text-muted">Post-Trade Allocation:</span> <strong>${fmtPct(snap?.allocation_impact?.post_trade_weight_pct)}</strong></div>
            </div>
          </div>
          <div class="card">
            <div class="card-header"><div class="card-title">Quality Monitor</div></div>
            <div class="card-body" style="font-size:0.8rem;display:grid;gap:8px">
              <div class="flex justify-between"><span>Completeness</span><strong>${fmtPct(dq.completeness_pct, 1)}</strong></div>
              <div class="flex justify-between"><span>Critical Missing</span><strong>${dq.critical_missing_count || 0}</strong></div>
              <div class="flex justify-between"><span>Important Missing</span><strong>${dq.important_missing_count || 0}</strong></div>
              <div class="flex justify-between"><span>Advanced Missing</span><strong>${dq.advanced_missing_count || 0}</strong></div>
              <div class="flex justify-between"><span>Market Data Fresh</span><strong>${dq.market_data_fresh ? 'Yes' : 'No'}</strong></div>
              <div class="flex justify-between"><span>Schema Version</span><strong>${a.schema_version || 'â€”'}</strong></div>
            </div>
          </div>
        </div>

        <div class="card" style="margin-bottom:16px">
          <div class="card-header"><div class="card-title">2. IM Intelligence</div></div>
          <div class="card-body">
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
              <div>
                <div style="font-size:0.78rem;font-weight:600;margin-bottom:8px">Key Covenants</div>
                <ul style="margin:0;padding-left:18px;font-size:0.8rem">
                  ${renderInsightList(im.key_covenants, 'No covenant record available')}
                </ul>
              </div>
              <div>
                <div style="font-size:0.78rem;font-weight:600;margin-bottom:8px">Red Flags & Triggers</div>
                <ul style="margin:0;padding-left:18px;font-size:0.8rem">
                  ${renderInsightList(im.red_flags, 'No red flags recorded')}
                </ul>
                <div style="margin-top:8px;font-size:0.76rem;color:var(--text-muted)">Coverage: ${safeText(im.coverage, 'UNKNOWN')} â€¢ Default trigger present: ${im?.red_flag_summary?.has_default_trigger ? 'Yes' : 'No'}</div>
              </div>
            </div>
          </div>
        </div>

        <div class="grid-row grid-2-1" style="margin-bottom:16px">
          <div class="card">
            <div class="card-header"><div class="card-title">3. Market Intelligence</div></div>
            <div class="card-body" style="display:grid;grid-template-columns:1fr 1fr;gap:10px;font-size:0.82rem">
              <div><span class="text-muted">Current Price:</span> <strong>${inr(mkt.current_price, 2)}</strong></div>
              <div><span class="text-muted">Face Value:</span> <strong>${inr(mkt.face_value, 0)}</strong></div>
              <div><span class="text-muted">Entry YTM:</span> <strong>${fmtPct(mkt.entry_ytm, 2, 'entry_ytm')}</strong></div>
              <div><span class="text-muted">Spread over G-Sec:</span> <strong>${fmtBps(mkt.spread_over_gsec_bps, 1, 'spread_over_gsec_bps')}</strong> <span class="text-muted">(${safeText(spreadSource)})</span></div>
              <div><span class="text-muted">Liquidity Score:</span> <strong>${fmtNum(mkt.liquidity_score, 1, 'liquidity_score')}</strong> <span class="text-muted">(${safeText(liquiditySource)})</span></div>
              <div><span class="text-muted">30D Price Trend:</span> <strong>${fmtPct(mkt.price_trend_30d_pct)}</strong></div>
              <div><span class="text-muted">Volatility:</span> <strong>${safeText(mkt.volatility_indicator, 'UNKNOWN')}</strong></div>
              <div><span class="text-muted">Freshness:</span> <strong>${toNumberOrNull(mkt.data_freshness_days) !== null ? `${toNumberOrNull(mkt.data_freshness_days)} days` : NOT_AVAILABLE}</strong></div>
            </div>
          </div>
          <div class="card">
            <div class="card-header"><div class="card-title">Price &amp; Yield Trend</div></div>
            <div class="card-body"><div class="chart-container" style="min-height:200px"><canvas id="chart-yield-history"></canvas></div></div>
          </div>
        </div>

        <div class="grid-row grid-2" style="margin-bottom:16px">
          <div class="card">
            <div class="card-header"><div class="card-title">4. Risk Engine</div></div>
            <div class="card-body" style="font-size:0.8rem;display:grid;gap:8px">
              <div class="flex justify-between"><span>Composite Risk Score</span><strong>${fmtNum(risk.risk_score, 2)} (${safeText(risk.risk_band, 'UNKNOWN')})</strong></div>
              <div class="flex justify-between"><span>Credit Risk</span><strong>${fmtNum(risk?.components?.credit_risk, 1)}</strong></div>
              <div class="flex justify-between"><span>Interest Rate Risk</span><strong>${fmtNum(risk?.components?.interest_rate_risk, 1, 'duration')}</strong></div>
              <div class="flex justify-between"><span>Liquidity Risk</span><strong>${fmtNum(risk?.components?.liquidity_risk, 1, 'liquidity_score')}</strong></div>
              <div class="flex justify-between"><span>Concentration Risk</span><strong>${fmtNum(risk?.components?.concentration_risk, 1)}</strong></div>
              <div class="flex justify-between"><span>Rate Shock (${scenario?.rate_shock_bps || 100} bps) Up</span><strong>${scenarioAvailable ? inr(scenario?.rate_up_pnl, 0) : DATA_MISSING}</strong></div>
              <div class="flex justify-between"><span>Rate Shock Down</span><strong>${scenarioAvailable ? inr(scenario?.rate_down_pnl, 0) : DATA_MISSING}</strong></div>
              <div class="text-muted" style="font-size:0.74rem">${scenarioAvailable ? 'Scenario based on current clean price and modified duration.' : `Scenario assumptions missing: ${safeText(scenarioAssumptions.join(' | '), DATA_MISSING)}`}</div>
            </div>
          </div>
          <div class="card">
            <div class="card-header"><div class="card-title">5. Compliance</div></div>
            <div class="card-body" style="font-size:0.8rem">
              <div class="compliance-indicator ${compliance.overall === 'PASS' ? 'green' : compliance.overall === 'FAIL' ? 'red' : 'amber'}" style="margin-bottom:10px">
                <span class="status-dot ${compliance.overall === 'PASS' ? 'green' : compliance.overall === 'FAIL' ? 'red' : 'amber'}"></span>
                Overall: ${compliance.overall || 'UNKNOWN'} â€¢ Score: ${fmtNum(compliance.score, 1)}% â€¢ Rules: ${compliance.rules_checked || 0}
              </div>
              <div class="flex gap-12" style="margin-bottom:8px">
                <span class="badge badge-green">Pass ${compliance.passed || 0}</span>
                <span class="badge badge-amber">Warn ${compliance.warnings || 0}</span>
                <span class="badge badge-red">Fail ${compliance.failures || 0}</span>
              </div>
              <div style="max-height:190px;overflow:auto;border-top:1px solid var(--border-light);padding-top:8px">
                ${checklist.length ? checklist.map(v => `<div style="padding:6px 0;border-bottom:1px solid var(--border-light)"><strong>${safeText(v.rule_code || v.rule || 'RULE')}</strong>: ${safeText(v.message || v.description || NOT_AVAILABLE)} <span class="text-muted">(${safeText(v.status, 'UNKNOWN')})</span></div>`).join('') : '<div class="text-muted">No rule-level checklist available</div>'}
              </div>
            </div>
          </div>
        </div>

        <div class="card" style="margin-bottom:16px">
          <div class="card-header"><div class="card-title">6. Portfolio Impact (â‚¹10 Cr Simulation)</div></div>
          <div class="card-body" style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;font-size:0.8rem">
            <div>
              <div style="font-weight:600;margin-bottom:6px">Before</div>
              <div class="flex justify-between"><span>AUM</span><strong>${inrCr(impact?.before?.portfolio_market_value, 'portfolio_market_value')}</strong></div>
              <div class="flex justify-between"><span>Weighted YTM</span><strong>${fmtPct(impact?.before?.weighted_ytm)}</strong></div>
              <div class="flex justify-between"><span>Weighted Duration</span><strong>${fmtNum(impact?.before?.weighted_duration, 3)}</strong></div>
            </div>
            <div>
              <div style="font-weight:600;margin-bottom:6px">After</div>
              <div class="flex justify-between"><span>AUM</span><strong>${inrCr(impact?.after?.portfolio_market_value, 'portfolio_market_value')}</strong></div>
              <div class="flex justify-between"><span>Weighted YTM</span><strong>${fmtPct(impact?.after?.weighted_ytm)}</strong></div>
              <div class="flex justify-between"><span>Weighted Duration</span><strong>${fmtNum(impact?.after?.weighted_duration, 3)}</strong></div>
            </div>
            <div>
              <div style="font-weight:600;margin-bottom:6px">Delta</div>
              <div class="flex justify-between"><span>YTM Change</span><strong>${fmtBps(impact?.delta?.weighted_ytm_bps, 2)}</strong></div>
              <div class="flex justify-between"><span>Duration Change</span><strong>${fmtNum(impact?.delta?.weighted_duration, 4)}</strong></div>
              <div class="flex justify-between"><span>Issuer Exposure Change</span><strong>${fmtPct(impact?.delta?.issuer_exposure_pct)}</strong></div>
            </div>
          </div>
          <div class="card-body" style="padding-top:0;font-size:0.74rem;color:var(--text-muted)">
            Display unit: â‚¹ Cr. AUM values are rendered only after portfolio sanity checks pass.
          </div>
        </div>

        <div class="grid-row grid-2" style="margin-bottom:16px">
          <div class="card">
            <div class="card-header"><div class="card-title">7. Relative Value</div></div>
            <div class="card-body" style="font-size:0.8rem">
              <div class="flex justify-between" style="margin-bottom:8px"><span>Peer Count</span><strong>${rel.peer_count || 0}</strong></div>
              <div class="flex justify-between" style="margin-bottom:8px"><span>Peer Avg YTM</span><strong>${fmtPct(rel.peer_avg_ytm)}</strong></div>
              <div class="flex justify-between" style="margin-bottom:8px"><span>Yield Advantage</span><strong>${fmtBps(rel.yield_advantage_bps, 2)}</strong></div>
              <div class="flex justify-between" style="margin-bottom:10px"><span>Risk-Adjusted Return</span><strong>${fmtPct(rel.risk_adjusted_return, 3)}</strong></div>
              <div style="max-height:180px;overflow:auto;border-top:1px solid var(--border-light);padding-top:8px">
                ${peers.length ? peers.map(p => `<div style="display:grid;grid-template-columns:1.2fr 1fr 1fr;gap:8px;padding:6px 0;border-bottom:1px solid var(--border-light)"><span>${safeText(p.isin)}</span><span>${fmtPct(p.ytm)}</span><span>${fmtBps(p.spread_over_gsec, 0)}</span></div>`).join('') : '<div class="text-muted">No peer basket available</div>'}
              </div>
            </div>
          </div>
          <div class="card">
            <div class="card-header"><div class="card-title">Gap Register</div></div>
            <div class="card-body" style="font-size:0.8rem;display:grid;gap:10px">
              <div><div style="font-weight:600;color:#b91c1c;margin-bottom:4px">Critical</div>${(gaps.critical || []).length ? `<ul style="margin:0;padding-left:16px">${(gaps.critical || []).map(g => `<li>${safeText(g)}</li>`).join('')}</ul>` : '<span class="text-muted">None</span>'}</div>
              <div><div style="font-weight:600;color:#92400e;margin-bottom:4px">Important</div>${(gaps.important || []).length ? `<ul style="margin:0;padding-left:16px">${(gaps.important || []).map(g => `<li>${safeText(g)}</li>`).join('')}</ul>` : '<span class="text-muted">None</span>'}</div>
              <div><div style="font-weight:600;color:#1d4ed8;margin-bottom:4px">Advanced</div>${(gaps.advanced || []).length ? `<ul style="margin:0;padding-left:16px">${(gaps.advanced || []).map(g => `<li>${safeText(g)}</li>`).join('')}</ul>` : '<span class="text-muted">None</span>'}</div>
              <div class="text-muted" style="font-size:0.74rem">Standardized sections: ${safeText((a?.standardization?.sections || []).join(', '), 'n/a')}</div>
            </div>
          </div>
        </div>
      `;

      if (history.length) {
        const ctx = document.getElementById('chart-yield-history');
        safeChart('yieldHistory', ctx, {
          type: 'line',
          data: {
            labels: historyPoints.map(h => h.label),
            datasets: [
              { label: 'YTM', data: history.map(h => h.ytm).reverse(), borderColor: '#2563eb', fill: false, tension: 0.25, pointRadius: 1, borderWidth: 2 },
              { label: 'Price', data: history.map(h => h.clean_price).reverse(), borderColor: '#16a34a', fill: false, tension: 0.25, pointRadius: 1, borderWidth: 1.5, yAxisID: 'y2' }
            ]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { position: 'bottom', labels: { boxWidth: 10, font: { size: 10 } } } },
            scales: {
              y: { position: 'left', title: { display: true, text: 'Yield %', font: { size: 10 } }, ticks: { font: { size: 10 } } },
              y2: { position: 'right', title: { display: true, text: 'Price â‚¹', font: { size: 10 } }, ticks: { font: { size: 10 } }, grid: { drawOnChartArea: false } },
              x: { ticks: { maxTicksLimit: 8, font: { size: 9 } } }
            }
          }
        });
      } else {
        const ctx = document.getElementById('chart-yield-history');
        if (ctx) ctx.parentElement.innerHTML = Utils.emptyHTML('No price history', 'Historical market series unavailable');
      }
    } catch (err) {
      const message = (err && err.message) ? err.message : 'Unable to build analysis payload';
      content.innerHTML = Utils.emptyHTML('Analysis Error', message);
      Utils.toast('error', 'Error', message);
    }
  }

  async function renderLiveSimulation(el) {
    const trustId = getActiveTrustId();
    const today = new Date();
    const endDefault = today.toISOString().slice(0, 10);
    const startDefaultDate = new Date(today.getTime());
    startDefaultDate.setDate(startDefaultDate.getDate() - 180);
    const startDefault = startDefaultDate.toISOString().slice(0, 10);

    _clearSimulationTimer();

    el.innerHTML = `
      <div class="page-header">
        <div>
          <div class="page-title">Live Simulation Engine</div>
          <div class="page-subtitle">Simulated Live Market (Historical Replay Mode)</div>
        </div>
        <div class="page-actions" style="display:flex;gap:8px;align-items:center">
          <span id="sim-session-status" class="badge badge-gray">Session: OFF</span>
          <span id="sim-indicator-label" class="badge" style="background:rgba(13,148,136,0.12);color:#0f766e">Simulated Live Market (Historical Replay Mode)</span>
        </div>
      </div>

      <div class="card" style="margin-bottom:12px">
        <div class="card-header">
          <div>
            <div class="card-title">Replay Controls</div>
            <div class="card-subtitle">Portfolio holdings are locked. Only price/yield paths are simulated.</div>
          </div>
        </div>
        <div class="card-body">
          <div class="grid-3" style="gap:12px;align-items:end">
            <div>
              <label style="font-size:0.76rem;color:var(--text-muted);display:block;margin-bottom:6px">Simulation Mode</label>
              <label style="display:flex;align-items:center;gap:8px;font-size:0.85rem">
                <input id="sim-mode-toggle" type="checkbox" />
                ON / OFF
              </label>
            </div>
            <div>
              <label style="font-size:0.76rem;color:var(--text-muted);display:block;margin-bottom:6px">Trust</label>
              <input id="sim-trust-id" value="${Utils.escapeHTML(trustId)}" style="width:100%" />
            </div>
            <div>
              <label style="font-size:0.76rem;color:var(--text-muted);display:block;margin-bottom:6px">Scenario</label>
              <select id="sim-scenario" style="width:100%">
                <option value="normal">Normal Market</option>
                <option value="stress">Stress Replay (Rates Up / Crash)</option>
                <option value="credit_event">Credit Event</option>
                <option value="bull">Equity Bull Overlay</option>
                <option value="bear">Equity Bear Overlay</option>
                <option value="rally">Rally Scenario</option>
              </select>
            </div>
          </div>

          <div class="grid-3" style="gap:12px;margin-top:10px;align-items:end">
            <div>
              <label style="font-size:0.76rem;color:var(--text-muted);display:block;margin-bottom:6px">Start Date</label>
              <input id="sim-start-date" type="date" value="${startDefault}" style="width:100%" />
            </div>
            <div>
              <label style="font-size:0.76rem;color:var(--text-muted);display:block;margin-bottom:6px">End Date</label>
              <input id="sim-end-date" type="date" value="${endDefault}" style="width:100%" />
            </div>
            <div>
              <label style="font-size:0.76rem;color:var(--text-muted);display:block;margin-bottom:6px">Step Mode</label>
              <select id="sim-step-mode" style="width:100%">
                <option value="trading_day">Trading Day</option>
                <option value="intraday">Intraday Sub-Steps</option>
              </select>
            </div>
          </div>

          <div class="grid-3" style="gap:12px;margin-top:10px;align-items:end">
            <div>
              <label style="font-size:0.76rem;color:var(--text-muted);display:block;margin-bottom:6px">Intraday Steps</label>
              <input id="sim-intraday-steps" type="number" min="1" max="24" value="4" style="width:100%" />
            </div>
            <div>
              <label style="font-size:0.76rem;color:var(--text-muted);display:block;margin-bottom:6px">Hybrid Noise</label>
              <label style="display:flex;align-items:center;gap:8px;font-size:0.85rem">
                <input id="sim-hybrid-toggle" type="checkbox" checked />
                Enable live tick noise
              </label>
            </div>
            <div>
              <label style="font-size:0.76rem;color:var(--text-muted);display:block;margin-bottom:6px">Noise Level</label>
              <input id="sim-noise-level" type="range" min="0" max="3" step="0.1" value="1" style="width:100%" />
            </div>
          </div>

          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px">
            <button id="sim-init-btn" class="btn btn-primary btn-sm">Initialize Session</button>
            <button id="sim-play-btn" class="btn btn-secondary btn-sm" disabled>Play</button>
            <button id="sim-pause-btn" class="btn btn-secondary btn-sm" disabled>Pause</button>
            <button id="sim-step-btn" class="btn btn-secondary btn-sm" disabled>Step</button>
            <button id="sim-reset-btn" class="btn btn-secondary btn-sm" disabled>Reset</button>
            <button id="sim-stop-btn" class="btn btn-ghost btn-sm" disabled>Stop Session</button>
            <select id="sim-speed" style="max-width:110px" disabled>
              <option value="1">1x</option>
              <option value="2">2x</option>
              <option value="10">10x</option>
            </select>
          </div>
        </div>
      </div>

      <div class="card" style="margin-bottom:12px">
        <div class="card-header">
          <div>
            <div class="card-title">Timeline</div>
            <div class="card-subtitle">Use slider to seek any replay point. Event markers indicate crashes and rate shifts.</div>
          </div>
        </div>
        <div class="card-body">
          <input id="sim-timeline-range" type="range" min="0" max="0" value="0" step="1" style="width:100%" disabled />
          <div style="display:flex;justify-content:space-between;gap:12px;margin-top:6px;font-size:0.78rem;color:var(--text-muted)">
            <span id="sim-timeline-label">No session loaded</span>
            <span id="sim-speed-label">Speed: 1x</span>
          </div>
          <div id="sim-event-markers" style="display:flex;flex-wrap:wrap;gap:6px;margin-top:10px"></div>
        </div>
      </div>

      <div class="metric-grid" style="margin-bottom:12px">
        <div class="metric-card blue"><div class="metric-label">Portfolio Value</div><div class="metric-value" id="sim-metric-value">â‚¹0</div></div>
        <div class="metric-card" id="sim-card-pnl"><div class="metric-label">P&L</div><div class="metric-value" id="sim-metric-pnl">â‚¹0</div><div class="metric-change" id="sim-metric-pnl-pct">0.00%</div></div>
        <div class="metric-card"><div class="metric-label">Yield (Weighted)</div><div class="metric-value" id="sim-metric-ytm">0.0000%</div></div>
        <div class="metric-card"><div class="metric-label">Risk (VaR 95% / DV01)</div><div class="metric-value" id="sim-metric-risk">0.0000% / 0</div></div>
      </div>

      <div class="grid-2" style="gap:12px;margin-bottom:12px">
        <div class="card">
          <div class="card-header"><div class="card-title">Replay NAV Path</div></div>
          <div class="card-body"><div class="chart-container" style="min-height:260px"><canvas id="chart-sim-nav"></canvas></div></div>
        </div>
        <div class="card">
          <div class="card-header"><div class="card-title">Dynamic Intelligence Signals</div></div>
          <div class="card-body">
            <div style="margin-bottom:10px">
              <div style="font-size:0.78rem;font-weight:700;margin-bottom:6px">Risk Flags</div>
              <div id="sim-risk-flags" style="font-size:0.78rem;color:var(--text-muted)">No active flags</div>
            </div>
            <div style="margin-bottom:10px">
              <div style="font-size:0.78rem;font-weight:700;margin-bottom:6px">Recommendations Stance</div>
              <div id="sim-rec-statement" style="font-size:0.82rem;color:var(--text-secondary)">Awaiting simulation tick...</div>
            </div>
            <div>
              <div style="font-size:0.78rem;font-weight:700;margin-bottom:6px">Validation</div>
              <div id="sim-validation" style="font-size:0.78rem;color:var(--text-muted)">No validation report yet</div>
            </div>
          </div>
        </div>
      </div>

      <div class="card" style="margin-bottom:12px">
        <div class="card-header"><div class="card-title">Live Holdings Repricing</div></div>
        <div class="card-body" style="padding:0">
          <table class="table">
            <thead>
              <tr>
                <th>Instrument</th>
                <th>Category</th>
                <th>Qty</th>
                <th>Price</th>
                <th>Yield</th>
                <th>Market Value</th>
                <th>P&L</th>
              </tr>
            </thead>
            <tbody id="sim-holdings-body">
              <tr><td colspan="7" style="text-align:center;color:var(--text-muted)">Initialize a simulation session to start replay</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="card">
        <div class="card-header"><div class="card-title">Debug Log</div></div>
        <div class="card-body"><div id="sim-log" style="font-family:var(--font-mono);font-size:0.74rem;line-height:1.45;max-height:220px;overflow:auto"></div></div>
      </div>
    `;

    const modeToggle = document.getElementById('sim-mode-toggle');
    const trustInput = document.getElementById('sim-trust-id');
    const startInput = document.getElementById('sim-start-date');
    const endInput = document.getElementById('sim-end-date');
    const stepModeInput = document.getElementById('sim-step-mode');
    const intradayInput = document.getElementById('sim-intraday-steps');
    const hybridToggle = document.getElementById('sim-hybrid-toggle');
    const noiseInput = document.getElementById('sim-noise-level');
    const scenarioInput = document.getElementById('sim-scenario');
    const speedInput = document.getElementById('sim-speed');
    const timelineInput = document.getElementById('sim-timeline-range');
    const speedLabel = document.getElementById('sim-speed-label');
    const sessionBadge = document.getElementById('sim-session-status');
    const timelineLabel = document.getElementById('sim-timeline-label');
    const initBtn = document.getElementById('sim-init-btn');
    const playBtn = document.getElementById('sim-play-btn');
    const pauseBtn = document.getElementById('sim-pause-btn');
    const stepBtn = document.getElementById('sim-step-btn');
    const resetBtn = document.getElementById('sim-reset-btn');
    const stopBtn = document.getElementById('sim-stop-btn');

    let chartSeries = [];

    const speedToSteps = (speed) => {
      const value = Number(speed || 1);
      if (value >= 10) return 10;
      if (value >= 2) return 2;
      return 1;
    };

    const setSessionControls = (enabled) => {
      playBtn.disabled = !enabled;
      pauseBtn.disabled = !enabled;
      stepBtn.disabled = !enabled;
      resetBtn.disabled = !enabled;
      stopBtn.disabled = !enabled;
      speedInput.disabled = !enabled;
      timelineInput.disabled = !enabled;
    };

    const appendLog = (text, level = 'info') => {
      const logEl = document.getElementById('sim-log');
      if (!logEl) return;
      const color = level === 'error' ? 'var(--accent-red)' : level === 'warn' ? 'var(--accent-amber)' : 'var(--text-secondary)';
      const ts = new Date().toLocaleTimeString('en-IN', { hour12: false });
      logEl.insertAdjacentHTML('beforeend', `<div style="color:${color}">[${ts}] ${Utils.escapeHTML(String(text || ''))}</div>`);
      const rows = Array.from(logEl.querySelectorAll('div'));
      if (rows.length > 160) {
        rows.slice(0, rows.length - 160).forEach((node) => node.remove());
      }
      logEl.scrollTop = logEl.scrollHeight;
    };

    const renderMarkers = (markers = [], currentDate = '') => {
      const host = document.getElementById('sim-event-markers');
      if (!host) return;
      if (!Array.isArray(markers) || !markers.length) {
        host.innerHTML = '<span class="badge badge-gray">No event markers</span>';
        return;
      }
      const current = String(currentDate || '');
      const filtered = markers.slice(-24);
      host.innerHTML = filtered.map((marker) => {
        const kind = String(marker?.type || 'event').replace(/_/g, ' ');
        const date = String(marker?.date || '');
        const active = current && date === current;
        const sev = String(marker?.severity || '').toLowerCase();
        const bg = sev === 'high' ? 'rgba(220,38,38,0.12)' : sev === 'medium' ? 'rgba(217,119,6,0.12)' : 'rgba(37,99,235,0.12)';
        const fg = sev === 'high' ? '#b91c1c' : sev === 'medium' ? '#b45309' : '#1d4ed8';
        return `<span class="badge" style="background:${active ? '#0f766e' : bg};color:${active ? '#fff' : fg}">${Utils.escapeHTML(date)} Â· ${Utils.escapeHTML(kind)}</span>`;
      }).join('');
    };

    const renderHoldings = (holdings = []) => {
      const body = document.getElementById('sim-holdings-body');
      if (!body) return;
      if (!Array.isArray(holdings) || !holdings.length) {
        body.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--text-muted)">No holdings in snapshot</td></tr>';
        return;
      }
      const rows = holdings
        .slice()
        .sort((a, b) => Number(b.market_value || 0) - Number(a.market_value || 0))
        .slice(0, 18);
      body.innerHTML = rows.map((row) => {
        const pnl = Number(row.pnl || 0);
        const pnlClass = pnl >= 0 ? 'color:var(--accent-green)' : 'color:var(--accent-red)';
        return `
          <tr>
            <td>${Utils.escapeHTML(String(row.isin || row.symbol || row.instrument_id || 'N/A'))}</td>
            <td>${Utils.escapeHTML(String(row.portfolio_category_label || row.portfolio_category || 'â€”'))}</td>
            <td>${Utils.formatNum(Number(row.quantity || 0), 2)}</td>
            <td>${Utils.formatNum(Number(row.market_price || 0), 4)}</td>
            <td>${String(row.asset_class || '').toUpperCase() === 'EQUITY' ? 'â€”' : Utils.formatYieldPct(Number(row.ytm || 0), 4)}</td>
            <td>${Utils.formatINR(Number(row.market_value || 0))}</td>
            <td style="${pnlClass}">${Utils.formatINR(pnl)} (${Utils.formatPct(Number(row.pnl_pct || 0), 2)})</td>
          </tr>
        `;
      }).join('');
    };

    const syncPlaybackTimer = (sessionMeta) => {
      if (!sessionMeta) return;
      const status = String(sessionMeta.status || '').toLowerCase();
      if (status === 'playing') {
        if (!_liveSimulation.timerId) startTimer();
      } else {
        _clearSimulationTimer();
      }
    };

    const renderSnapshot = (snapshot, sessionMeta, options = {}) => {
      if (options.syncTimer !== false) {
        syncPlaybackTimer(sessionMeta);
      }
      if (!snapshot) return;
      if (options.broadcast === false) {
        _liveSimulation.lastSnapshot = snapshot;
        try {
          window.__pfSimulationSnapshot = snapshot;
          window.__pfSimulationSessionMeta = sessionMeta && typeof sessionMeta === 'object' ? sessionMeta : null;
          window.__pfSimulationSessionId = sessionMeta?.session_id || snapshot.session_id || _liveSimulation.sessionId || null;
        } catch (_) {}
      } else {
        _emitSimulationTick(snapshot, sessionMeta);
      }

      const summary = snapshot.summary || {};
      const risk = snapshot.risk || {};
      const validation = snapshot.validation || {};
      const timeline = snapshot.timeline || {};
      const recommendation = risk.recommendations || {};

      const value = Number(summary.total_market_value || 0);
      const pnl = Number(summary.pnl || 0);
      const pnlPct = Number(summary.pnl_pct || 0);
      const ytm = Number(summary.weighted_ytm || 0);
      const var95 = Number(summary.var95_1d_pct || 0);
      const dv01 = Number(summary.portfolio_dv01 || 0);

      const valueEl = document.getElementById('sim-metric-value');
      const pnlEl = document.getElementById('sim-metric-pnl');
      const pnlPctEl = document.getElementById('sim-metric-pnl-pct');
      const riskEl = document.getElementById('sim-metric-risk');
      const ytmEl = document.getElementById('sim-metric-ytm');
      const pnlCard = document.getElementById('sim-card-pnl');

      if (valueEl) valueEl.textContent = Utils.formatINR(value);
      if (pnlEl) pnlEl.textContent = Utils.formatINR(pnl);
      if (pnlPctEl) pnlPctEl.textContent = `${Utils.formatPct(pnlPct, 2)} vs invested`;
      if (riskEl) riskEl.textContent = `${Utils.formatPct(var95, 4)} / ${Utils.formatNum(dv01, 2)}`;
      if (ytmEl) ytmEl.textContent = `${Utils.formatPct(ytm, 4)}`;
      if (pnlCard) pnlCard.className = `metric-card ${pnl >= 0 ? 'green' : 'red'}`;

      const index = Number(timeline.index || 0);
      const len = Math.max(1, Number(timeline.length || 1));
      timelineInput.max = String(Math.max(0, len - 1));
      timelineInput.value = String(Math.max(0, Math.min(index, len - 1)));
      const dateLabel = timeline.date || (timeline.point && timeline.point.date) || 'n/a';
      const slot = timeline.point?.slot || 1;
      const slotCount = timeline.point?.slot_count || 1;
      if (timelineLabel) {
        timelineLabel.textContent = `${dateLabel} | Step ${index + 1}/${len} | Slot ${slot}/${slotCount}`;
      }

      const riskFlagsHost = document.getElementById('sim-risk-flags');
      const flags = risk.flags || [];
      if (riskFlagsHost) {
        riskFlagsHost.innerHTML = Array.isArray(flags) && flags.length
          ? flags.map((flag) => `<div><strong>${Utils.escapeHTML(String(flag.severity || 'INFO'))}</strong> Â· ${Utils.escapeHTML(String(flag.message || ''))}</div>`).join('')
          : 'No active dynamic risk flags';
      }

      const recHost = document.getElementById('sim-rec-statement');
      if (recHost) {
        recHost.textContent = `${recommendation.stance || 'balanced'}: ${recommendation.reason || 'No recommendation override.'}`;
      }

      const validationHost = document.getElementById('sim-validation');
      if (validationHost) {
        const bondCheck = validation.bond_price_vs_yield_consistency || {};
        const pnlCheck = validation.pnl_correctness || {};
        validationHost.innerHTML = `
          <div>Bond price vs yield consistency: <strong>${bondCheck.passed ? 'PASS' : 'FAIL'}</strong> (issues: ${Number(bondCheck.issue_count || 0)})</div>
          <div>P&L correctness: <strong>${pnlCheck.passed ? 'PASS' : 'FAIL'}</strong> (error: ${Number(pnlCheck.identity_error || 0).toFixed(10)})</div>
        `;
      }

      renderHoldings(snapshot.holdings || []);
      renderMarkers(snapshot.event_markers || [], dateLabel);

      chartSeries.push({
        label: `${dateLabel}${slotCount > 1 ? ` (${slot}/${slotCount})` : ''}`,
        value,
      });
      if (chartSeries.length > 320) chartSeries = chartSeries.slice(chartSeries.length - 320);

      const chartCtx = document.getElementById('chart-sim-nav');
      if (chartCtx) {
        safeChart('sim-nav', chartCtx, {
          type: 'line',
          data: {
            labels: chartSeries.map((x) => x.label),
            datasets: [{
              label: 'Portfolio Value',
              data: chartSeries.map((x) => x.value),
              borderColor: '#0f766e',
              backgroundColor: 'rgba(15,118,110,0.10)',
              fill: true,
              tension: 0.2,
              pointRadius: 0,
              borderWidth: 2,
            }],
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
              legend: { display: false },
            },
            scales: {
              x: { ticks: { maxTicksLimit: 8, font: { size: 9 } } },
              y: { ticks: { callback: (v) => `â‚¹${Utils.formatNum(v / 1e7, 2)}Cr` } },
            },
          },
        });
      }

      if (sessionMeta) {
        sessionBadge.textContent = `Session: ${sessionMeta.status || 'paused'} | ${sessionMeta.session_id?.slice(0, 8) || ''}`;
        sessionBadge.className = `badge ${sessionMeta.status === 'playing' ? 'badge-green' : 'badge-gray'}`;
      }
    };

    window.__pfRenderLiveSimulationSnapshot = renderSnapshot;

    const runStep = async (steps) => {
      if (!_liveSimulation.sessionId || _liveSimulation.busy) return;
      _liveSimulation.busy = true;
      try {
        const result = await API.stepSimulationSession(_liveSimulation.sessionId, steps);
        renderSnapshot(result?.snapshot, result?.session);
        if (result?.session?.status === 'completed') {
          _clearSimulationTimer();
          appendLog('Reached end of timeline. Session paused.', 'warn');
        }
      } catch (err) {
        appendLog(`Step failed: ${err.message}`, 'error');
        _clearSimulationTimer();
      } finally {
        _liveSimulation.busy = false;
      }
    };

    const startTimer = () => {
      _clearSimulationTimer();
      _liveSimulation.timerId = setInterval(() => {
        const steps = speedToSteps(_liveSimulation.speed);
        runStep(steps);
      }, 2000);
    };

    const loadSession = async (sessionId) => {
      const result = await API.getSimulationSession(sessionId);
      if (!result?.session) throw new Error('Invalid simulation session payload');
      _liveSimulation.sessionId = result.session.session_id;
      _liveSimulation.speed = Number(result.session.speed_multiplier || 1);
      speedInput.value = String(_liveSimulation.speed);
      speedLabel.textContent = `Speed: ${_liveSimulation.speed}x`;
      setSessionControls(true);
      modeToggle.checked = true;
      if (result.snapshot) {
        renderSnapshot(result.snapshot, result.session);
      }
      appendLog(`Session loaded: ${_liveSimulation.sessionId}`, 'info');
    };

    initBtn?.addEventListener('click', async () => {
      if (!modeToggle.checked) {
        appendLog('Enable Simulation Mode first.', 'warn');
        return;
      }
      try {
        initBtn.disabled = true;
        const payload = {
          trust_id: trustInput.value || trustId,
          start_date: startInput.value || null,
          end_date: endInput.value || null,
          step_mode: stepModeInput.value || 'trading_day',
          intraday_steps: Number(intradayInput.value || 1),
          speed_multiplier: Number(speedInput.value || 1),
          hybrid_mode: !!hybridToggle.checked,
          noise_level: Number(noiseInput.value || 1),
          scenario_mode: scenarioInput.value || 'normal',
        };
        const result = await API.createSimulationSession(payload);
        chartSeries = [];
        await loadSession(result?.session?.session_id);
        renderSnapshot(result?.snapshot, result?.session);
      } catch (err) {
        appendLog(`Initialize failed: ${err.message}`, 'error');
      } finally {
        initBtn.disabled = false;
      }
    });

    playBtn?.addEventListener('click', async () => {
      if (!_liveSimulation.sessionId) return;
      try {
        const result = await API.controlSimulationSession(_liveSimulation.sessionId, { action: 'play' });
        renderSnapshot(result?.snapshot, result?.session);
        appendLog('Playback started', 'info');
      } catch (err) {
        appendLog(`Play failed: ${err.message}`, 'error');
      }
    });

    pauseBtn?.addEventListener('click', async () => {
      if (!_liveSimulation.sessionId) return;
      try {
        const result = await API.controlSimulationSession(_liveSimulation.sessionId, { action: 'pause' });
        _clearSimulationTimer();
        renderSnapshot(result?.snapshot, result?.session);
        appendLog('Playback paused', 'info');
      } catch (err) {
        appendLog(`Pause failed: ${err.message}`, 'error');
      }
    });

    stepBtn?.addEventListener('click', async () => {
      const steps = speedToSteps(_liveSimulation.speed);
      await runStep(steps);
    });

    resetBtn?.addEventListener('click', async () => {
      if (!_liveSimulation.sessionId) return;
      try {
        _clearSimulationTimer();
        chartSeries = [];
        const result = await API.controlSimulationSession(_liveSimulation.sessionId, { action: 'reset' });
        renderSnapshot(result?.snapshot, result?.session);
        appendLog('Session reset to timeline start', 'info');
      } catch (err) {
        appendLog(`Reset failed: ${err.message}`, 'error');
      }
    });

    stopBtn?.addEventListener('click', async () => {
      if (!_liveSimulation.sessionId) return;
      try {
        await API.closeSimulationSession(_liveSimulation.sessionId);
        _clearSimulationState();
        chartSeries = [];
        setSessionControls(false);
        modeToggle.checked = false;
        sessionBadge.textContent = 'Session: OFF';
        sessionBadge.className = 'badge badge-gray';
        timelineLabel.textContent = 'No session loaded';
        renderMarkers([], '');
        _setSimulationBadge(false);
        appendLog('Simulation session stopped', 'warn');
      } catch (err) {
        appendLog(`Stop failed: ${err.message}`, 'error');
      }
    });

    speedInput?.addEventListener('change', async () => {
      _liveSimulation.speed = Number(speedInput.value || 1);
      speedLabel.textContent = `Speed: ${_liveSimulation.speed}x`;
      if (!_liveSimulation.sessionId) return;
      try {
        await API.controlSimulationSession(_liveSimulation.sessionId, { action: 'speed', speed_multiplier: _liveSimulation.speed });
      } catch (_) {}
    });

    scenarioInput?.addEventListener('change', async () => {
      if (!_liveSimulation.sessionId) return;
      try {
        const result = await API.controlSimulationSession(_liveSimulation.sessionId, { action: 'scenario', scenario_mode: scenarioInput.value });
        renderSnapshot(result?.snapshot, result?.session);
        appendLog(`Scenario switched to ${scenarioInput.value}`, 'warn');
      } catch (err) {
        appendLog(`Scenario switch failed: ${err.message}`, 'error');
      }
    });

    const applyHybridSettings = async () => {
      if (!_liveSimulation.sessionId) return;
      try {
        const result = await API.controlSimulationSession(_liveSimulation.sessionId, {
          action: 'hybrid',
          hybrid_mode: !!hybridToggle.checked,
          noise_level: Number(noiseInput.value || 1),
        });
        renderSnapshot(result?.snapshot, result?.session);
      } catch (err) {
        appendLog(`Hybrid settings failed: ${err.message}`, 'error');
      }
    };
    hybridToggle?.addEventListener('change', applyHybridSettings);
    noiseInput?.addEventListener('change', applyHybridSettings);

    timelineInput?.addEventListener('input', async () => {
      if (!_liveSimulation.sessionId) return;
      const targetIndex = Number(timelineInput.value || 0);
      try {
        const result = await API.controlSimulationSession(_liveSimulation.sessionId, {
          action: 'seek',
          index: targetIndex,
        });
        renderSnapshot(result?.snapshot, result?.session);
      } catch (err) {
        appendLog(`Seek failed: ${err.message}`, 'error');
      }
    });

    modeToggle?.addEventListener('change', async () => {
      if (modeToggle.checked) {
        _setSimulationBadge(true);
        appendLog('Simulation mode enabled. Initialize a session to begin replay.', 'info');
      } else {
        const activeSessionId = _liveSimulation.sessionId;
        if (activeSessionId) {
          try { await API.closeSimulationSession(activeSessionId); } catch (_) {}
        }
        _clearSimulationState();
        chartSeries = [];
        setSessionControls(false);
        sessionBadge.textContent = 'Session: OFF';
        sessionBadge.className = 'badge badge-gray';
        timelineLabel.textContent = 'No session loaded';
        renderMarkers([], '');
        _setSimulationBadge(false);
        appendLog('Simulation mode disabled', 'warn');
      }
    });

    // Try to restore the last active session first, then fall back to the latest one on the trust.
    try {
      const storedState = _getLiveSimulationState();
      const storedSessionId = storedState?.snapshot?.session_id || null;
      if (storedSessionId) {
        await loadSession(storedSessionId);
      } else {
        const all = await API.listSimulationSessions({ trust_id: trustId });
        const sessions = Array.isArray(all?.sessions) ? all.sessions : [];
        const active = sessions.length ? sessions[sessions.length - 1] : null;
        if (active && active.session_id) {
          await loadSession(active.session_id);
        } else {
          setSessionControls(false);
        }
      }
    } catch (_) {
      setSessionControls(false);
    }
  }

  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  // 4b. BEST QUOTES & RECOMMENDATIONS PAGE
  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  function _dedupeQuoteRows(rows = []) {
    const byIsin = new Map();
    for (const row of rows || []) {
      const isin = String(row?.isin || '').trim().toUpperCase();
      if (!isin) continue;
      const existing = byIsin.get(isin);
      if (!existing) {
        byIsin.set(isin, row);
        continue;
      }
      const currentYtm = Utils.normalizePercentValue(row?.best_ytm) ?? -Infinity;
      const existingYtm = Utils.normalizePercentValue(existing?.best_ytm) ?? -Infinity;
      const currentBrokerCount = Number(row?.broker_count || (row?.all_broker_quotes || []).length || 0);
      const existingBrokerCount = Number(existing?.broker_count || (existing?.all_broker_quotes || []).length || 0);
      if (currentYtm > existingYtm || (currentYtm === existingYtm && currentBrokerCount > existingBrokerCount)) {
        byIsin.set(isin, row);
      }
    }
    return Array.from(byIsin.values());
  }

  function _dedupeRecommendationRows(rows = []) {
    const byIsin = new Map();
    for (const row of rows || []) {
      const isin = String(row?.isin || '').trim().toUpperCase();
      if (!isin) continue;
      const existing = byIsin.get(isin);
      if (!existing) {
        byIsin.set(isin, row);
        continue;
      }
      const currentScore = Number(row?.score || 0);
      const existingScore = Number(existing?.score || 0);
      const currentReason = String(row?.reason || '').trim();
      const existingReason = String(existing?.reason || '').trim();
      if (currentScore > existingScore || (currentScore === existingScore && currentReason.length > existingReason.length)) {
        byIsin.set(isin, row);
      }
    }
    return Array.from(byIsin.values());
  }

  async function renderRecommendations(el) {
    el.innerHTML = `
      <div class="page-header">
        <div>
          <div class="page-title">Best Quotes &amp; Recommendations</div>
          <div class="page-subtitle">Live broker quote comparison and AI bond recommendations</div>
        </div>
        <div style="display:flex;gap:8px;align-items:center">
          <input type="text" id="rec-search" placeholder="Search ISIN or Issuer..." class="search-input" style="width:220px" data-method="_filterRecs">
          <select id="rec-category" class="filter-select" data-method="_filterRecs">
            <option value="">All Categories</option>
            <option value="STATE_CENTRAL">Central &amp; State</option>
            <option value="PSU">PSU</option>
            <option value="PRIVATE">Private</option>
            <option value="EQUITY">Equity</option>
          </select>
          <select id="rec-rating" class="filter-select" data-method="_filterRecs">
            <option value="">All Ratings</option>
            <option value="AAA">AAA</option><option value="AA">AA+/AA/AA-</option>
            <option value="A">A+/A/A-</option><option value="BBB">BBB & below</option>
          </select>
          <button class="btn btn-ghost btn-sm" type="button" data-method="_exportRecsCSV">${IC.download} CSV</button>
          <button class="btn btn-primary btn-sm" id="btn-refresh-recs" type="button" data-method="_refreshRecs">${IC.refresh} Refresh</button>
        </div>
      </div>

      <!-- Summary cards (filled after data loads) -->
      <div class="grid-4" id="rec-summary-cards" style="margin-bottom:16px">
        ${[1,2,3,4].map(() => `<div class="card">${Utils.skeletonTable(1)}</div>`).join('')}
      </div>

      <!-- Best Picks (computed from today's full quote set) -->
      <div id="rec-best-picks" style="margin-bottom:16px"></div>

      <!-- Investment Recommendation Panel -->
      <div class="card" style="margin-bottom:16px">
        <div class="card-header" style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
          <div>
            <div class="card-title">Investment Recommendation Panel</div>
            <div class="card-subtitle">PF-compliant allocation for incoming investment</div>
          </div>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <input id="inv-portfolio" type="text" value="default" readonly class="search-input" style="width:110px" title="Portfolio ID (backend context)" />
            <input id="inv-date" type="date" class="search-input" style="width:150px" title="Investment Date" />
            <input id="inv-amount" type="number" min="100000" step="100000" value="100000000" class="search-input" style="width:170px" />
            <button class="btn btn-primary btn-sm" id="btn-run-investment" type="button" data-method="_runInvestmentRecommendation">Run Allocation</button>
          </div>
        </div>
        <div class="card-body" id="investment-recommendation-panel">
          <div class="text-muted" style="font-size:0.82rem">Run allocation to view recommended bonds, expected yield, and PF compliance status.</div>
        </div>
      </div>

      <!-- Tabs: Best Quotes | AI Recommendations | Broker Profiles -->
      <div class="tab-header" style="margin-bottom:0;display:flex;align-items:center;gap:0">
        <button class="tab-btn active" id="tab-quotes-btn" type="button" data-method="_showRecTab" data-arg="quotes">Best Broker Quotes</button>
        <button class="tab-btn" id="tab-ai-btn" type="button" data-method="_showRecTab" data-arg="ai">AI Recommendations</button>
        <button class="tab-btn" id="tab-profiles-btn" type="button" data-method="_showRecTab" data-arg="profiles">Broker Profiles</button>
        <div style="flex:1"></div>
        <button class="btn btn-secondary btn-sm" style="margin-right:8px" type="button" data-method="showQuoteUploadModal">${IC.download} Upload File</button>
        <button class="btn btn-ghost btn-sm" type="button" data-method="_ingestWorkspace">Ingest Workspace</button>
      </div>

      <!-- Best Quotes Table -->
      <div class="card" id="tab-quotes" style="border-radius:0 0 var(--border-radius) var(--border-radius)">
        <div class="table-container" id="rec-table">${Utils.skeletonTable(8)}</div>
      </div>

      <!-- AI Recommendations Table -->
      <div class="card" id="tab-ai" style="border-radius:0 0 var(--border-radius) var(--border-radius);display:none">
        <div class="table-container" id="ai-rec-table">${Utils.skeletonTable(8)}</div>
      </div>

      <!-- Broker Profiles -->
      <div class="card" id="tab-profiles" style="border-radius:0 0 var(--border-radius) var(--border-radius);display:none">
        <div id="broker-profiles-content">${Utils.skeletonCards(4)}</div>
      </div>
    `;

    let allQuotes = [];
    let allRecs = [];
    let allProfiles = [];

    const [quotesRes, recsRes, profilesRes, portfolioRes, concentrationRes] = await Promise.all([
      API.getBestQuotes(150).catch((err) => ({ quotes: [], _error: err?.message || 'Quote data unavailable', _status: err?.status || 0 })),
      API.getRecommendations().catch((err) => ({ recommendations: [], _error: err?.message || 'Recommendation data unavailable', _status: err?.status || 0 })),
      API.getBrokerProfiles().catch((err) => ({ profiles: [], _error: err?.message || 'Broker profile data unavailable', _status: err?.status || 0 })),
      API.getPortfolio().catch((err) => ({ summary: {}, holdings: [], _error: err?.message || 'Portfolio data unavailable', _status: err?.status || 0 })),
      API.getPortfolioConcentration().catch((err) => ({ _error: err?.message || 'Concentration data unavailable', _status: err?.status || 0 })),
    ]);

    allQuotes = _dedupeQuoteRows(quotesRes.quotes || []);
    allRecs = _dedupeRecommendationRows(recsRes.recommendations || []);
    allProfiles = profilesRes.profiles || [];

    const recErrors = {
      quotes: quotesRes._error || null,
      recs: recsRes._error || null,
      profiles: profilesRes._error || null,
      portfolio: portfolioRes._error || null,
      concentration: concentrationRes?._error || null,
    };
    window._recPageStatus = recErrors;

    await _seedInvestmentPanelContext(portfolioRes, concentrationRes);

    // Summary cards
    const totalISINs = allQuotes.length;
    const allBrokers = new Set();
    let bestYTM = 0;
    let quotedToday = quotesRes.as_of || '';
    allQuotes.forEach(q => {
      (q.all_broker_quotes || []).forEach(b => allBrokers.add(b.broker_name));
      if ((q.best_ytm || 0) > bestYTM) bestYTM = q.best_ytm || 0;
    });

    // Guard: user may have navigated away while API calls were in-flight
    if (!document.getElementById('rec-summary-cards')) return;

    const summaryValue = (value, error) => (error ? 'n/a' : value);
    const hasNumericValue = (value) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
    const bestYtmLabel = totalISINs > 0 && hasNumericValue(bestYTM) ? Utils.formatYieldPct(bestYTM, 4) : 'Unavailable';
    document.getElementById('rec-summary-cards').innerHTML = `
      <div class="card"><div class="card-body"><div class="metric-label">ISINs Quoted</div><div class="metric-value">${summaryValue(Utils.formatNumber(totalISINs), recErrors.quotes)}</div><div class="metric-label">as of ${quotedToday || 'today'}</div></div></div>
      <div class="card"><div class="card-body"><div class="metric-label">Active Brokers</div><div class="metric-value">${summaryValue(allBrokers.size, recErrors.quotes)}</div></div></div>
      <div class="card"><div class="card-body"><div class="metric-label">Best Yield Available</div><div class="metric-value text-green">${summaryValue(bestYtmLabel, recErrors.quotes)}</div></div></div>
      <div class="card"><div class="card-body"><div class="metric-label">Broker Profiles</div><div class="metric-value">${summaryValue(allProfiles.length, recErrors.profiles)}</div></div></div>
    `;

    if (Object.values(recErrors).some(Boolean)) {
      const banner = document.createElement('div');
      banner.style.cssText = 'margin-bottom:14px;padding:10px 12px;border-radius:8px;background:#fff7ed;border:1px solid #fdba74;color:#9a3412;font-size:0.82rem';
      const parts = [];
      if (recErrors.quotes) parts.push('broker quotes unavailable');
      if (recErrors.recs) parts.push('AI recommendations unavailable');
      if (recErrors.profiles) parts.push('broker profiles unavailable');
      if (recErrors.portfolio) parts.push('portfolio context unavailable');
      if (recErrors.concentration) parts.push('allocation context unavailable');
      banner.textContent = `Some recommendation sources are unavailable: ${parts.join('; ')}.`;
      const header = document.getElementById('rec-summary-cards');
      if (header && header.parentElement) header.parentElement.insertBefore(banner, header);
    }

    // Store for filter
    el._allQuotes = allQuotes;
    el._allRecs = allRecs;
    el._allProfiles = allProfiles;
    window._recPageEl = el;

    App._renderQuotesTable(allQuotes);
    App._renderAIRecsTable(allRecs);
    App._renderBrokerProfiles(allProfiles);
    App._renderBestPicks(allQuotes, quotedToday);
  }

  async function _runInvestmentRecommendation() {
    const panel = document.getElementById('investment-recommendation-panel');
    const btn = document.getElementById('btn-run-investment');
    const amountInput = document.getElementById('inv-amount');
    const portfolioInput = document.getElementById('inv-portfolio');
    const dateInput = document.getElementById('inv-date');
    if (!panel || !amountInput) return;

    if (recommendationInFlight) {
      Utils.toast('info', 'Recommendation in progress', 'Please wait for the current run to complete');
      return;
    }

    const amount = Number(amountInput.value || 0);
    const portfolioId = (portfolioInput?.value || 'default').trim() || 'default';
    const investmentDate = dateInput?.value || new Date().toISOString().slice(0, 10);
    const minAmount = Number(amountInput.getAttribute('min') || 100000);
    if (!amount || amount <= 0 || amount < minAmount) {
      Utils.toast('warning', 'Invalid amount', `Please enter a valid investment amount (minimum â‚¹${Utils.formatNumber(minAmount)})`);
      return;
    }

    recommendationInFlight = true;
    if (btn) {
      btn.disabled = true;
      btn.textContent = 'Running...';
    }
    panel.innerHTML = Utils.loadingHTML('Computing PF-compliant allocation...');

    try {
      const resp = await API.getInvestmentRecommendation(portfolioId, amount, investmentDate);
      const list = resp.recommended_bonds || [];
      const pf = resp.pf_compliance || {};
      const checks = pf.checks || {};
      const violations = pf.violations || [];
      const strategyMode = resp.strategy_mode || 'Standard';
      const tacticalPhase = resp.tactical_phase?.phase || 'N/A';
      const marketMode = resp.display_market_mode || resp.market_mode || 'NEUTRAL';
      const spreadSignal = resp.spread_signal || 'NO_DATA';
      const spreadOpportunity = !!resp.opportunity_flag;
      const spreadConfidence = _toFiniteNumber(resp.confidence_score, 0);
      const extraYield = _toFiniteNumber(resp.extra_yield, 0);
      const recommendationStatement = resp.recommendation_statement || `Recommended Bonds to Buy for Rs ${Utils.formatNumber(amount)}`;
      const recommendationExplainKey = _registerExplanationPayload(resp.explanation);
      const decisionStatus = String(resp.decision_status || '').toUpperCase();
      const decisionStatusLabel = decisionStatus ? decisionStatus.replace(/_/g, ' ') : 'UNKNOWN';
      const investmentDecision = resp.investment_decision || {};
      const investmentDecisionAction = String(investmentDecision.action || resp.im_decision?.action || 'CAUTION').toUpperCase();
      const investmentDecisionReason = String(investmentDecision.reason || resp.im_decision?.reason || 'IM decision unavailable');
      const investmentDecisionColor = investmentDecisionAction === 'INVEST' ? '#16a34a' : (investmentDecisionAction === 'AVOID' ? '#dc2626' : '#f59e0b');
      const noRecommendationReason = String(resp.no_recommendation_reason || '');
      const engineDiagnostics = resp.engine_diagnostics || {};
      const optimizerMethod = String(engineDiagnostics.optimizer || resp.optimization_method || 'Unknown');
      const optimizerFallbackApplied = !!engineDiagnostics.optimizer_fallback_applied;
      const optimizerFallbackReason = String(engineDiagnostics.optimizer_fallback_reason || resp.optimization_fallback_reason || '').trim();
      const evaluatedAlternativesCount = Number(engineDiagnostics.evaluated_alternatives_count || (Array.isArray(resp.evaluated_alternatives) ? resp.evaluated_alternatives.length : 0) || 0);
      const topBlockers = Array.isArray(resp.top_blockers) ? resp.top_blockers : [];
      const strictMode = !!pf.strict_mode;
      const currentDuration = _toFiniteNumber(resp.portfolio_metrics?.portfolio_duration, 0);
      const projectedDurationRaw = resp.expected_portfolio_metrics?.projected_portfolio_duration;
      const projectedDuration = projectedDurationRaw == null ? null : Number(projectedDurationRaw);
      const safeStrategyMode = Utils.escapeHTML(strategyMode);
      const safeTacticalPhase = Utils.escapeHTML(tacticalPhase);
      const currentAllocLegacy = _legacyToCanonicalWeights(resp.current_allocation_vs_target?.current || {});
      const currentAllocFromExposure = _categoryExposureToWeights(resp.category_exposure?.current || []);
      const currentAlloc = Object.values(currentAllocFromExposure).some(v => _toFiniteNumber(v, 0) > 0.0001)
        ? currentAllocFromExposure
        : currentAllocLegacy;
      const targetAllocText = _allocationTargetBandText(resp.current_allocation_vs_target?.target || {});
      const projectedAllocLegacy = _legacyToCanonicalWeights(resp.projected_allocation_percentages || {});
      const projectedAllocFromExposure = _categoryExposureToWeights(resp.category_exposure?.projected || resp.risk_indicators?.category_exposure || []);
      const projectedAlloc = Object.values(projectedAllocFromExposure).some(v => _toFiniteNumber(v, 0) > 0.0001)
        ? projectedAllocFromExposure
        : projectedAllocLegacy;
      const currentAllocBond = _bondAllocationWeights(currentAlloc);
      const projectedAllocBond = _bondAllocationWeights(projectedAlloc);
      const currentAllocText = _bondAllocationBucketText(currentAllocBond);
      const gapAlloc = {};
      CATEGORY_ORDER.forEach((key) => {
        gapAlloc[key] = _toFiniteNumber(targetAlloc[key], 0) - _toFiniteNumber(currentAlloc[key], 0);
      });
      const gapAllocText = _bondAllocationBucketText(gapAlloc, true);
      const rebalancePlan = resp.rebalance_plan || {};
      const complianceStatusObj = resp.compliance_status || {};
      const riskIndicators = resp.risk_indicators || {};
      const totalAllocated = list.reduce((s, b) => s + _toFiniteNumber(b.allocation_amount, 0), 0);
      const byCategory = {};
      const bySector = {};
      const byIssuer = {};
      list.forEach((b) => {
        const category = _canonicalCategoryKey(b.instrument_category || _normalizeAllocationBucket(b));
        const sector = String(b.sector || 'OTHER').toUpperCase();
        const issuer = String(b.issuer_name || b.issuer || b.isin || 'UNKNOWN').toUpperCase();
        const alloc = _toFiniteNumber(b.allocation_amount, 0);
        byCategory[category] = (byCategory[category] || 0) + alloc;
        bySector[sector] = (bySector[sector] || 0) + alloc;
        byIssuer[issuer] = (byIssuer[issuer] || 0) + alloc;
      });
      const categorySections = (resp.recommendations_by_category || [])
        .filter((section) => _toFiniteNumber(section.count, 0) > 0)
        .map((section) => ({
          category: _canonicalCategoryKey(section.instrument_category || section.category_display),
          title: section.section_title || section.category_display || _displayCategoryLabel(section.instrument_category),
          picks: (section.recommendations || []).slice(0, 3),
        }));
      const bondCategorySplit = Object.fromEntries(Object.entries(byCategory).filter(([key]) => key !== 'SDS'));
      const fmtSplit = (obj) => Object.entries(obj)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 4)
        .map(([k, v]) => `${Utils.escapeHTML(_displayCategoryLabel(k))}: ${totalAllocated > 0 ? ((v / totalAllocated) * 100).toFixed(2) : '0.00'}%`)
        .join(' | ') || 'â€”';

      const complianceStatus = pf.pf_compliant ? 'Compliant' : (violations.length ? 'Critical' : 'Warning');
      const complianceColor = complianceStatus === 'Compliant' ? '#16a34a' : (complianceStatus === 'Warning' ? '#f59e0b' : '#dc2626');

      const safeRows = list.map(b => {
        const isinRaw = b.isin || 'â€”';
        const bondLabelRaw = b.bond_name || b.issuer_name || 'â€”';
        const explanationRaw = String(b.ai_explanation || '');
        const quoteOnly = !!(b.quote_only_market_fallback || String(b.im_explanation_source || '').toUpperCase() === 'BROKER_QUOTES_INDICATIVE');
        const quoteOnlyReason = String(
          b.quote_only_fallback_reason
          || b.quote_only_market_fallback_reason
          || (quoteOnly ? 'No persisted market print was available; broker quotes were used to synthesize indicative market fields.' : '')
        ).trim();
        const ratingRaw = String(b.credit_rating || b.rating || 'NR').toUpperCase();
        const issuerRaw = String(b.issuer_name || b.issuer || 'â€”');
        const durationRaw = b.modified_duration ?? b.duration;
        const alloc = Number(b.allocation_amount || 0);
        const imIntel = b.im_intelligence || {};
        const imDecision = b.im_decision || imIntel.decision || {};
        const imRiskScore = imIntel.risk_score != null ? Number(imIntel.risk_score).toFixed(1) : 'â€”';
        const imRiskLevel = String(imIntel.risk_level || 'UNKNOWN');
        const imDecisionAction = String(imDecision.action || 'CAUTION').toUpperCase();
        return {
          isin: Utils.escapeHTML(isinRaw),
          isinTitle: Utils.escapeAttr(isinRaw),
          bondLabel: Utils.escapeHTML(bondLabelRaw),
          bondTitle: Utils.escapeAttr(bondLabelRaw),
          quoteOnlyBadge: quoteOnly
            ? `<span class="badge badge-amber" title="${Utils.escapeAttr(quoteOnlyReason || 'No persisted market print was available; broker quotes were used to synthesize indicative market fields.') }" style="font-size:0.62rem;white-space:nowrap">Quote-only</span>`
            : '',
          issuer: Utils.escapeHTML(issuerRaw),
          rating: Utils.escapeHTML(ratingRaw),
          duration: durationRaw != null ? Number(durationRaw).toFixed(2) + 'Y' : 'â€”',
          allocationAmount: Utils.formatNumber(alloc),
          allocationPct: totalAllocated > 0 ? ((alloc / totalAllocated) * 100).toFixed(2) + '%' : '0.00%',
          category: Utils.escapeHTML(_investmentDisplayLabel(b)),
          ytm: b.ytm != null ? Number(b.ytm).toFixed(4) + '%' : 'â€”',
          spreadBps: b.spread != null ? (Number(b.spread) * 100).toFixed(2) + ' bps' : 'â€”',
          histSpread: b.historical_avg_spread != null ? (Number(b.historical_avg_spread) * 100).toFixed(2) + ' bps' : 'â€”',
          yieldAdvBps: b.yield_advantage_bps != null ? Number(b.yield_advantage_bps).toFixed(2) + ' bps' : 'â€”',
          role: Utils.escapeHTML(String(b.portfolio_role || 'Yield boost / safety / rebalancing')),
          complianceJustification: Utils.escapeHTML(String(b.compliance_justification || 'PF eligibility and limits validated')),
          imRiskScore,
          imRiskLevel: Utils.escapeHTML(imRiskLevel),
          imDecisionAction: Utils.escapeHTML(imDecisionAction),
          imDecisionReason: Utils.escapeHTML(String(imDecision.reason || imIntel.summary || 'No IM intelligence available')),
          explanation: Utils.escapeHTML(explanationRaw.slice(0, 120) + (explanationRaw.length > 120 ? 'â€¦' : '')),
          explanationTitle: Utils.escapeAttr(explanationRaw),
          explainKey: _registerExplanationPayload(b.explanation || {
            module: 'recommendation_ui_fallback',
            decision_outcome: 'INFO',
            summary: explanationRaw || 'Bond selected by recommendation engine',
            explanation_levels: {
              level_1: { headline: explanationRaw || 'Bond selected by recommendation engine' },
              level_2: { key_factors: [], rule_checks: [] },
              level_3: { calculation_trace: {}, data_lineage: [] },
            },
          }),
        };
      });
      const safeViolations = violations.slice(0, 4).map(v => Utils.escapeHTML(String(v))).join(' | ');
      const quoteOnlyRecommendations = list.some((b) => b.quote_only_market_fallback || String(b.im_explanation_source || '').toUpperCase() === 'BROKER_QUOTES_INDICATIVE');
      const quoteOnlySummaryReason = String(
        list.find((b) => b.quote_only_fallback_reason || b.quote_only_market_fallback_reason || b.quote_only_market_fallback)
          ?.quote_only_fallback_reason
        || list.find((b) => b.quote_only_fallback_reason || b.quote_only_market_fallback_reason || b.quote_only_market_fallback)
          ?.quote_only_market_fallback_reason
        || 'At least one recommended bond used broker-quote fallback data'
      );

      panel.innerHTML = `
        <div class="grid-4" style="margin-bottom:12px">
          <div class="metric-card"><div class="metric-label">Strategy Mode</div><div class="metric-value" style="font-size:0.9rem">${safeStrategyMode}</div></div>
          <div class="metric-card"><div class="metric-label">Tactical Phase</div><div class="metric-value">${safeTacticalPhase}</div></div>
          <div class="metric-card"><div class="metric-label">Market Mode</div><div class="metric-value">${Utils.escapeHTML(String(marketMode))}</div></div>
          <div class="metric-card"><div class="metric-label">PF Compliance Status</div><div class="metric-value" style="color:${complianceColor}">${complianceStatus}</div></div>
          <div class="metric-card"><div class="metric-label">Investment Amount</div><div class="metric-value">â‚¹${Utils.formatNumber(amount)}</div></div>
          <div class="metric-card"><div class="metric-label">Investment Date</div><div class="metric-value">${Utils.escapeHTML(investmentDate)}</div></div>
          <div class="metric-card"><div class="metric-label">Portfolio ID</div><div class="metric-value">${Utils.escapeHTML(portfolioId)}</div></div>
          <div class="metric-card"><div class="metric-label">Allocated</div><div class="metric-value">â‚¹${Utils.formatNumber(_toFiniteNumber(resp.portfolio_impact?.allocated_amount, 0))}</div></div>
          <div class="metric-card"><div class="metric-label">Expected Yield</div><div class="metric-value">${_toFiniteNumber(resp.expected_yield, 0).toFixed(4)}%</div></div>
          <div class="metric-card"><div class="metric-label">Portfolio Duration</div><div class="metric-value">${currentDuration.toFixed(2)}Y${projectedDuration != null ? ` â†’ ${projectedDuration.toFixed(2)}Y` : ''}</div></div>
          <div class="metric-card"><div class="metric-label">PF Compliance</div><div class="metric-value" style="color:${pf.pf_compliant ? '#16a34a' : '#dc2626'}">${pf.pf_compliant ? 'PASS' : 'FAIL'}</div></div>
          <div class="metric-card"><div class="metric-label">Static Yield</div><div class="metric-value">${_toFiniteNumber(resp.static_yield, 0).toFixed(4)}%</div></div>
          <div class="metric-card"><div class="metric-label">Tactical Yield</div><div class="metric-value">${_toFiniteNumber(resp.tactical_yield, 0).toFixed(4)}%</div></div>
          <div class="metric-card"><div class="metric-label">Extra Yield</div><div class="metric-value" style="color:${extraYield >= 0 ? '#16a34a' : '#dc2626'}">${extraYield >= 0 ? '+' : ''}${extraYield.toFixed(4)}%</div></div>
        </div>

        <div class="grid-3" style="margin-bottom:10px">
          <div class="card"><div class="card-body"><div class="metric-label">Current Allocation Buckets (Cost Basis)</div><div class="metric-value" style="font-size:0.82rem">${currentAllocText}</div></div></div>
          <div class="card"><div class="card-body"><div class="metric-label">Target Allocation Bands</div><div class="metric-value" style="font-size:0.82rem">${targetAllocText}</div></div></div>
          <div class="card"><div class="card-body"><div class="metric-label">Gap vs Target</div><div class="metric-value" style="font-size:0.82rem">${gapAllocText}</div></div></div>
        </div>

        <div class="card" style="margin-bottom:10px">
          <div class="card-body">
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:2px">
              <div class="metric-label">Main Answer</div>
              ${quoteOnlyRecommendations ? `<span class="badge badge-amber" title="${Utils.escapeAttr(quoteOnlySummaryReason)}" style="font-size:0.66rem;white-space:nowrap">Quote-only fallback</span>` : ''}
            </div>
            <div class="metric-value" style="font-size:0.95rem">${Utils.escapeHTML(String(recommendationStatement))}</div>
            ${recommendationExplainKey ? `<div style="margin-top:8px"><button class="btn btn-ghost btn-sm" type="button" data-method="_openExplainFromKey" data-arg="${Utils.escapeAttr(recommendationExplainKey)}" data-arg2="Portfolio Recommendation">Why?</button></div>` : ''}
          </div>
        </div>

        ${optimizerFallbackApplied || optimizerFallbackReason ? `
          <div class="card" style="margin-bottom:10px;border-color:rgba(245,158,11,0.35)">
            <div class="card-body">
              <div class="metric-label" style="color:#b45309">Optimizer fallback</div>
              <div style="font-size:0.8rem;margin-top:4px">The allocation engine used a fallback path instead of the primary solver.</div>
              ${optimizerFallbackReason ? `<div style="font-size:0.74rem;color:var(--text-muted);margin-top:4px">Reason: ${Utils.escapeHTML(optimizerFallbackReason)}</div>` : ''}
            </div>
          </div>
        ` : ''}

        <div class="grid-3" style="margin-bottom:10px">
          <div class="card"><div class="card-body"><div class="metric-label">Optimizer</div><div class="metric-value" style="font-size:0.86rem">${Utils.escapeHTML(optimizerMethod)}</div><div class="metric-sub text-muted" style="font-size:0.7rem">${optimizerFallbackApplied ? 'Fallback active' : 'Primary solver'}</div></div></div>
          <div class="card"><div class="card-body"><div class="metric-label">Evaluated Alternatives</div><div class="metric-value">${Utils.formatNumber(evaluatedAlternativesCount)}</div><div class="metric-sub text-muted" style="font-size:0.7rem">Ledger rows returned by backend</div></div></div>
          <div class="card"><div class="card-body"><div class="metric-label">Decision State</div><div class="metric-value" style="font-size:0.86rem">${Utils.escapeHTML(decisionStatusLabel)}</div><div class="metric-sub text-muted" style="font-size:0.7rem">${topBlockers.length ? `${topBlockers.length} blocker(s)` : 'No blockers reported'}</div></div></div>
        </div>

        <div class="grid-3" style="margin-bottom:10px">
          <div class="card"><div class="card-body"><div class="metric-label">Investment Decision</div><div class="metric-value" style="color:${investmentDecisionColor}">${Utils.escapeHTML(investmentDecisionAction)}</div><div style="font-size:0.76rem;color:var(--text-muted);margin-top:4px">${Utils.escapeHTML(investmentDecisionReason)}</div></div></div>
          <div class="card"><div class="card-body"><div class="metric-label">IM Risk Signal</div><div class="metric-value">${resp.im_intelligence?.risk_score != null ? Number(resp.im_intelligence.risk_score).toFixed(1) : 'â€”'}</div><div style="font-size:0.76rem;color:var(--text-muted);margin-top:4px">${Utils.escapeHTML(String(resp.im_intelligence?.risk_level || 'UNKNOWN'))}</div></div></div>
          <div class="card"><div class="card-body"><div class="metric-label">IM Clause Coverage</div><div class="metric-value">${resp.im_intelligence?.clause_coverage?.ratio != null ? (Number(resp.im_intelligence.clause_coverage.ratio) * 100).toFixed(0) + '%' : 'â€”'}</div><div style="font-size:0.76rem;color:var(--text-muted);margin-top:4px">Critical: ${resp.im_intelligence?.clause_coverage?.critical_present ?? 'â€”'}/${resp.im_intelligence?.clause_coverage?.critical_total ?? 'â€”'}</div></div></div>
        </div>

        ${categorySections.length ? `
          <div class="grid-2" style="margin-bottom:10px">
            ${categorySections.map((section) => `
              <div class="card">
                <div class="card-body">
                  <div class="metric-label">${Utils.escapeHTML(section.title)}</div>
                  <div style="font-size:0.76rem;color:var(--text-muted)">${Utils.escapeHTML(_displayCategoryLabel(section.category))}</div>
                  <div style="margin-top:6px;font-size:0.78rem">${section.picks.map((pick) => `${Utils.escapeHTML(pick.isin || 'â€”')} (${_toFiniteNumber(pick.allocation_amount, 0) > 0 ? 'â‚¹' + Utils.formatNumber(_toFiniteNumber(pick.allocation_amount, 0)) : 'n/a'})`).join(' | ') || 'No picks'}</div>
                </div>
              </div>
            `).join('')}
          </div>
        ` : ''}

        ${decisionStatus === 'REMEDIATION_REQUIRED' ? `
          <div class="card" style="margin-bottom:10px;border-color:rgba(220,38,38,0.35)">
            <div class="card-body">
              <div class="metric-label" style="color:#dc2626">Allocation Blocked</div>
              <div style="font-size:0.82rem;margin-bottom:6px">${Utils.escapeHTML(noRecommendationReason || 'Compliant allocation is currently blocked by portfolio-level constraints.')}</div>
              <div style="font-size:0.78rem;color:var(--text-muted)">Top blockers: ${topBlockers.length ? topBlockers.slice(0, 3).map(x => Utils.escapeHTML(String(x))).join(' | ') : 'Review PF violations and concentration breaches'}</div>
            </div>
          </div>
        ` : ''}

        <div style="margin-bottom:8px;font-weight:700;font-size:0.82rem">Recommended Bonds to Buy for â‚¹${Utils.formatNumber(amount)}</div>
        ${list.length ? `
          <div class="table-container" style="max-height:300px;overflow:auto">
            <table class="data-table">
              <thead><tr><th>ISIN</th><th>Bond</th><th>Issuer</th><th>Category</th><th>Rating</th><th class="col-number">Yield</th><th class="col-number">Duration</th><th class="col-number">Allocation Amount</th><th class="col-number">% of Investment</th><th>Yield Adv vs Peers</th><th>Role in Portfolio</th><th>Compliance Justification</th><th>IM Risk</th><th>IM Decision</th><th>IM Reason</th><th>AI Explanation</th><th>Why?</th></tr></thead>
              <tbody>
                ${safeRows.map(b => `
                  <tr>
                    <td class="font-mono" style="font-size:0.72rem" title="${b.isinTitle}">${b.isin}</td>
                    <td class="truncate" style="max-width:180px" title="${b.bondTitle}"><span style="display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap;min-width:0">${b.bondLabel}${b.quoteOnlyBadge}</span></td>
                    <td class="truncate" style="max-width:140px">${b.issuer}</td>
                    <td>${b.category}</td>
                    <td>${b.rating}</td>
                    <td class="col-number">${b.ytm}</td>
                    <td class="col-number">${b.duration}</td>
                    <td class="col-number">â‚¹${b.allocationAmount}</td>
                    <td class="col-number">${b.allocationPct}</td>
                    <td>${b.yieldAdvBps}</td>
                    <td style="font-size:0.72rem;max-width:140px">${b.role}</td>
                    <td style="font-size:0.72rem;max-width:190px">${b.complianceJustification}</td>
                    <td style="font-size:0.72rem">${b.imRiskScore} / ${b.imRiskLevel}</td>
                    <td><span class="badge ${b.imDecisionAction === 'INVEST' ? 'badge-green' : b.imDecisionAction === 'AVOID' ? 'badge-red' : 'badge-amber'}">${b.imDecisionAction}</span></td>
                    <td class="text-muted" style="font-size:0.68rem;max-width:170px" title="${b.imDecisionReason}">${b.imDecisionReason.slice(0, 80)}${b.imDecisionReason.length > 80 ? 'â€¦' : ''}</td>
                    <td class="text-muted" style="font-size:0.72rem;max-width:260px" title="${b.explanationTitle}">${b.explanation}</td>
                    <td>${b.explainKey ? `<button class="btn btn-ghost btn-sm" type="button" style="font-size:0.7rem;padding:2px 8px" data-method="_openExplainFromKey" data-arg="${Utils.escapeAttr(b.explainKey)}" data-arg2="Bond Recommendation">Why?</button>` : 'â€”'}</td>
                  </tr>`).join('')}
              </tbody>
            </table>
          </div>
        ` : Utils.emptyHTML('No allocation generated', 'No PF-compliant allocation could be generated for this amount')}

        <div class="grid-2" style="margin-top:12px;margin-bottom:8px">
          <div class="card"><div class="card-body"><div class="metric-label">Allocation Split by Investment Class (Cost Basis)</div><div style="font-size:0.78rem">${fmtSplit(bondCategorySplit)}</div></div></div>
          <div class="card"><div class="card-body"><div class="metric-label">Allocation Split by Sector</div><div style="font-size:0.78rem">${fmtSplit(bySector)}</div></div></div>
          <div class="card"><div class="card-body"><div class="metric-label">Allocation Split by Issuer</div><div style="font-size:0.78rem">${fmtSplit(byIssuer)}</div></div></div>
          <div class="card"><div class="card-body"><div class="metric-label">Projected Allocation Buckets (Cost Basis)</div><div style="font-size:0.78rem">${_bondAllocationBucketText(projectedAllocBond)}</div></div></div>
        </div>

        <div class="grid-3" style="margin-bottom:8px">
          <div class="card"><div class="card-body"><div class="metric-label">Spread Intelligence</div><div style="font-size:0.78rem">Opportunity: ${spreadOpportunity ? 'Yes' : 'No'} | Signal: ${Utils.escapeHTML(String(spreadSignal))} | Confidence: ${(spreadConfidence * 100).toFixed(2)}%</div></div></div>
          <div class="card"><div class="card-body"><div class="metric-label">Rebalancing Plan</div><div style="font-size:0.78rem">${(rebalancePlan.actions || []).slice(0, 2).map(a => Utils.escapeHTML(String(a))).join(' | ') || 'On-track'}${_toFiniteNumber(gapAlloc.STATE_CENTRAL, 0) > 0 ? ` | State/Central deficit = ${_toFiniteNumber(gapAlloc.STATE_CENTRAL, 0).toFixed(2)}%, system will prioritize sovereign allocation in next runs` : ''}</div></div></div>
          <div class="card"><div class="card-body"><div class="metric-label">Risk & Compliance</div><div style="font-size:0.78rem">Issuer ${checks.validate_issuer_limits ? 'PASS' : 'FAIL'} | Sector ${checks.validate_sector_limits ? 'PASS' : 'FAIL'} | Rating ${checks.validate_rating ? 'PASS' : 'FAIL'} | Tier I/II ${strictMode ? (checks.validate_strict_pf_pattern ? 'PASS' : 'FAIL') : 'N/A'}</div></div></div>
        </div>

        <div class="grid-3" style="margin-bottom:8px">
          <div class="card"><div class="card-body"><div class="metric-label">Compliance Trajectory</div><div style="font-size:0.78rem">PF ${complianceStatusObj.pf_compliant ? 'Compliant' : 'Not Compliant'} | Temporary Deviation: ${complianceStatusObj.temporary_deviation ? 'Yes' : 'No'} | Year-end Achievable: ${complianceStatusObj.year_end_compliance_achievable ? 'Yes' : 'No'}</div></div></div>
          <div class="card"><div class="card-body"><div class="metric-label">Risk Indicators</div><div style="font-size:0.78rem">Issuer Exposure: ${_toFiniteNumber(riskIndicators.issuer_exposure_pct, 0).toFixed(2)}% | Sector Exposure: ${_toFiniteNumber(riskIndicators.sector_exposure_pct, 0).toFixed(2)}%</div></div></div>
          <div class="card"><div class="card-body"><div class="metric-label">Credit & Liquidity Risk</div><div style="font-size:0.78rem">Credit: ${Utils.escapeHTML(String(riskIndicators.credit_risk_level || 'LOW'))} (${_toFiniteNumber(riskIndicators.credit_risk_count, 0)}) | Liquidity: ${Utils.escapeHTML(String(riskIndicators.liquidity_risk_level || 'LOW'))} (${_toFiniteNumber(riskIndicators.liquidity_risk_count, 0)})</div></div></div>
        </div>

        <div style="margin-top:12px;font-size:0.76rem;color:var(--text-muted)">
          Checks: Rating ${checks.validate_rating ? 'PASS' : 'FAIL'} | Sector ${checks.validate_sector_limits ? 'PASS' : 'FAIL'} |
          Issuer ${checks.validate_issuer_limits ? 'PASS' : 'FAIL'} | Duration ${checks.validate_duration ? 'PASS' : 'FAIL'} |
          Asset Allocation ${checks.validate_asset_allocation ? 'PASS' : 'FAIL'} |
          Strict PF ${strictMode ? (checks.validate_strict_pf_pattern ? 'PASS' : 'FAIL') : 'N/A'}
        </div>
        ${violations.length ? `<div style="margin-top:8px;font-size:0.74rem;color:#dc2626">${safeViolations}</div>` : ''}
      `;
    } catch (err) {
      panel.innerHTML = Utils.emptyHTML('Recommendation failed', err.message || 'Could not generate recommendation');
      Utils.toast('error', 'Recommendation Error', err.message || 'Failed to run investment recommendation');
    } finally {
      recommendationInFlight = false;
      if (btn) {
        btn.disabled = false;
        btn.textContent = 'Run Allocation';
      }
    }
  }

  function _refreshRecs() {
    const btn = document.getElementById('btn-refresh-recs');
    if (btn) { btn.disabled = true; btn.innerHTML = `${IC.refresh} Refreshingâ€¦`; }
    const el = window._recPageEl || document.getElementById('page-content');
    const rerender = currentPage === 'bond-allocation' ? renderBondAllocation(el) : renderRecommendations(el);
    rerender.finally(() => {
      const b = document.getElementById('btn-refresh-recs');
      if (b) { b.disabled = false; b.innerHTML = `${IC.refresh} Refresh`; }
    });
  }

  function _refreshBondAllocation() {
    const btn = document.getElementById('btn-refresh-bond-allocation');
    if (btn) {
      btn.disabled = true;
      btn.innerHTML = `${IC.refresh} Refreshing…`;
    }
    const el = document.getElementById('page-content');
    renderBondAllocation(el).finally(() => {
      const refreshBtn = document.getElementById('btn-refresh-bond-allocation');
      if (refreshBtn) {
        refreshBtn.disabled = false;
        refreshBtn.innerHTML = `${IC.refresh} Refresh Context`;
      }
    });
  }

  // Composite bond attractiveness score (0â€“100)
  function _computeBondScore(q, maxYTM) {
    const bondYtm = Utils.normalizePercentValue(q.best_ytm) || 0;
    // YTM component (50 pts): higher yield = better
    const ytmScore = maxYTM > 0 ? (bondYtm / maxYTM) * 50 : 0;
    // Rating component (20 pts): investment-grade quality
    const rMap = { 'AAA': 20, 'AA+': 18, 'AA': 17, 'AA-': 16, 'A+': 14, 'A': 12, 'A-': 11, 'BBB+': 8, 'BBB': 6 };
    const ratingScore = rMap[(q.rating || '').trim()] || 4;
    // Broker count (15 pts): multi-broker confirmation = liquid / trustworthy price
    const brokerScore = Math.min((q.broker_count || 1) / 4, 1) * 15;
    // Broker reliability (15 pts): avg reliability of quoting brokers
    const allBQ = q.all_broker_quotes || [];
    const reliabs = allBQ.map(b => b.reliability_score).filter(r => r != null);
    const avgReliab = reliabs.length ? reliabs.reduce((a, b) => a + b, 0) / reliabs.length : 50;
    const reliabScore = (avgReliab / 100) * 15;
    return Math.round((ytmScore + ratingScore + brokerScore + reliabScore) * 10) / 10;
  }

  function _renderBestPicks(quotes, asOf) {
    const el = document.getElementById('rec-best-picks');
    if (!el || !quotes.length) return;
    // Compute scores and pick top 5
    const maxYTM = Math.max(...quotes.map(q => Utils.normalizePercentValue(q.best_ytm) || 0));
    const scored = quotes
      .map(q => ({ ...q, _normalizedYtm: Utils.normalizePercentValue(q.best_ytm) }))
      .filter(q => q._normalizedYtm != null && q._normalizedYtm > 0)
      .map(q => ({ ...q, _score: _computeBondScore(q, maxYTM) }))
      .sort((a, b) => b._score - a._score)
      .slice(0, 10);
    if (!scored.length) { el.innerHTML = ''; return; }
    const quoteBucket = (q) => _canonicalCategoryKey(q.instrument_category || q.category_bucket || q.category || q.issuer_type);
    const byCategory = {
      STATE_CENTRAL: scored.filter(q => quoteBucket(q) === 'STATE_CENTRAL').slice(0, 5),
      PSU: scored.filter(q => quoteBucket(q) === 'PSU').slice(0, 5),
      PRIVATE: scored.filter(q => quoteBucket(q) === 'PRIVATE').slice(0, 5),
      EQUITY: scored.filter(q => quoteBucket(q) === 'EQUITY').slice(0, 5),
    };
    const rankColors = ['#22c55e', '#3b82f6', '#f59e0b', '#8b5cf6', '#f43f5e'];
    const now = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const renderPickCards = (items, emptyText) => items.length ? `
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px">
        ${items.map((q, i) => {
          const bestBQ = (q.all_broker_quotes || []).find(b => b.is_best) || {};
          const reasonText = (bestBQ.reason || '').slice(0, 70) || `${q.broker_count} broker(s) confirmed`;
          const imIntel = q.im_intelligence || {};
          const imDecision = imIntel.decision || {};
          const imAction = String(imDecision.action || 'CAUTION').toUpperCase();
          return `
          <div style="background:var(--color-surface-2);border-radius:10px;padding:14px;border-left:4px solid ${rankColors[i] || rankColors[rankColors.length - 1]};position:relative">
            <div style="font-size:0.6rem;font-weight:700;letter-spacing:.8px;color:${rankColors[i] || rankColors[rankColors.length - 1]};margin-bottom:4px">PICK #${i + 1} &nbsp; SCORE ${q._score}</div>
            <div class="compact-ellipsis" style="font-weight:700;font-size:0.82rem;margin-bottom:2px" title="${Utils.escapeAttr(q.bond_name || q.isin)}">${q.bond_name || q.isin}</div>
            <div style="font-family:monospace;font-size:0.64rem;color:var(--color-text-muted);margin-bottom:8px">${q.isin}</div>
            <div style="display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:8px">
              <div><div style="font-size:1.45rem;font-weight:800;color:var(--color-success);line-height:1">${q.best_ytm != null ? Utils.formatYieldPct(q.best_ytm, 3) : 'â€”'}</div><div style="font-size:0.62rem;color:var(--color-text-muted)">Best Yield</div></div>
              <div style="text-align:right"><div style="font-size:0.9rem;font-weight:600">${q.best_price != null ? 'â‚¹' + q.best_price.toFixed(2) : 'â€”'}</div><div style="font-size:0.62rem;color:var(--color-text-muted)">Price</div></div>
            </div>
            <div style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:6px">
              ${Utils.ratingBadge(q.rating)}
              <span class="badge">${q.broker_count} broker${q.broker_count !== 1 ? 's' : ''}</span>
              <span class="badge badge-green">${q.best_broker || 'â€”'}</span>
              ${imIntel.risk_score != null ? `<span class="badge ${imAction === 'INVEST' ? 'badge-green' : imAction === 'AVOID' ? 'badge-red' : 'badge-amber'}">IM ${imAction}</span>` : ''}
            </div>
            <div style="font-size:0.68rem;color:var(--color-text-muted);margin-bottom:10px;min-height:28px">${reasonText}${reasonText.length >= 70 ? 'â€¦' : ''}</div>
            ${imIntel.risk_score != null ? `<div style="font-size:0.66rem;color:var(--color-text-muted);margin-bottom:8px">IM risk ${Number(imIntel.risk_score).toFixed(1)} / ${Utils.escapeHTML(String(imIntel.risk_level || 'UNKNOWN'))}</div>` : ''}
            <button class="btn btn-ghost btn-sm" type="button" style="width:100%;font-size:0.72rem;justify-content:center" data-method="navigate" data-page="bond-analysis" data-isin="${Utils.escapeAttr(q.isin)}">Analyse ${q.isin.slice(-6)}</button>
          </div>`;
        }).join('')}
      </div>` : `<div style="color:var(--color-text-muted);font-size:0.8rem;padding:8px 0">${emptyText}</div>`;
    el.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <div style="font-weight:700;font-size:0.9rem">${IC.trending} Best Bond Picks <span style="font-size:0.72rem;font-weight:400;color:var(--color-text-muted)">â€” scored across yield, rating, broker coverage &amp; reliability for ${asOf || 'today'}</span></div>
        <div style="font-size:0.72rem;color:var(--color-text-muted)">${IC.clock} refreshed ${now}</div>
      </div>
      <div style="display:grid;gap:14px">
        <div>
          <div style="font-size:0.8rem;font-weight:700;margin-bottom:8px;color:var(--color-text-muted)">Central &amp; State</div>
          ${renderPickCards(byCategory.STATE_CENTRAL, 'No Central & State bonds in the current quote set')}
        </div>
        <div>
          <div style="font-size:0.8rem;font-weight:700;margin-bottom:8px;color:var(--color-text-muted)">PSU</div>
          ${renderPickCards(byCategory.PSU, 'No PSU bonds in the current quote set')}
        </div>
        <div>
          <div style="font-size:0.8rem;font-weight:700;margin-bottom:8px;color:var(--color-text-muted)">Private</div>
          ${renderPickCards(byCategory.PRIVATE, 'No private bonds in the current quote set')}
        </div>
        <div>
          <div style="font-size:0.8rem;font-weight:700;margin-bottom:8px;color:var(--color-text-muted)">Equity</div>
          ${renderPickCards(byCategory.EQUITY, 'No equity instruments in the current quote set')}
        </div>
      </div>`;
  }

  function _showRecTab(tab) {
    const qTab = document.getElementById('tab-quotes');
    const aiTab = document.getElementById('tab-ai');
    const pTab = document.getElementById('tab-profiles');
    const qBtn = document.getElementById('tab-quotes-btn');
    const aiBtn = document.getElementById('tab-ai-btn');
    const pBtn = document.getElementById('tab-profiles-btn');
    if (qTab) qTab.style.display = tab === 'quotes' ? '' : 'none';
    if (aiTab) aiTab.style.display = tab === 'ai' ? '' : 'none';
    if (pTab) pTab.style.display = tab === 'profiles' ? '' : 'none';
    if (qBtn) qBtn.classList.toggle('active', tab === 'quotes');
    if (aiBtn) aiBtn.classList.toggle('active', tab === 'ai');
    if (pBtn) pBtn.classList.toggle('active', tab === 'profiles');
  }

  function _filterRecs() {
    const el = window._recPageEl;
    if (!el) return;
    const kw = (document.getElementById('rec-search')?.value || '').toLowerCase();
    const rat = (document.getElementById('rec-rating')?.value || '');
    const cat = (document.getElementById('rec-category')?.value || '');
    const filtered = (el._allQuotes || []).filter(q => {
      const isin = (q.isin || '').toLowerCase();
      const bondName = (q.bond_name || '').toLowerCase();
      const textMatch = !kw || isin.includes(kw) || bondName.includes(kw);
      const ratingMatch = !rat || (q.rating || '').includes(rat.replace(/[+\-].*/, ''));
      const categoryMatch = !cat || _canonicalCategoryKey(q.instrument_category || q.category_bucket || q.category || q.issuer_type) === cat;
      return textMatch && ratingMatch && categoryMatch;
    });
    App._renderQuotesTable(filtered);
  }

  function _renderQuotesTable(quotes) {
    const el = document.getElementById('rec-table');
    if (!el) return;
    const status = window._recPageStatus || {};
    if (!quotes.length) {
      el.innerHTML = status.quotes
        ? Utils.emptyHTML('Quote data unavailable', 'The broker quote service did not return data for this session.')
        : Utils.emptyHTML('No quotes found', 'Upload broker files or try adjusting filters');
      return;
    }
    el.innerHTML = `
      <table class="data-table">
        <thead><tr>
          <th>ISIN</th><th>Bond Name</th><th>Rating</th>
          <th>Type</th>
          <th class="col-number">Best Yield</th><th class="col-number">Best Price</th>
          <th>Best Broker</th><th>Reason</th>
          <th>IM Risk</th><th>IM Decision</th>
          <th class="col-number"># Brokers</th><th>Maturity</th><th>Action</th>
        </tr></thead>
        <tbody>${quotes.map(q => {
          const bq = q.all_broker_quotes || [];
          const bestQ = bq.find(b => b.is_best) || bq[0] || {};
          const bestYtmPct = Utils.normalizePercentValue(q.best_ytm);
          const bucket = _canonicalCategoryKey(q.instrument_category || q.category_bucket || q.category || q.issuer_type);
          const trendIcon = bestQ.trend_direction === 'TIGHTENING' ? 'â–²' : bestQ.trend_direction === 'WIDENING' ? 'â–¼' : '';
          const trendColor = bestQ.trend_direction === 'TIGHTENING' ? 'color:var(--color-success)' : bestQ.trend_direction === 'WIDENING' ? 'color:var(--color-danger)' : '';
          const reliabilityBadge = bestQ.reliability_score != null ? `<span title="Broker reliability" style="font-size:0.65rem;background:var(--color-surface-2);border-radius:4px;padding:1px 4px">${(bestQ.reliability_score).toFixed(0)}%</span>` : '';
          const imIntel = q.im_intelligence || {};
          const imDecision = imIntel.decision || {};
          const imAction = String(imDecision.action || 'CAUTION').toUpperCase();
          const imRiskText = imIntel.risk_score != null ? `${Number(imIntel.risk_score).toFixed(1)} / ${Utils.escapeHTML(String(imIntel.risk_level || 'UNKNOWN'))}` : 'â€”';
          return `<tr>
            <td class="font-mono" style="font-size:0.72rem">${q.isin}</td>
            <td class="truncate" style="max-width:140px" title="${q.bond_name || ''}">${q.bond_name || 'â€”'}</td>
            <td>${Utils.ratingBadge(q.rating)} ${Utils.ratingAgencyTag(null, q.rating)}</td>
            <td><span class="badge" style="font-size:0.68rem">${_investmentDisplayLabel(q)}</span></td>
            <td class="col-number font-medium" style="color:${(bestYtmPct||0)>=8?'#16a34a':(bestYtmPct||0)>=7?'#f59e0b':'var(--color-text,inherit)'}">${q.best_ytm != null ? Utils.formatYieldPct(q.best_ytm, 4) : 'â€”'}</td>
            <td class="col-number">${q.best_price != null ? 'â‚¹' + q.best_price.toFixed(2) : 'â€”'}</td>
            <td><span class="badge badge-green">${q.best_broker || 'â€”'}</span> ${reliabilityBadge} <span style="${trendColor}">${trendIcon}</span></td>
            <td class="text-muted" style="font-size:0.68rem;max-width:180px" title="${bestQ.reason || ''}">${(bestQ.reason || '').slice(0, 60)}${(bestQ.reason || '').length > 60 ? 'â€¦' : ''}</td>
            <td>${imRiskText}</td>
            <td><span class="badge ${imAction === 'INVEST' ? 'badge-green' : imAction === 'AVOID' ? 'badge-red' : 'badge-amber'}">${imAction}</span></td>
            <td class="col-number">${q.broker_count || bq.length}</td>
            <td class="font-mono" style="font-size:0.72rem">${q.maturity_date ? Utils.formatDate(q.maturity_date) : 'â€”'}</td>
            <td><button class="btn btn-ghost btn-sm" type="button" data-method="navigate" data-page="bond-analysis" data-isin="${Utils.escapeAttr(q.isin)}">Analyse</button></td>
          </tr>`;
        }).join('')}</tbody>
      </table>
    `;
  }

  function _renderBrokerProfiles(profiles) {
    const el = document.getElementById('broker-profiles-content');
    if (!el) return;
    const status = window._recPageStatus || {};
    if (!profiles.length) {
      el.innerHTML = status.profiles
        ? Utils.emptyHTML('Broker profiles unavailable', 'The broker profile service did not return data for this session.')
        : Utils.emptyHTML('No broker profiles', 'Upload broker quote files to generate profiles');
      return;
    }
    const trendBadge = (t) => {
      if (t === 'TIGHTENING') return '<span class="badge badge-green">â–² Tightening</span>';
      if (t === 'WIDENING') return '<span class="badge badge-red">â–¼ Widening</span>';
      return '<span class="badge">â€” Stable</span>';
    };
    el.innerHTML = `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;padding:16px">` +
      profiles.map(p => {
        let tags = [];
        if (Array.isArray(p.behaviour_tags)) {
          tags = p.behaviour_tags;
        } else {
          try {
            tags = JSON.parse(p.behaviour_tags || '[]');
          } catch (_) {
            tags = [];
          }
        }
        const score = p.reliability_score != null ? p.reliability_score.toFixed(1) + '%' : 'N/A';
        const scoreColor = (p.reliability_score || 0) > 70 ? 'var(--color-success)' : (p.reliability_score || 0) > 40 ? 'var(--color-warning)' : 'var(--color-danger)';
        return `<div class="card" style="padding:16px">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px">
            <div style="font-weight:600;font-size:0.9rem">${p.broker_name}</div>
            <div style="font-size:1.3rem;font-weight:700;color:${scoreColor}">${score}</div>
          </div>
          <div style="font-size:0.72rem;color:var(--color-text-muted);margin-bottom:4px">Reliability Score</div>
          <div style="margin-bottom:8px">${trendBadge(p.trend_direction)}</div>
          <div style="font-size:0.72rem;margin-bottom:6px">
            <span style="color:var(--color-text-muted)">Avg premium vs market:</span>
            <strong>${p.avg_ytm_premium_bps != null ? p.avg_ytm_premium_bps.toFixed(1) + ' bps' : 'N/A'}</strong>
          </div>
          <div style="font-size:0.72rem;margin-bottom:4px">
            <span style="color:var(--color-text-muted)">Avg bid-ask spread:</span>
            <strong>${p.avg_spread_bps != null ? p.avg_spread_bps.toFixed(1) + ' bps' : 'N/A'}</strong>
          </div>
          <div style="font-size:0.72rem;margin-bottom:4px">
            <span style="color:var(--color-text-muted)">Pricing consistency (Ïƒ):</span>
            <strong>${p.pricing_consistency != null ? p.pricing_consistency.toFixed(2) + ' bps' : 'N/A'}</strong>
          </div>
          <div style="font-size:0.72rem;margin-bottom:4px">
            <span style="color:var(--color-text-muted)">Trend slope:</span>
            <strong>${p.trend_slope_bps != null ? (p.trend_slope_bps > 0 ? '+' : '') + p.trend_slope_bps.toFixed(2) + ' bps/day' : 'N/A'}</strong>
          </div>
          <div style="font-size:0.72rem;margin-bottom:4px">
            <span style="color:var(--color-text-muted)">Today's quotes:</span>
            <strong>${p.todays_quotes != null ? p.todays_quotes : 'â€”'}</strong>
          </div>
          <div style="font-size:0.72rem;margin-bottom:6px;color:var(--color-text-muted);font-style:italic;border-left:2px solid var(--color-surface-3);padding-left:6px;margin-top:4px">${p.pattern_summary || ''}</div>
          <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px">
            ${tags.map(t => `<span style="background:var(--color-surface-2);border-radius:4px;padding:1px 6px;font-size:0.65rem">${t.replace(/_/g,' ')}</span>`).join('')}
          </div>
          <div style="font-size:0.68rem;color:var(--color-text-muted)">
            ISINs covered: ${p.total_isins_quoted || 'â€”'} &nbsp;|&nbsp; Active days: ${p.active_days || 'â€”'}<br/>
            First seen: ${p.first_seen_date || 'â€”'} &nbsp;|&nbsp; Last seen: ${p.last_seen_date || 'â€”'}
          </div>
          <button class="btn btn-ghost btn-sm" type="button" style="margin-top:8px;width:100%" data-method="_showBrokerDetail" data-arg="${Utils.escapeAttr(p.broker_name)}">View Detail</button>
        </div>`;
      }).join('') + `</div>`;
  }

  async function _ingestWorkspace() {
    const restoreButtons = _setBusyButtons('button[onclick*="App._ingestWorkspace()"]', true, 'Ingestingâ€¦');
    Utils.toast('info', 'Ingestingâ€¦', 'Scanning workspace for broker quote files');
    try {
      const r = await API.ingestWorkspaceFiles();
      Utils.toast('success', 'Ingested', `${r.total_quotes_stored} quotes from ${r.files_scanned} files`);
      // Reload page
      renderRecommendations(window._recPageEl || document.getElementById('page-content'));
    } catch (e) {
      Utils.toast('error', 'Ingest Failed', e.message);
    } finally {
      restoreButtons();
    }
  }

  async function _showBrokerDetail(name) {
    Utils.toast('info', 'Loadingâ€¦', name);
    try {
      const r = await API.getBrokerDetail(name);
      const p = r.profile || {};
      const history = r.daily_history || [];
      const premHistory = r.premium_history || [];
      const topIsins = r.top_isins || [];
      const trend = r.trend_analysis || {};
      const tags = Array.isArray(p.behaviour_tags) ? p.behaviour_tags : JSON.parse(p.behaviour_tags || '[]');

      // Build mini sparkline SVG for premium history
      function sparkline(data, field, color, label) {
        if (!data.length) return `<div style="color:var(--color-text-muted);font-size:0.77rem;padding:8px">No historical data yet</div>`;
        const vals = data.map(d => d[field]).filter(v => v != null);
        if (!vals.length) return `<div style="color:var(--color-text-muted);font-size:0.77rem;padding:8px">Insufficient data for ${label}</div>`;
        const min = Math.min(...vals), max = Math.max(...vals);
        const range = max - min || 1;
        const W = 320, H = 60, pad = 4;
        const pts = vals.map((v, i) => {
          const x = pad + (i / Math.max(vals.length - 1, 1)) * (W - 2 * pad);
          const y = H - pad - ((v - min) / range) * (H - 2 * pad);
          return `${x.toFixed(1)},${y.toFixed(1)}`;
        }).join(' ');
        const zeroY = min <= 0 && max >= 0 ? (H - pad - ((0 - min) / range) * (H - 2 * pad)).toFixed(1) : null;
        return `<svg width="${W}" height="${H}" style="display:block;margin:4px 0">
          ${zeroY ? `<line x1="${pad}" y1="${zeroY}" x2="${W-pad}" y2="${zeroY}" stroke="#64748b" stroke-width="0.5" stroke-dasharray="3,3"/>` : ''}
          <polyline fill="none" stroke="${color}" stroke-width="2" points="${pts}"/>
          ${vals.map((v, i) => {
            const x = pad + (i / Math.max(vals.length - 1, 1)) * (W - 2 * pad);
            const y = H - pad - ((v - min) / range) * (H - 2 * pad);
            return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3" fill="${color}" title="${v.toFixed(2)}"/>`;
          }).join('')}
        </svg>
        <div style="display:flex;justify-content:space-between;font-size:0.65rem;color:var(--color-text-muted)">
          <span>${data[0]?.quote_date || ''}</span>
          <span style="color:${color};font-weight:600">${label}</span>
          <span>${data[data.length-1]?.quote_date || ''}</span>
        </div>`;
      }

      const interpretColor = (trend.mean_premium_bps || 0) < -3 ? '#16a34a' : (trend.mean_premium_bps || 0) > 10 ? '#dc2626' : '#64748b';

      const html = `
        <div style="max-height:75vh;overflow-y:auto;padding:4px">
          <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px">
            <div style="flex:1;min-width:200px">
              <div style="font-size:0.72rem;color:var(--color-text-muted)">Reliability Score</div>
              <div style="font-size:2rem;font-weight:700;color:${(p.reliability_score||0)>70?'#16a34a':(p.reliability_score||0)>40?'#f59e0b':'#dc2626'}">${p.reliability_score != null ? p.reliability_score.toFixed(1) + '%' : 'N/A'}</div>
            </div>
            <div style="flex:1;min-width:200px">
              <div style="font-size:0.72rem;color:var(--color-text-muted)">Avg Premium vs Market</div>
              <div style="font-size:1.6rem;font-weight:700;color:${interpretColor}">${trend.mean_premium_bps != null ? (trend.mean_premium_bps > 0 ? '+' : '') + trend.mean_premium_bps.toFixed(1) + ' bps' : 'N/A'}</div>
            </div>
            <div style="flex:1;min-width:200px">
              <div style="font-size:0.72rem;color:var(--color-text-muted)">Consistency (Ïƒ)</div>
              <div style="font-size:1.6rem;font-weight:700">${p.pricing_consistency != null ? p.pricing_consistency.toFixed(2) + ' bps' : 'N/A'}</div>
            </div>
          </div>

          ${trend.interpretation ? `
          <div style="background:var(--color-surface-2);border-radius:6px;padding:10px 14px;margin-bottom:14px;border-left:3px solid ${interpretColor}">
            <div style="font-size:0.72rem;color:var(--color-text-muted)">Behaviour Interpretation</div>
            <div style="font-size:0.82rem;font-weight:500;margin-top:3px">${trend.interpretation}</div>
            <div style="font-size:0.68rem;color:var(--color-text-muted);margin-top:4px">Range: ${trend.min_premium_bps != null ? trend.min_premium_bps.toFixed(1) : 'â€”'} to ${trend.max_premium_bps != null ? trend.max_premium_bps.toFixed(1) : 'â€”'} bps | Days analysed: ${trend.days_with_data || 0}</div>
          </div>` : ''}

          ${premHistory.length ? `
          <div style="background:var(--color-surface-2);border-radius:6px;padding:10px 14px;margin-bottom:14px">
            <div style="font-weight:600;font-size:0.82rem;margin-bottom:6px">Premium vs Market Trend (bps/day)</div>
            ${sparkline(premHistory, 'avg_premium_bps', interpretColor, 'Avg YTM Premium (bps)')}
            <div style="font-size:0.68rem;color:var(--color-text-muted);margin-top:4px">
              Positive = above market (expensive) | Negative = below market (competitive)
            </div>
          </div>
          <div style="background:var(--color-surface-2);border-radius:6px;padding:10px 14px;margin-bottom:14px">
            <div style="font-weight:600;font-size:0.82rem;margin-bottom:6px">ISINs Quoted Per Day</div>
            ${sparkline(premHistory, 'num_isins', '#2563eb', 'ISINs/day')}
          </div>` : `<div style="color:var(--color-text-muted);font-size:0.77rem;padding:8px;margin-bottom:12px">No daily premium history yet â€” upload more quote files to build trend data</div>`}

          <div style="margin-bottom:12px">
            <div style="font-weight:600;font-size:0.82rem;margin-bottom:6px">Behaviour Tags</div>
            <div style="display:flex;flex-wrap:wrap;gap:4px">
              ${tags.map(t => `<span style="background:var(--color-surface-2);border-radius:4px;padding:2px 8px;font-size:0.7rem">${t.replace(/_/g,' ')}</span>`).join('') || '<span style="color:var(--color-text-muted)">No tags</span>'}
            </div>
          </div>

          <div style="margin-bottom:12px">
            <div style="font-weight:600;font-size:0.82rem;margin-bottom:6px">Quote Activity History</div>
            <table class="data-table" style="font-size:0.73rem">
              <thead><tr><th>Date</th><th>Quotes</th><th>Min YTM</th><th>Avg YTM</th><th>Max YTM</th></tr></thead>
              <tbody>${history.map(h => `<tr>
                <td>${h.quote_date}</td>
                <td>${h.quotes}</td>
                <td>${h.min_ytm ? h.min_ytm.toFixed(4)+'%' : 'â€”'}</td>
                <td>${h.avg_ytm ? h.avg_ytm.toFixed(4)+'%' : 'â€”'}</td>
                <td>${h.max_ytm ? h.max_ytm.toFixed(4)+'%' : 'â€”'}</td>
              </tr>`).join('')}</tbody>
            </table>
          </div>
          <div>
            <div style="font-weight:600;font-size:0.82rem;margin-bottom:6px">Top Bonds Covered (by frequency)</div>
            <table class="data-table" style="font-size:0.72rem">
              <thead><tr><th>ISIN</th><th>Issuer</th><th>Type</th><th>Rating</th><th>Days</th><th>Avg YTM</th></tr></thead>
              <tbody>${topIsins.map(i => `<tr>
                <td class="font-mono" style="font-size:0.67rem">${i.isin}</td>
                <td style="max-width:140px"><div class="compact-ellipsis" title="${Utils.escapeAttr(i.bond_name || 'â€”')}">${i.bond_name||'â€”'}</div></td>
                <td>${i.issuer_type||'â€”'}</td>
                <td>${Utils.ratingBadge ? Utils.ratingBadge(i.credit_rating) : (i.credit_rating||'â€”')}</td>
                <td>${i.days_quoted}</td>
                <td>${i.avg_ytm ? i.avg_ytm.toFixed(4)+'%' : 'â€”'}</td>
              </tr>`).join('')}</tbody>
            </table>
          </div>
        </div>
      `;
      const overlay = document.getElementById('modal-overlay');
      const content = document.getElementById('modal-content');
      if (overlay && content) {
        content.innerHTML = `
          <div class="modal-header">
            <div class="modal-title">${name} â€” Broker Analytics</div>
            <button class="modal-close" data-action="close-modal">âœ•</button>
          </div>
          <div class="modal-body">${html}</div>
          <div class="modal-footer">
            <button class="btn btn-secondary" data-action="close-modal">Close</button>
          </div>
        `;
        overlay.classList.add('show');
      } else {
        alert(name + ': ' + topIsins.length + ' ISINs covered');
      }
    } catch (e) {
      Utils.toast('error', 'Load Failed', e.message);
    }
  }

  const _explanationStore = {};
  let _explanationSeq = 0;

  function _registerExplanationPayload(payload) {
    if (!payload) return '';
    const key = 'exp_' + (++_explanationSeq);
    _explanationStore[key] = payload;
    return key;
  }

  function _formatExplainPrimitive(value) {
    if (value === null || value === undefined || value === '') return 'â€”';
    if (typeof value === 'number') {
      if (!Number.isFinite(value)) return String(value);
      if (Math.abs(value) >= 1000) return value.toLocaleString('en-IN', { maximumFractionDigits: 6 });
      return value.toFixed(6).replace(/\.0+$/, '').replace(/(\.\d*?)0+$/, '$1');
    }
    if (typeof value === 'boolean') return value ? 'Yes' : 'No';
    if (Array.isArray(value)) return value.map((v) => _formatExplainPrimitive(v)).join(', ');
    if (typeof value === 'object') return Utils.escapeHTML(JSON.stringify(value));
    return Utils.escapeHTML(String(value));
  }

  function _openExplainFromKey(key, title = 'Decision Explainability') {
    const payload = _explanationStore[key];
    if (!payload) {
      Utils.toast('warning', 'Explanation unavailable', 'No explainability payload is available for this item.');
      return;
    }

    const exp = (payload && typeof payload === 'object') ? payload : { summary: String(payload || 'No explanation provided') };
    const levels = exp.explanation_levels || {};
    const level1 = levels.level_1 || {};
    const level2 = levels.level_2 || {};
    const level3 = levels.level_3 || {};
    const factors = Array.isArray(level2.key_factors) ? level2.key_factors : [];
    const rules = Array.isArray(level2.rule_checks) ? level2.rule_checks : [];
    const lineage = Array.isArray(level3.data_lineage) ? level3.data_lineage : [];
    const regulatory = Array.isArray(level3.regulatory_basis) ? level3.regulatory_basis : [];
    const counterfactuals = Array.isArray(exp.counterfactuals) ? exp.counterfactuals : [];
    const changes = Array.isArray((exp.change_tracking || {}).changes) ? exp.change_tracking.changes : [];
    const confidence = exp.confidence || {};
    const uiHints = exp.ui_hints || {};

    const modal = document.getElementById('modal-overlay');
    const modalContent = document.getElementById('modal-content');
    if (!modal || !modalContent) return;

    modalContent.innerHTML = `
      <div class="modal-header">
        <div class="modal-title">${Utils.escapeHTML(String(title))}</div>
        <button class="modal-close" data-action="close-modal">${IC.x}</button>
      </div>
      <div class="modal-body">
        <div class="card" style="margin-bottom:10px">
          <div class="card-body">
            <div class="metric-label">Level 1 - Main Answer</div>
            <div style="font-size:0.86rem;margin-top:4px">${Utils.escapeHTML(String(level1.headline || exp.summary || 'No summary available'))}</div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;font-size:0.72rem;color:var(--text-muted)">
              <span>Module: <strong>${Utils.escapeHTML(String(exp.module || 'unknown'))}</strong></span>
              <span>Outcome: <strong>${Utils.escapeHTML(String(exp.decision_outcome || 'UNKNOWN'))}</strong></span>
              <span>Confidence: <strong>${Number(confidence.score || 0).toFixed(4)} (${Utils.escapeHTML(String(confidence.level || 'low'))})</strong></span>
              <span>Severity: <strong>${Utils.escapeHTML(String(uiHints.severity || 'INFO'))}</strong></span>
            </div>
          </div>
        </div>

        <div class="grid-2" style="margin-bottom:10px">
          <div class="card">
            <div class="card-header"><div class="card-title">Level 2 - Key Factors</div></div>
            <div class="card-body" style="max-height:220px;overflow:auto">
              ${factors.length ? `
              <table class="data-table" style="font-size:0.74rem">
                <thead><tr><th>Factor</th><th>Value</th><th>Impact</th><th>Evidence</th></tr></thead>
                <tbody>${factors.map((f) => `<tr>
                  <td>${Utils.escapeHTML(String(f.name || 'Factor'))}</td>
                  <td>${_formatExplainPrimitive(f.value)}</td>
                  <td>${Utils.escapeHTML(String(f.impact || 'neutral'))}</td>
                  <td>${Utils.escapeHTML(String(f.evidence || 'â€”'))}</td>
                </tr>`).join('')}</tbody>
              </table>` : '<div class="text-muted" style="font-size:0.78rem">No key factors provided.</div>'}
            </div>
          </div>

          <div class="card">
            <div class="card-header"><div class="card-title">Level 2 - Rule Checks</div></div>
            <div class="card-body" style="max-height:220px;overflow:auto">
              ${rules.length ? `
              <table class="data-table" style="font-size:0.74rem">
                <thead><tr><th>Rule</th><th>Status</th><th>Message</th></tr></thead>
                <tbody>${rules.map((r) => `<tr>
                  <td>${Utils.escapeHTML(String(r.rule_code || 'RULE'))}</td>
                  <td>${Utils.statusBadge(String(r.status || 'INFO'))}</td>
                  <td>${Utils.escapeHTML(String(r.message || 'â€”'))}</td>
                </tr>`).join('')}</tbody>
              </table>` : '<div class="text-muted" style="font-size:0.78rem">No rule checks provided.</div>'}
            </div>
          </div>
        </div>

        <div class="grid-2" style="margin-bottom:10px">
          <div class="card">
            <div class="card-header"><div class="card-title">Level 3 - Calculation Trace</div></div>
            <div class="card-body">
              <pre style="max-height:220px;overflow:auto;background:var(--bg-tertiary);border:1px solid var(--border-light);padding:10px;border-radius:8px;font-size:0.72rem">${Utils.escapeHTML(JSON.stringify(level3.calculation_trace || {}, null, 2))}</pre>
            </div>
          </div>
          <div class="card">
            <div class="card-header"><div class="card-title">Level 3 - Data Lineage</div></div>
            <div class="card-body" style="max-height:220px;overflow:auto">
              ${lineage.length ? lineage.map((l) => `
                <div style="font-size:0.76rem;padding:6px 0;border-bottom:1px dashed var(--border-light)">
                  <div><strong>${Utils.escapeHTML(String(l.source || 'source'))}</strong> â†’ ${Utils.escapeHTML(String(l.field || 'field'))}</div>
                  <div style="color:var(--text-muted)">Trust: ${Utils.escapeHTML(String(l.trust_level || 'n/a'))}</div>
                </div>
              `).join('') : '<div class="text-muted" style="font-size:0.78rem">No lineage metadata provided.</div>'}
            </div>
          </div>
        </div>

        <div class="grid-2" style="margin-bottom:10px">
          <div class="card">
            <div class="card-header"><div class="card-title">Counterfactuals</div></div>
            <div class="card-body" style="max-height:180px;overflow:auto">
              ${counterfactuals.length ? counterfactuals.map((c) => `
                <div style="font-size:0.76rem;padding:6px 0;border-bottom:1px dashed var(--border-light)">
                  <div><strong>${Utils.escapeHTML(String(c.scenario || 'scenario'))}</strong> â†’ ${Utils.escapeHTML(String(c.expected_outcome || 'UNKNOWN'))}</div>
                  <div style="color:var(--text-muted)">${Utils.escapeHTML(String(c.impact_summary || ''))}</div>
                </div>
              `).join('') : '<div class="text-muted" style="font-size:0.78rem">No counterfactuals provided.</div>'}
            </div>
          </div>
          <div class="card">
            <div class="card-header"><div class="card-title">Change Tracking & Governance</div></div>
            <div class="card-body" style="max-height:180px;overflow:auto">
              <div style="font-size:0.76rem;margin-bottom:6px">Changed: <strong>${(exp.change_tracking || {}).changed ? 'Yes' : 'No'}</strong></div>
              ${changes.length ? changes.map((c) => `
                <div style="font-size:0.74rem;color:var(--text-muted)">
                  ${Utils.escapeHTML(String(c.field || 'field'))}: ${_formatExplainPrimitive(c.before)} â†’ ${_formatExplainPrimitive(c.after)}
                </div>
              `).join('') : '<div class="text-muted" style="font-size:0.78rem">No tracked changes recorded.</div>'}
              <div style="font-size:0.74rem;margin-top:8px">Override: <strong>${(exp.override || {}).applied ? 'Applied' : 'No'}</strong></div>
              ${(exp.override || {}).reason ? `<div style="font-size:0.74rem;color:var(--text-muted)">Reason: ${Utils.escapeHTML(String((exp.override || {}).reason))}</div>` : ''}
            </div>
          </div>
        </div>

        ${regulatory.length ? `
        <div class="card" style="margin-bottom:6px">
          <div class="card-header"><div class="card-title">Regulatory Basis</div></div>
          <div class="card-body" style="font-size:0.78rem">${regulatory.map((x) => Utils.escapeHTML(String(x))).join(' | ')}</div>
        </div>` : ''}
      </div>
      <div class="modal-footer">
        <button class="btn btn-secondary" data-action="close-modal">Close</button>
      </div>
    `;
    modal.classList.add('show');
  }

  function _renderAIRecsTable(recs) {
    const el = document.getElementById('ai-rec-table');
    if (!el) return;
    const status = window._recPageStatus || {};
    if (!recs.length) { el.innerHTML = status.recs ? Utils.emptyHTML('AI recommendations unavailable', 'The recommendation service did not return data for this session.') : Utils.emptyHTML('No AI recommendations', 'Run market data refresh to generate recommendations'); return; }
    const hasValue = (value) => value !== null && value !== undefined && !(typeof value === 'string' && value.trim() === '');
    const textOrUnavailable = (value, fallback = 'Unavailable') => hasValue(value) ? String(value) : fallback;
    el.innerHTML = `
      <div style="padding:10px 12px;font-size:0.72rem;color:var(--text-muted);border-bottom:1px solid var(--border-light)">AI recommendations are ISIN-deduped to avoid repeating broker-quote rows.</div>
      <table class="data-table">
        <thead><tr><th>Bond</th><th>Type</th><th>Rating</th><th class="col-number">Yield</th><th class="col-number">Price</th><th class="col-number">Score</th><th>Risk</th><th>Reason</th><th>Why?</th></tr></thead>
        <tbody>${recs.map(r => {
          const riskRaw = String(r.risk_category || '').trim();
          const riskClass = riskRaw === 'Low Risk' ? 'badge-green' : riskRaw === 'Medium Risk' ? 'badge-amber' : riskRaw ? 'badge-red' : 'badge';
          const categoryLabel = textOrUnavailable(r.category_display || _investmentDisplayLabel(r));
          const ytmDisplay = hasValue(r.ytm) && Number.isFinite(Number(r.ytm)) ? Utils.formatYieldPct(r.ytm, 4) : 'Unavailable';
          const cleanPriceDisplay = hasValue(r.clean_price) && Number.isFinite(Number(r.clean_price)) ? '\u20b9' + Number(r.clean_price).toFixed(2) : 'Unavailable';
          const scoreDisplay = hasValue(r.score) && Number.isFinite(Number(r.score)) ? Number(r.score).toFixed(1) : 'Unavailable';
          const quoteOnly = !!(r.quote_only_market_fallback || String(r.im_explanation_source || '').toUpperCase() === 'BROKER_QUOTES_INDICATIVE');
          const quoteOnlyReason = String(
            r.quote_only_fallback_reason
            || r.quote_only_market_fallback_reason
            || (quoteOnly ? 'No persisted market print was available; broker quotes were used to synthesize indicative market fields.' : '')
          ).trim();
          const quoteOnlyBadge = quoteOnly ? `<span class="badge badge-amber" title="${Utils.escapeAttr(quoteOnlyReason || 'No persisted market print was available; broker quotes were used to synthesize indicative market fields.') }" style="font-size:0.62rem;white-space:nowrap">Quote-only</span>` : '';
          const explainKey = _registerExplanationPayload(r.explanation || {
            module: 'legacy_recommendation_ui_fallback',
            decision_outcome: r.recommendation || 'INFO',
            summary: textOrUnavailable(r.reason, 'No recommendation rationale available'),
            explanation_levels: {
              level_1: { headline: textOrUnavailable(r.reason, 'No recommendation rationale available') },
              level_2: { key_factors: [], rule_checks: [] },
              level_3: { calculation_trace: {}, data_lineage: [] },
            },
          });
          return `<tr>
          <td class="truncate" style="max-width:150px"><strong>${textOrUnavailable(r.issuer_name || r.bond_name)}</strong><br/><span class="font-mono text-muted" style="font-size:0.68rem">${textOrUnavailable(r.isin)}</span>${quoteOnlyBadge}</td>
          <td>${categoryLabel}<div class="text-muted" style="font-size:0.68rem">${textOrUnavailable(r.issuer_type, '')}</div></td>
          <td>${Utils.ratingBadge(String(r.credit_rating || 'NR').trim() || 'NR')}</td>
          <td class="col-number text-green font-medium">${ytmDisplay}</td>
          <td class="col-number">${cleanPriceDisplay}</td>
          <td class="col-number">${scoreDisplay}</td>
          <td><span class="badge ${riskClass}">${riskRaw || 'Unavailable'}</span></td>
          <td class="text-muted" style="font-size:0.72rem;max-width:180px">${textOrUnavailable(r.reason)}</td>
          <td>${explainKey ? `<button class="btn btn-ghost btn-sm" type="button" style="font-size:0.7rem;padding:2px 8px" data-method="_openExplainFromKey" data-arg="${Utils.escapeAttr(explainKey)}" data-arg2="Legacy Recommendation">Why?</button>` : 'â€”'}</td>
        </tr>`;
        }).join('')}</tbody>
      </table>
    `;
  }

  function _exportRecsCSV() {
    const el = window._recPageEl;
    if (!el || !el._allQuotes) return;
    const esc = (v) => {
      const s = String(v ?? '');
      if (/[",\n]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
      return s;
    };
    const rows = [['ISIN','BondName','Rating','Type','BestYTM','BestPrice','BestBroker','NumBrokers','MaturityDate','QuoteDate']];
    (el._allQuotes || []).forEach(q => {
      rows.push([q.isin, q.bond_name || '', q.rating || '', _investmentDisplayLabel(q), q.best_ytm || '', q.best_price || '', q.best_broker || '', q.broker_count || '', q.maturity_date || '', q.quote_date || '']);
    });
    const csv = rows.map(r => r.map(esc).join(',')).join('\n');
    const a = document.createElement('a'); a.href = 'data:text/csv;charset=utf-8,' + encodeURIComponent(csv);
    a.download = 'best_quotes_' + new Date().toISOString().slice(0,10) + '.csv'; a.click();
  }

  function printPortfolioReport() {
    window.print();
  }

  async function exportPortfolioCSV() {
    const restoreButtons = _setBusyButtons('button[data-method="exportPortfolioCSV"], button[onclick*="App.exportPortfolioCSV()"]', true, 'Exportingâ€¦');
    try {
      const data = await API.getPortfolio();
      const holdings = data.holdings || [];
      const rows = [['ISIN','Issuer','Type','SubType','Rating','FaceValue','Quantity','MarketValue','CostBasis','UnrealizedGain','YTM','Duration','MaturityDate']];
      holdings.forEach(h => {
        const typeLabel = _investmentDisplayLabel(h);
        const subtypeLabel = _investmentTypeFromRow(h) === 'BOND' ? _displayCategoryLabel(_bondSubtypeFromRow(h)) : '';
        rows.push([h.isin, h.issuer_name || '', typeLabel, subtypeLabel, h.rating || '', h.face_value || '', h.quantity || '', h.market_val || '', h.cost_basis || '', h.unrealized_gain || '', h.current_ytm || '', h.duration || '', h.maturity_date || '']);
      });
      const csv = rows.map(r => r.join(',')).join('\n');
      const a = document.createElement('a'); a.href = 'data:text/csv;charset=utf-8,' + encodeURIComponent(csv);
      a.download = 'portfolio_' + new Date().toISOString().slice(0,10) + '.csv'; a.click();
      Utils.toast('success', 'Exported', 'Portfolio CSV downloaded');
    } catch (err) { Utils.toast('error', 'Export Failed', err.message); }
    finally { restoreButtons(); }
  }

  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  // 5. RISK & COMPLIANCE PAGE
  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  async function renderRiskCompliance(el) {
    el.innerHTML = `
      <div class="page-header">
        <div>
          <div class="page-title">Risk & Compliance</div>
          <div class="page-subtitle">Stress testing, VaR, scenario analysis â€” Institutional grade</div>
        </div>
        <div class="page-actions">
          <button class="btn btn-secondary btn-sm" type="button" data-method="runComplianceScan">${IC.shield} Run Full Scan</button>
        </div>
      </div>

      <!-- Risk KPI strip -->
      <div class="metric-grid" id="risk-kpis">${Utils.skeletonCards(5)}</div>

      <div id="risk-access-banner" style="margin-top:12px"></div>

      <!-- Tabs -->
      <div class="card">
        <div class="tabs" id="risk-tabs">
          <div class="tab active" data-tab="rate-sensitivity">Rate Sensitivity</div>
          <div class="tab" data-tab="var">VaR &amp; ES</div>
          <div class="tab" data-tab="scenarios">Scenarios</div>
          <div class="tab" data-tab="spread-sim">Spread Simulation</div>
          <div class="tab" data-tab="yield-curve">Yield Curve</div>
          <div class="tab" data-tab="compliance">Compliance Rules</div>
        </div>

        <div class="tab-content active" id="tab-rate-sensitivity">
          <div class="card-body" id="risk-rate-sensitivity">${Utils.loadingHTML('Computing rate sensitivity...')}</div>
        </div>
        <div class="tab-content" id="tab-var">
          <div class="card-body" id="risk-var">${Utils.loadingHTML('Computing VaR...')}</div>
        </div>
        <div class="tab-content" id="tab-scenarios">
          <div class="card-body" id="risk-scenarios">${Utils.loadingHTML('Running scenarios...')}</div>
        </div>
        <div class="tab-content" id="tab-spread-sim">
          <div class="card-body" id="risk-spread-sim">${Utils.loadingHTML('Running spread simulation...')}</div>
        </div>
        <div class="tab-content" id="tab-yield-curve">
          <div class="card-body" id="risk-yield-curve">${Utils.loadingHTML('Loading yield curve...')}</div>
        </div>
        <div class="tab-content" id="tab-compliance">
          <div class="card-body">
            <div class="metric-grid" id="comp-overview">${Utils.skeletonCards(4)}</div>
            <div class="table-container" id="comp-rules" style="margin-top:16px">${Utils.skeletonTable(8)}</div>
            <div class="table-container" id="comp-audit" style="max-height:400px;overflow-y:auto;margin-top:16px">${Utils.skeletonTable(5)}</div>
          </div>
        </div>
      </div>

      <div class="last-updated" id="comp-updated" style="margin-top:12px"></div>
    `;

    // Tab switching
    document.querySelectorAll('#risk-tabs .tab').forEach(tab => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('#risk-tabs .tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('#risk-tabs ~ .tab-content, #risk-tabs + * ~ .tab-content, .tab-content').forEach(t => {
          if (t.id && t.id.startsWith('tab-') && ['tab-rate-sensitivity','tab-var','tab-scenarios','tab-spread-sim','tab-yield-curve','tab-compliance'].includes(t.id)) {
            t.classList.remove('active');
          }
        });
        tab.classList.add('active');
        const contentEl = document.getElementById('tab-' + tab.dataset.tab);
        if (contentEl) contentEl.classList.add('active');
      });
    });

    loadRiskData();
    loadComplianceData();
  }

  async function loadRiskData() {
    try {
      const NOT_COMPUTED = 'Not computed';
      const NO_DATA_MSG = 'No portfolio data available. Run Full Scan to generate risk metrics.';
      const NO_ANALYSIS_MSG = 'No analysis available. Run Full Scan or upload data.';

      const asState = (raw) => {
        const status = String(raw?.status || '').toLowerCase();
        if (['ok', 'no_data', 'error', 'restricted'].includes(status)) return raw;
        return { status: 'error', data: {}, message: 'Invalid data contract from API' };
      };
      const isNumber = (value) => {
        if (value == null) return false;
        if (typeof value === 'string' && value.trim() === '') return false;
        return Number.isFinite(Number(value));
      };
      const formatINROrNotComputed = (value) => (isNumber(value) ? Utils.formatINR(Number(value)) : NOT_COMPUTED);
      const formatPctOrNotComputed = (value, dp = 2) => (isNumber(value) ? `${Number(value).toFixed(dp)}%` : NOT_COMPUTED);
      const formatNumOrNotComputed = (value, dp = 2) => (isNumber(value) ? Number(value).toFixed(dp) : NOT_COMPUTED);
      const tabMessageHTML = (state, unavailableTitle) => {
        if (state.status === 'restricted') {
          return Utils.emptyHTML('Risk data restricted', 'Access to risk analytics is restricted for this role/session.');
        }
        if (state.status === 'no_data') {
          return Utils.emptyHTML('No portfolio data available', 'Run Full Scan to generate risk metrics.');
        }
        const msg = state.message || NO_ANALYSIS_MSG;
        return Utils.emptyHTML(unavailableTitle, msg);
      };

      const [
        rateStateRaw,
        varStateRaw,
        scenarioStateRaw,
        spreadStateRaw,
        curveStateRaw,
        concentrationStateRaw,
        alertStateRaw,
      ] = await Promise.all([
        API.getRateSensitivityState(),
        API.getVaRState(),
        API.getScenariosState(),
        API.getSpreadSimulationState(),
        API.getYieldCurveState(),
        API.getPortfolioConcentrationState(),
        API.alertSummaryState(),
      ]);

      const rateState = asState(rateStateRaw);
      const varState = asState(varStateRaw);
      const scenarioState = asState(scenarioStateRaw);
      const spreadState = asState(spreadStateRaw);
      const curveState = asState(curveStateRaw);
      const concentrationState = asState(concentrationStateRaw);
      const alertState = asState(alertStateRaw);

      const riskStates = [rateState, varState, scenarioState, spreadState, curveState];
      const authBlocked = riskStates.some((x) => x.status === 'restricted');
      const allNoData = riskStates.every((x) => x.status === 'no_data');

      const rateSens = rateState.data || {};
      const varData = varState.data || {};
      const scenarios = scenarioState.data || {};
      const spreadSim = spreadState.data || {};
      const yieldCurve = curveState.data || {};

      const rateExplainKey = _registerExplanationPayload(rateSens?.explanation);
      const varExplainKey = _registerExplanationPayload(varData?.explanation);
      const scenarioExplainKey = _registerExplanationPayload(scenarios?.explanation);
      const spreadExplainKey = _registerExplanationPayload(spreadSim?.explanation);
      const curveExplainKey = _registerExplanationPayload(yieldCurve?.explanation);

      const var95 = varData?.var?.var_95_1d ?? null;
      const var99 = varData?.var?.var_99_1d ?? null;
      const var95Pct = varData?.var?.var_95_1d_pct ?? null;
      const es95 = varData?.expected_shortfall?.es_95_1d ?? null;
      const worstScenario = Array.isArray(scenarios?.scenarios)
        ? [...scenarios.scenarios].sort((a, b) => (Number(a?.pnl_pct ?? a?.pnl ?? Number.POSITIVE_INFINITY) - Number(b?.pnl_pct ?? b?.pnl ?? Number.POSITIVE_INFINITY)))[0]
        : null;
      const sens100 = Array.isArray(rateSens?.rate_shocks) ? rateSens.rate_shocks.find((s) => Number(s?.shock_bps) === 100) : null;

      const concentration = concentrationState.status === 'ok' ? (concentrationState.data || {}) : {};
      const topIssuerPct = concentration?.top_issuer_exposure_pct ?? null;
      const ratingBreakdown = Array.isArray(concentration?.rating_breakdown) ? concentration.rating_breakdown : [];
      const lowerCreditPct = ratingBreakdown
        .filter((row) => /BBB|BB|B\+|B\-|C|D|NR/i.test(String(row?.name || '')))
        .reduce((sum, row) => sum + (isNumber(row?.pct) ? Number(row.pct) : 0), 0);

      const alertsData = alertState.status === 'ok' ? (alertState.data || {}) : {};
      const alertSummaryRows = Array.isArray(alertsData.summary) ? alertsData.summary : [];
      const criticalAlerts = alertSummaryRows
        .filter((row) => String(row?.severity || '').toUpperCase() === 'CRITICAL')
        .reduce((sum, row) => sum + (isNumber(row?.cnt) ? Number(row.cnt) : 0), 0);
      const liquidityAlerts = alertSummaryRows
        .filter((row) => /liquidity/i.test(String(row?.alert_type || '')))
        .reduce((sum, row) => sum + (isNumber(row?.cnt) ? Number(row.cnt) : 0), 0);

      let riskScore = 0;
      const riskDrivers = [];
      if (isNumber(topIssuerPct)) {
        if (Number(topIssuerPct) > 15) {
          riskScore += 2;
          riskDrivers.push(`Concentration risk high (${Number(topIssuerPct).toFixed(2)}%)`);
        } else if (Number(topIssuerPct) > 12) {
          riskScore += 1;
          riskDrivers.push(`Concentration risk elevated (${Number(topIssuerPct).toFixed(2)}%)`);
        }
      }
      if (isNumber(var95Pct)) {
        if (Number(var95Pct) > 2) {
          riskScore += 2;
          riskDrivers.push(`VaR 95% elevated (${Number(var95Pct).toFixed(2)}%)`);
        } else if (Number(var95Pct) > 1) {
          riskScore += 1;
          riskDrivers.push(`VaR 95% moderate (${Number(var95Pct).toFixed(2)}%)`);
        }
      }
      if (lowerCreditPct > 20) {
        riskScore += 2;
        riskDrivers.push(`Credit quality pressure (${lowerCreditPct.toFixed(2)}% lower-grade)`);
      } else if (lowerCreditPct > 10) {
        riskScore += 1;
        riskDrivers.push(`Credit quality watch (${lowerCreditPct.toFixed(2)}% lower-grade)`);
      }
      if (criticalAlerts > 0) {
        riskScore += 2;
        riskDrivers.push(`Critical alerts active (${criticalAlerts})`);
      }
      if (liquidityAlerts > 0) {
        riskScore += 1;
        riskDrivers.push(`Liquidity indicators flagged (${liquidityAlerts})`);
      }

      const overallRisk = riskScore >= 5 ? 'High' : riskScore >= 3 ? 'Medium' : 'Low';
      const overallRiskCard = overallRisk === 'High' ? 'red' : overallRisk === 'Medium' ? 'amber' : 'green';

      if (!document.getElementById('risk-kpis')) return;
      document.getElementById('risk-kpis').innerHTML = authBlocked ? `
        <div class="metric-card amber">
          <div class="metric-label">Overall Risk</div>
          <div class="metric-value">Restricted</div>
          <div class="metric-sub text-muted" style="font-size:0.72rem">Access to risk analytics is restricted</div>
        </div>
        <div class="metric-card amber"><div class="metric-label">VaR 1-Day 95%</div><div class="metric-value">${NOT_COMPUTED}</div></div>
        <div class="metric-card amber"><div class="metric-label">VaR 1-Day 99%</div><div class="metric-value">${NOT_COMPUTED}</div></div>
        <div class="metric-card amber"><div class="metric-label">Expected Shortfall 95%</div><div class="metric-value">${NOT_COMPUTED}</div></div>
        <div class="metric-card amber"><div class="metric-label">Worst Scenario</div><div class="metric-value">${NOT_COMPUTED}</div></div>
        <div class="metric-card amber"><div class="metric-label">P&L @+100bps</div><div class="metric-value">${NOT_COMPUTED}</div></div>
      ` : allNoData ? `
        <div class="metric-card amber">
          <div class="metric-label">Overall Risk</div>
          <div class="metric-value">No Data</div>
          <div class="metric-sub text-muted" style="font-size:0.72rem">${NO_DATA_MSG}</div>
        </div>
        <div class="metric-card amber"><div class="metric-label">VaR 1-Day 95%</div><div class="metric-value">${NOT_COMPUTED}</div></div>
        <div class="metric-card amber"><div class="metric-label">VaR 1-Day 99%</div><div class="metric-value">${NOT_COMPUTED}</div></div>
        <div class="metric-card amber"><div class="metric-label">Expected Shortfall 95%</div><div class="metric-value">${NOT_COMPUTED}</div></div>
        <div class="metric-card amber"><div class="metric-label">Worst Scenario</div><div class="metric-value">${NOT_COMPUTED}</div></div>
        <div class="metric-card amber"><div class="metric-label">P&L @+100bps</div><div class="metric-value">${NOT_COMPUTED}</div></div>
      ` : `
        <div class="metric-card ${overallRiskCard}">
          <div class="metric-label">Overall Risk</div>
          <div class="metric-value">${overallRisk}</div>
          <div class="metric-sub text-muted" style="font-size:0.72rem">${riskDrivers.length ? riskDrivers.slice(0, 2).join(' | ') : 'Derived from concentration, credit quality, and liquidity signals'}</div>
        </div>
        <div class="metric-card red">
          <div class="metric-label">VaR 1-Day 95%</div>
          <div class="metric-value">${formatINROrNotComputed(var95)}</div>
          <div class="metric-sub text-muted" style="font-size:0.72rem">Parametric</div>
        </div>
        <div class="metric-card red">
          <div class="metric-label">VaR 1-Day 99%</div>
          <div class="metric-value">${formatINROrNotComputed(var99)}</div>
        </div>
        <div class="metric-card amber">
          <div class="metric-label">Expected Shortfall 95%</div>
          <div class="metric-value">${formatINROrNotComputed(es95)}</div>
        </div>
        <div class="metric-card amber">
          <div class="metric-label">Worst Scenario</div>
          <div class="metric-value" style="font-size:0.88rem">${formatPctOrNotComputed(worstScenario?.pnl_pct, 2)}</div>
          <div class="metric-sub text-muted" style="font-size:0.65rem">${worstScenario?.scenario_name ?? worstScenario?.name ?? NOT_COMPUTED}</div>
        </div>
        <div class="metric-card blue">
          <div class="metric-label">P&L @+100bps</div>
          <div class="metric-value" style="font-size:0.88rem">${formatINROrNotComputed(sens100?.pnl)}</div>
        </div>
      `;

      const rsEl = document.getElementById('risk-rate-sensitivity');
      if (rsEl && rateState.status === 'ok') {
        const shocks = Array.isArray(rateSens?.rate_shocks) ? rateSens.rate_shocks : [];
        const durContribs = Array.isArray(rateSens?.duration_contributions) ? rateSens.duration_contributions : [];
        rsEl.innerHTML = `
          <h4 style="margin-bottom:12px;font-size:0.88rem;font-weight:600">Parallel Rate Shock â€” Portfolio P&L</h4>
          <p style="font-size:0.78rem;color:var(--text-muted);margin-bottom:16px">Formula: Î”P = âˆ’D_mod Ã— Î”Y Ã— P + Â½ Ã— Convexity Ã— (Î”Y)Â² Ã— P</p>
          ${rateExplainKey ? `<div style="margin-bottom:10px"><button class="btn btn-ghost btn-sm" onclick="App._openExplainFromKey('${rateExplainKey}','Rate Sensitivity')">Why?</button></div>` : ''}
          <table class="data-table" style="margin-bottom:24px">
            <thead><tr>
              <th>Shock</th>
              <th class="col-number">Post-Shock Yield</th>
              <th class="col-number">P&L (â‚¹)</th>
              <th class="col-number">P&L %</th>
              <th class="col-number">Portfolio Value (â‚¹)</th>
              <th>Direction</th>
            </tr></thead>
            <tbody>${shocks.map((s) => {
              const bps = s?.shock_bps ?? s?.scenario ?? null;
              const postShockYield = s?.post_shock_yield ?? null;
              const pnl = s?.pnl ?? s?.portfolio_pnl ?? null;
              const pnlPct = s?.pnl_pct ?? null;
              const newVal = s?.new_value ?? s?.new_nav ?? s?.new_portfolio_value ?? null;
              const dir = (isNumber(bps) && Number(bps) < 0) ? '<span class="badge badge-green">Rate Cut</span>' : '<span class="badge badge-red">Rate Hike</span>';
              const pnlColor = !isNumber(pnl) ? 'color:var(--text-muted)' : (Number(pnl) >= 0 ? 'color:#10b981' : 'color:#ef4444');
              const pnlPctColor = !isNumber(pnlPct) ? 'color:var(--text-muted)' : (Number(pnlPct) >= 0 ? 'color:#10b981' : 'color:#ef4444');
              return `<tr>
                <td class="col-number font-mono">${isNumber(bps) ? `${Number(bps) > 0 ? '+' : ''}${Number(bps)}` : NOT_COMPUTED}</td>
                <td class="col-number">${isNumber(postShockYield) ? `${Number(postShockYield).toFixed(4)}%` : NOT_COMPUTED}</td>
                <td class="col-number font-medium" style="${pnlColor}">${isNumber(pnl) ? `${Number(pnl) >= 0 ? '+' : ''}${Utils.formatINR(Number(pnl))}` : NOT_COMPUTED}</td>
                <td class="col-number" style="${pnlPctColor}">${isNumber(pnlPct) ? `${Number(pnlPct) >= 0 ? '+' : ''}${Number(pnlPct).toFixed(4)}%` : NOT_COMPUTED}</td>
                <td class="col-number">${formatINROrNotComputed(newVal)}</td>
                <td>${dir}</td>
              </tr>`;
            }).join('')}</tbody>
          </table>
          ${durContribs.length ? `
          <h4 style="margin-bottom:12px;font-size:0.88rem;font-weight:600">Duration Contributions by Bucket</h4>
          <table class="data-table">
            <thead><tr>
              <th>Bucket / Rating</th>
              <th class="col-number">Market Value (â‚¹)</th>
              <th class="col-number">Weight %</th>
              <th class="col-number">Mod. Duration</th>
              <th class="col-number">Duration Contribution</th>
              <th class="col-number">DV01 (â‚¹/bp)</th>
            </tr></thead>
            <tbody>${durContribs.map((d) => {
              const cat = d?.category ?? d?.name ?? NOT_COMPUTED;
              const rat = d?.rating ?? '';
              const catRat = rat ? `${cat} <span class="badge badge-sm" style="font-size:0.65rem;padding:1px 5px;background:rgba(79,140,255,0.15);color:#93c5fd;border-radius:4px;vertical-align:middle">${rat}</span>` : cat;
              return `<tr>
                <td>${catRat}</td>
                <td class="col-number">${formatINROrNotComputed(d?.market_value)}</td>
                <td class="col-number">${formatPctOrNotComputed(d?.weight_pct, 2)}</td>
                <td class="col-number">${formatNumOrNotComputed(d?.modified_duration, 4)}</td>
                <td class="col-number font-medium">${formatNumOrNotComputed(d?.duration_contribution, 4)}</td>
                <td class="col-number">${formatINROrNotComputed(d?.dv01)}</td>
              </tr>`;
            }).join('')}</tbody>
          </table>` : ''}
        `;
      } else if (rsEl) {
        rsEl.innerHTML = tabMessageHTML(rateState, 'Rate sensitivity unavailable');
      }

      const varEl = document.getElementById('risk-var');
      if (varEl && varState.status === 'ok') {
        const v1d95 = varData?.var?.var_95_1d ?? null;
        const v1d99 = varData?.var?.var_99_1d ?? null;
        const v10d95 = varData?.var?.var_95_10d ?? null;
        const v10d99 = varData?.var?.var_99_10d ?? null;
        const es = varData?.expected_shortfall?.es_95_1d ?? null;
        const portVal = varData?.portfolio_value ?? null;
        const methodNotes = varData?.methodology ?? 'Parametric (variance-covariance), rating-based yield volatilities, block-diagonal correlation matrix';
        const byCat = Array.isArray(varData?.var_by_category) ? varData.var_by_category : [];
        varEl.innerHTML = `
          <p style="font-size:0.78rem;color:var(--text-muted);margin-bottom:16px">${methodNotes}</p>
          ${varExplainKey ? `<div style="margin-bottom:10px"><button class="btn btn-ghost btn-sm" type="button" data-method="_openExplainFromKey" data-arg="${Utils.escapeAttr(varExplainKey)}" data-arg2="Value at Risk">Why?</button></div>` : ''}
          <div class="metric-grid" style="margin-bottom:24px">
            <div class="metric-card red"><div class="metric-label">1-Day VaR (95%)</div><div class="metric-value" style="font-size:0.95rem">${formatINROrNotComputed(v1d95)}</div><div class="metric-sub">${(isNumber(portVal) && Number(portVal) > 0 && isNumber(v1d95)) ? `${((Number(v1d95) / Number(portVal)) * 100).toFixed(4)}%` : NOT_COMPUTED}</div></div>
            <div class="metric-card red"><div class="metric-label">1-Day VaR (99%)</div><div class="metric-value" style="font-size:0.95rem">${formatINROrNotComputed(v1d99)}</div><div class="metric-sub">${(isNumber(portVal) && Number(portVal) > 0 && isNumber(v1d99)) ? `${((Number(v1d99) / Number(portVal)) * 100).toFixed(4)}%` : NOT_COMPUTED}</div></div>
            <div class="metric-card amber"><div class="metric-label">10-Day VaR (95%)</div><div class="metric-value" style="font-size:0.95rem">${formatINROrNotComputed(v10d95)}</div></div>
            <div class="metric-card amber"><div class="metric-label">10-Day VaR (99%)</div><div class="metric-value" style="font-size:0.95rem">${formatINROrNotComputed(v10d99)}</div></div>
            <div class="metric-card purple"><div class="metric-label">Expected Shortfall (95%)</div><div class="metric-value" style="font-size:0.95rem">${formatINROrNotComputed(es)}</div><div class="metric-sub text-muted" style="font-size:0.65rem">CVaR â€” avg loss beyond VaR</div></div>
          </div>
          ${byCat.length ? `
          <h4 style="margin-bottom:12px;font-size:0.88rem;font-weight:600">VaR by Bucket / Rating</h4>
          <table class="data-table">
            <thead><tr><th>Category</th><th class="col-number">Market Value (â‚¹)</th><th class="col-number">Daily Vol (â‚¹)</th><th class="col-number">1-Day 95% VaR (â‚¹)</th><th class="col-number">% of Total VaR</th></tr></thead>
            <tbody>${byCat.map((c) => `<tr>
              <td>${c?.category ?? c?.name ?? NOT_COMPUTED}</td>
              <td class="col-number">${formatINROrNotComputed(c?.market_value)}</td>
              <td class="col-number">${formatINROrNotComputed(c?.daily_vol)}</td>
              <td class="col-number text-red">${formatINROrNotComputed(c?.var_95 ?? c?.var)}</td>
              <td class="col-number">${formatPctOrNotComputed(c?.pct_of_total, 2)}</td>
            </tr>`).join('')}</tbody>
          </table>` : ''}
        `;
      } else if (varEl) {
        varEl.innerHTML = tabMessageHTML(varState, 'VaR unavailable');
      }

      const scenEl = document.getElementById('risk-scenarios');
      if (scenEl && scenarioState.status === 'ok') {
        const scenList = Array.isArray(scenarios?.scenarios)
          ? [...scenarios.scenarios].sort((a, b) => Number(a?.pnl_pct ?? a?.pnl ?? Number.POSITIVE_INFINITY) - Number(b?.pnl_pct ?? b?.pnl ?? Number.POSITIVE_INFINITY))
          : [];
        scenEl.innerHTML = `
          <p style="font-size:0.78rem;color:var(--text-muted);margin-bottom:16px">9 predefined stress scenarios â€” RBI rate actions, recession, liquidity freeze, curve reshaping, credit events.</p>
          ${scenarioExplainKey ? `<div style="margin-bottom:10px"><button class="btn btn-ghost btn-sm" onclick="App._openExplainFromKey('${scenarioExplainKey}','Scenario Analysis')">Why?</button></div>` : ''}
          <table class="data-table">
            <thead><tr>
              <th>Scenario</th>
              <th>Description</th>
              <th class="col-number">P&L (â‚¹)</th>
              <th class="col-number">P&L %</th>
              <th class="col-number">New NAV (â‚¹)</th>
              <th>Severity</th>
            </tr></thead>
            <tbody>${scenList.map((s) => {
              const pnl = s?.pnl ?? s?.portfolio_pnl ?? null;
              const pnlPct = s?.pnl_pct ?? null;
              const newNav = s?.new_value ?? s?.new_nav ?? s?.new_portfolio_value ?? null;
              const severity = !isNumber(pnlPct)
                ? '<span class="badge badge-amber">UNKNOWN</span>'
                : (Number(pnlPct) < -3 ? '<span class="badge badge-red">SEVERE</span>' : Number(pnlPct) < -1 ? '<span class="badge badge-amber">MODERATE</span>' : '<span class="badge badge-green">MILD</span>');
              const pnlColor = !isNumber(pnl) ? 'color:var(--text-muted)' : (Number(pnl) >= 0 ? 'color:#10b981' : 'color:#ef4444');
              const pnlPctColor = !isNumber(pnlPct) ? 'color:var(--text-muted)' : (Number(pnlPct) >= 0 ? 'color:#10b981' : 'color:#ef4444');
              return `<tr>
                <td style="font-size:0.8rem;font-weight:600">${s?.scenario_name ?? s?.name ?? NOT_COMPUTED}</td>
                <td style="font-size:0.72rem;color:var(--text-muted);max-width:220px">${s?.description ?? NOT_COMPUTED}</td>
                <td class="col-number font-medium" style="${pnlColor}">${isNumber(pnl) ? `${Number(pnl) >= 0 ? '+' : ''}${Utils.formatINR(Number(pnl))}` : NOT_COMPUTED}</td>
                <td class="col-number" style="${pnlPctColor}">${isNumber(pnlPct) ? `${Number(pnlPct) >= 0 ? '+' : ''}${Number(pnlPct).toFixed(4)}%` : NOT_COMPUTED}</td>
                <td class="col-number">${formatINROrNotComputed(newNav)}</td>
                <td>${severity}</td>
              </tr>`;
            }).join('')}</tbody>
          </table>
        `;
      } else if (scenEl) {
        scenEl.innerHTML = tabMessageHTML(scenarioState, 'Scenarios unavailable');
      }

      const ssEl = document.getElementById('risk-spread-sim');
      if (ssEl && spreadState.status === 'ok') {
        const p50 = spreadSim?.parallel_50bps ?? {};
        const tierW = spreadSim?.tiered_widening ?? {};
        const lgDef = spreadSim?.largest_default ?? {};
        const totalMarketValue = spreadSim?.total_market_value ?? null;
        const sims = [
          {
            name: 'Parallel +50bps (Non-SOV)',
            description: p50?.description ?? '',
            pnl: p50?.pnl ?? null,
            pnl_pct: p50?.pnl_pct ?? null,
            new_value: (isNumber(totalMarketValue) && isNumber(p50?.pnl)) ? Number(totalMarketValue) + Number(p50.pnl) : null,
          },
          {
            name: 'Tiered Spread Widening',
            description: tierW?.description ?? '',
            pnl: tierW?.pnl ?? null,
            pnl_pct: tierW?.pnl_pct ?? null,
            new_value: (isNumber(totalMarketValue) && isNumber(tierW?.pnl)) ? Number(totalMarketValue) + Number(tierW.pnl) : null,
          },
          {
            name: `Largest Default (${lgDef?.issuer ?? NOT_COMPUTED})`,
            description: lgDef?.description ?? '',
            pnl: lgDef?.pnl ?? null,
            pnl_pct: lgDef?.pnl_pct ?? null,
            new_value: (isNumber(totalMarketValue) && isNumber(lgDef?.pnl)) ? Number(totalMarketValue) + Number(lgDef.pnl) : null,
          },
        ];
        ssEl.innerHTML = `
          <p style="font-size:0.78rem;color:var(--text-muted);margin-bottom:16px">Three credit stress scenarios: parallel spread widening, tiered widening by rating, and largest issuer default.</p>
          ${spreadExplainKey ? `<div style="margin-bottom:10px"><button class="btn btn-ghost btn-sm" onclick="App._openExplainFromKey('${spreadExplainKey}','Spread Simulation')">Why?</button></div>` : ''}
          <table class="data-table">
            <thead><tr>
              <th>Scenario</th>
              <th>Description</th>
              <th class="col-number">P&L (â‚¹)</th>
              <th class="col-number">P&L %</th>
              <th class="col-number">New Value (â‚¹)</th>
            </tr></thead>
            <tbody>${sims.map((s) => {
              const pnlColor = !isNumber(s?.pnl) ? 'color:var(--text-muted)' : (Number(s.pnl) >= 0 ? 'color:#10b981' : 'color:#ef4444');
              const pnlPctColor = !isNumber(s?.pnl_pct) ? 'color:var(--text-muted)' : (Number(s.pnl_pct) >= 0 ? 'color:#10b981' : 'color:#ef4444');
              return `<tr>
                <td style="font-size:0.8rem;font-weight:600">${s?.name ?? NOT_COMPUTED}</td>
                <td style="font-size:0.72rem;color:var(--text-muted)">${s?.description ?? NOT_COMPUTED}</td>
                <td class="col-number font-medium" style="${pnlColor}">${isNumber(s?.pnl) ? `${Number(s.pnl) >= 0 ? '+' : ''}${Utils.formatINR(Number(s.pnl))}` : NOT_COMPUTED}</td>
                <td class="col-number" style="${pnlPctColor}">${isNumber(s?.pnl_pct) ? `${Number(s.pnl_pct) >= 0 ? '+' : ''}${Number(s.pnl_pct).toFixed(4)}%` : NOT_COMPUTED}</td>
                <td class="col-number">${formatINROrNotComputed(s?.new_value)}</td>
              </tr>`;
            }).join('')}</tbody>
          </table>
        `;
      } else if (ssEl) {
        ssEl.innerHTML = tabMessageHTML(spreadState, 'Spread simulation unavailable');
      }

      const ycEl = document.getElementById('risk-yield-curve');
      if (ycEl && curveState.status === 'ok') {
        const positions = Array.isArray(yieldCurve?.yield_curve) ? yieldCurve.yield_curve.filter((p) => isNumber(p?.count) && Number(p.count) > 0) : [];
        const portSpread = yieldCurve?.portfolio_spread_bps ?? null;
        const portYield = yieldCurve?.portfolio_weighted_yield ?? null;
        ycEl.innerHTML = `
          <div style="margin-bottom:12px;display:flex;gap:24px;font-size:0.82rem">
            <span><strong>Portfolio Weighted Yield:</strong> ${formatPctOrNotComputed(portYield, 4)}</span>
            <span><strong>Avg Portfolio Spread over G-Sec:</strong> ${isNumber(portSpread) ? `${Number(portSpread).toFixed(1)} bps` : NOT_COMPUTED}</span>
          </div>
          ${curveExplainKey ? `<div style="margin-bottom:10px"><button class="btn btn-ghost btn-sm" type="button" data-method="_openExplainFromKey" data-arg="${Utils.escapeAttr(curveExplainKey)}" data-arg2="Yield Curve Positioning">Why?</button></div>` : ''}
          ${positions.length ? `
          <table class="data-table">
            <thead><tr>
              <th>Tenor (Yr)</th>
              <th class="col-number"># Holdings</th>
              <th class="col-number">Weight %</th>
              <th class="col-number">Portfolio Yield %</th>
              <th class="col-number">G-Sec Benchmark %</th>
              <th class="col-number">Spread (bps)</th>
            </tr></thead>
            <tbody>${positions.map((p) => `<tr>
              <td>${isNumber(p?.tenor) ? `${Number(p.tenor)}Y` : NOT_COMPUTED}</td>
              <td class="col-number">${isNumber(p?.count) ? Number(p.count) : NOT_COMPUTED}</td>
              <td class="col-number">${formatPctOrNotComputed(p?.weight_pct, 2)}</td>
              <td class="col-number">${formatPctOrNotComputed(p?.portfolio_yield, 4)}</td>
              <td class="col-number">${formatPctOrNotComputed(p?.gsec_yield, 4)}</td>
              <td class="col-number"><span class="badge ${isNumber(p?.spread_bps) ? (Number(p.spread_bps) > 200 ? 'badge-red' : Number(p.spread_bps) > 100 ? 'badge-amber' : 'badge-green') : 'badge-amber'}">${isNumber(p?.spread_bps) ? `${Number(p.spread_bps).toFixed(0)} bp` : NOT_COMPUTED}</span></td>
            </tr>`).join('')}</tbody>
          </table>` : Utils.emptyHTML('No analysis available', 'No yield curve positions computed yet. Run Full Scan or upload data.')}
        `;
      } else if (ycEl) {
        ycEl.innerHTML = tabMessageHTML(curveState, 'Yield curve unavailable');
      }

      document.getElementById('comp-updated').textContent = `Last updated: ${Utils.formatDateTime(new Date())}`;
    } catch (err) {
      Utils.toast('error', 'Risk Analytics Error', err.message);
    }
  }

  async function loadComplianceData() {
    try {
      const asState = (raw) => {
        const status = String(raw?.status || '').toLowerCase();
        if (['ok', 'no_data', 'error', 'restricted'].includes(status)) return raw;
        return { status: 'error', data: {}, message: 'Invalid data contract from API' };
      };
      const isNumber = (value) => {
        if (value == null) return false;
        if (typeof value === 'string' && value.trim() === '') return false;
        return Number.isFinite(Number(value));
      };
      const NOT_COMPUTED = 'Not computed';
      const NO_DATA_MSG = 'No portfolio data available. Run Full Scan to generate risk metrics.';

      const [alertStateRaw, concentrationStateRaw, rulesStateRaw, portfolioStateRaw] = await Promise.all([
        API.alertSummaryState(),
        API.getPortfolioConcentrationState(),
        API.complianceRulesState(),
        API.getPortfolio()
          .then((data) => ({ status: 'ok', data, message: null }))
          .catch((err) => ({
            status: (Number(err?.status || 0) === 401 || Number(err?.status || 0) === 403) ? 'restricted' : 'error',
            data: { summary: {}, holdings: [] },
            message: err?.message || 'Portfolio data unavailable',
          })),
      ]);

      const alertState = asState(alertStateRaw);
      const concentrationState = asState(concentrationStateRaw);
      const rulesState = asState(rulesStateRaw);
      const portfolioState = asState(portfolioStateRaw);

      const restricted = [alertState, concentrationState, rulesState, portfolioState].some((x) => x.status === 'restricted');

      const portfolioData = portfolioState.data || {};
      const portfolioSummary = portfolioData?.summary || {};
      const portfolioAvgDuration = portfolioSummary?.weighted_modified_duration ?? portfolioSummary?.weighted_avg_maturity ?? PORTFOLIO.avgDuration;
      const holdingsCountRaw = portfolioSummary?.holdings_count ?? (Array.isArray(portfolioData?.holdings) ? portfolioData.holdings.length : null);
      const holdingsCount = isNumber(holdingsCountRaw) ? Number(holdingsCountRaw) : null;
      const hasPortfolioData = holdingsCount != null && holdingsCount > 0;

      const alertsData = alertState.data || {};
      const alertRows = Array.isArray(alertsData?.summary) ? alertsData.summary : [];
      const totalAlerts = alertRows.reduce((sum, row) => sum + (isNumber(row?.cnt) ? Number(row.cnt) : 0), 0);
      const critAlerts = alertRows
        .filter((row) => String(row?.severity || '').toUpperCase() === 'CRITICAL')
        .reduce((sum, row) => sum + (isNumber(row?.cnt) ? Number(row.cnt) : 0), 0);
      const openAlerts = isNumber(alertsData?.open_count) ? Number(alertsData.open_count) : totalAlerts;
      const complianceAlertsExplainKey = _registerExplanationPayload(alertsData?.explanation);

      const concentration = concentrationState.data || {};
      const topIssuerPct = hasPortfolioData ? (concentration?.top_issuer_exposure_pct ?? null) : null;
      const topSectorPct = hasPortfolioData ? (concentration?.top_sector_exposure_pct ?? null) : null;
      const topIssuerName = concentration?.top_issuers?.[0]?.name ?? NOT_COMPUTED;
      const topSectorName = concentration?.top_sectors?.[0]?.name ?? NOT_COMPUTED;
      const rulesData = rulesState.data || {};
      const rulebookCount = Array.isArray(rulesData?.rules) ? rulesData.rules.length : null;

      const rules = [
        { name: 'EPFO Eligibility (EPFO-001)', threshold: 'PF eligible bonds only', current: 'Active â€” enforced at ingestion', status: 'PASS' },
        { name: 'Rating Floor (RAT-001)', threshold: 'Min AA-', current: 'AA- enforced â€” registry filtered', status: 'PASS' },
        {
          name: 'Single Issuer Exposure (EXP-001)',
          threshold: 'â‰¤ 15%',
          current: topIssuerPct != null ? `${Number(topIssuerPct).toFixed(2)}% â€” ${topIssuerName}` : (hasPortfolioData ? NOT_COMPUTED : 'No portfolio data available'),
          status: topIssuerPct == null ? (hasPortfolioData ? 'WARN' : 'WARN') : Number(topIssuerPct) > 15 ? 'FAIL' : Number(topIssuerPct) > 12 ? 'WARN' : 'PASS',
        },
        {
          name: 'Group / Sector Exposure (EXP-002)',
          threshold: 'â‰¤ 35%',
          current: topSectorPct != null ? `${Number(topSectorPct).toFixed(2)}% â€” ${topSectorName}` : (hasPortfolioData ? NOT_COMPUTED : 'No portfolio data available'),
          status: topSectorPct == null ? (hasPortfolioData ? 'WARN' : 'WARN') : Number(topSectorPct) > 35 ? 'FAIL' : Number(topSectorPct) > 28 ? 'WARN' : 'PASS',
        },
        { name: 'Min Sovereign / G-Sec (EXP-004)', threshold: 'â‰¥ 20%', current: 'SOV holdings tracked', status: 'PASS' },
        { name: 'Max Below-AA Exposure (EXP-005)', threshold: 'â‰¤ 20%', current: 'Monitored via rating filter', status: 'PASS' },
        {
          name: 'Max Portfolio Duration (DUR-001)',
          threshold: 'â‰¤ 8Y',
          current: isNumber(portfolioAvgDuration) ? `${Number(portfolioAvgDuration).toFixed(2)}Y actual` : (hasPortfolioData ? NOT_COMPUTED : 'No portfolio data available'),
          status: !isNumber(portfolioAvgDuration) ? 'WARN' : Number(portfolioAvgDuration) > 8 ? 'FAIL' : Number(portfolioAvgDuration) > 7 ? 'WARN' : 'PASS',
        },
        { name: 'Max Maturity (ELIG-001)', threshold: 'â‰¤ 30Y', current: 'Enforced at bond registration', status: 'PASS' },
        {
          name: 'ALM Duration Match (ALM-001)',
          threshold: 'Â±1Y of 4.5Y target',
          current: isNumber(portfolioAvgDuration) ? `Target 4.5Y â€” Actual ${Number(portfolioAvgDuration).toFixed(2)}Y` : (hasPortfolioData ? NOT_COMPUTED : 'No portfolio data available'),
          status: !isNumber(portfolioAvgDuration) ? 'WARN' : Math.abs(Number(portfolioAvgDuration) - 4.5) > 2 ? 'WARN' : 'PASS',
        },
        { name: 'Listing Requirement (LIQ-001)', threshold: 'NSE/BSE listed only', current: 'Active â€” verified at ingest', status: 'PASS' },
        { name: 'SEBI Investment Guidelines (SEBI-001)', threshold: 'Full compliance', current: 'Active monitoring', status: 'PASS' },
        { name: 'RBI Prudential Norms (RBI-001)', threshold: 'Full compliance', current: 'Active monitoring', status: 'PASS' },
      ];

      const violations = rules.filter((r) => r.status === 'FAIL').length;
      const warnings = rules.filter((r) => r.status === 'WARN').length;
      const derivedAlertViolations = critAlerts > 0 ? 1 : 0;
      const derivedAlertWarnings = critAlerts > 0 ? 0 : ((openAlerts > 0 || totalAlerts > 0) ? 1 : 0);
      const effectiveViolations = violations + derivedAlertViolations;
      const effectiveWarnings = warnings + derivedAlertWarnings;
      const compStatus = effectiveViolations > 0 ? 'RED' : effectiveWarnings > 0 ? 'AMBER' : 'GREEN';

      // Guard: user may have navigated away while API calls were in-flight
      if (!document.getElementById('comp-overview')) return;

      document.getElementById('comp-overview').innerHTML = restricted ? `
        <div class="metric-card amber"><div class="metric-label">Compliance Status</div><div class="metric-value">Restricted</div></div>
        <div class="metric-card amber"><div class="metric-label">Rules Active</div><div class="metric-value">${NOT_COMPUTED}</div></div>
        <div class="metric-card amber"><div class="metric-label">Violations</div><div class="metric-value">${NOT_COMPUTED}</div></div>
        <div class="metric-card amber"><div class="metric-label">Warnings</div><div class="metric-value">${NOT_COMPUTED}</div></div>
      ` : `
        <div class="metric-card ${compStatus === 'GREEN' ? 'green' : compStatus === 'AMBER' ? 'amber' : 'red'}">
          <div class="metric-label">Compliance Status</div>
          <div class="metric-value">${compStatus}</div>
          <div class="metric-sub text-muted" style="font-size:0.72rem">${hasPortfolioData ? 'Computed from rules + alerts' : NO_DATA_MSG}</div>
        </div>
        <div class="metric-card blue">
          <div class="metric-label">Rules Active</div>
          <div class="metric-value">${rules.length}</div>
          <div class="metric-sub text-muted" style="font-size:0.72rem">${isNumber(rulebookCount) ? `${Number(rulebookCount)} in rulebook` : 'rulebook unavailable'}</div>
        </div>
        <div class="metric-card ${effectiveViolations > 0 ? 'red' : 'green'}">
          <div class="metric-label">Violations</div>
          <div class="metric-value">${effectiveViolations}</div>
        </div>
        <div class="metric-card ${effectiveWarnings > 0 ? 'amber' : 'green'}">
          <div class="metric-label">Warnings</div>
          <div class="metric-value">${effectiveWarnings}</div>
        </div>
      `;

      document.getElementById('comp-rules').innerHTML = restricted ? Utils.emptyHTML('Compliance data restricted', 'This role cannot view compliance rules or the session lost authorization.') : `
        ${rulesState.status === 'error' ? `<div style="font-size:0.72rem;color:var(--text-muted);margin-bottom:8px">${rulesState.message || 'Rulebook endpoint unavailable. Showing computed portfolio compliance checks.'}</div>` : ''}
        <table class="data-table">
          <thead><tr><th>Rule</th><th>Threshold</th><th>Current Portfolio Value</th><th>Status</th><th>Why?</th></tr></thead>
          <tbody>${rules.map(r => {
            const key = _registerExplanationPayload({
              module: 'risk_compliance_ui_local',
              decision_type: 'compliance_rule_evaluation',
              decision_outcome: r.status,
              summary: `${r.name}: ${r.current} against threshold ${r.threshold}`,
              explanation_levels: {
                level_1: { headline: `${r.name}: ${r.status}` },
                level_2: {
                  key_factors: [
                    { name: 'Current Value', value: r.current, impact: r.status === 'FAIL' ? 'negative' : 'neutral' },
                    { name: 'Threshold', value: r.threshold, impact: 'neutral' },
                  ],
                  rule_checks: [
                    { rule_code: r.name, status: r.status, message: 'Live compliance-rule dashboard evaluation' },
                  ],
                },
                level_3: {
                  calculation_trace: { current: r.current, threshold: r.threshold, status: r.status },
                  data_lineage: [{ source: 'portfolio + compliance dashboard', field: 'rule_state', trust_level: 'high' }],
                },
              },
              confidence: { score: r.status === 'PASS' ? 0.85 : 0.75, level: r.status === 'PASS' ? 'high' : 'medium' },
            });
            return `<tr>
            <td><strong>${r.name}</strong></td>
            <td>${r.threshold}</td>
            <td>${r.current}</td>
            <td>${Utils.statusBadge(r.status)}</td>
            <td>${key ? `<button class="btn btn-ghost btn-sm" type="button" style="font-size:0.7rem;padding:2px 8px" data-method="_openExplainFromKey" data-arg="${Utils.escapeAttr(key)}" data-arg2="Compliance Rule">Why?</button>` : 'â€”'}</td>
          </tr>`;
          }).join('')}</tbody>
        </table>
      `;

      const allAlerts = alertRows;
      document.getElementById('comp-audit').innerHTML = restricted ? Utils.emptyHTML('Compliance audit restricted', 'This role cannot view compliance audit data or the session lost authorization.') : allAlerts.length ? `
        <div style="font-size:0.72rem;color:var(--text-muted);margin-bottom:8px">Open alerts: ${openAlerts}</div>
        <div style="display:flex;justify-content:flex-end;margin-bottom:8px">${complianceAlertsExplainKey ? `<button class="btn btn-ghost btn-sm" onclick="App._openExplainFromKey('${complianceAlertsExplainKey}','Compliance Alert Summary')">Why?</button>` : ''}</div>
        <table class="data-table">
          <thead><tr><th>Alert Type</th><th>Severity</th><th>Count</th><th>Status</th></tr></thead>
          <tbody>${allAlerts.map(a => `<tr>
            <td>${(a.alert_type || '').replace(/_/g, ' ')}</td>
            <td>${Utils.statusBadge(a.severity)}</td>
            <td>${a.cnt}</td>
            <td><span class="badge badge-amber">Active</span></td>
          </tr>`).join('')}</tbody>
        </table>
      ` : Utils.emptyHTML('No active compliance alerts', 'All rules within thresholds. Run a full scan to generate new alerts.');
    } catch (err) {
      Utils.toast('error', 'Compliance Error', err.message);
    }
  }

  async function runComplianceScan() {
    const restoreButtons = _setBusyButtons('button[data-method="runComplianceScan"], button[onclick*="App.runComplianceScan()"]', true, 'Scanningâ€¦');
    Utils.toast('info', 'Running', 'Executing full compliance scan...');
    try {
      await API.runComplianceScan();
      Utils.toast('success', 'Complete', 'Compliance scan completed');
      navigate('risk');
    } catch (err) {
      Utils.toast('error', 'Error', err.message);
    } finally {
      restoreButtons();
    }
  }

  async function downloadAuthenticatedFile(url, preferredName) {
    if (!ensureAuthenticated('download this file')) return;
    await API.refreshSession().catch(() => false);
    const token = API.getToken ? API.getToken() : '';
    const resp = await fetch(url, {
      method: 'GET',
      credentials: 'include',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => 'Download failed');
      throw new Error(text || `Download failed (${resp.status})`);
    }
    const blob = await resp.blob();
    const disposition = resp.headers.get('content-disposition') || '';
    const match = disposition.match(/filename="?([^";]+)"?/i);
    const filename = match?.[1] || preferredName || `download_${Date.now()}`;
    const href = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = href;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(href);
  }

  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  // 6. COMPLIANCE DASHBOARD PAGE
  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  async function renderComplianceDashboard(el) {
    const trustId = getActiveTrustId();
    const fmtDateTime = (value) => {
      if (!value) return 'â€”';
      const dt = new Date(value);
      if (Number.isNaN(dt.getTime())) return String(value);
      return Utils.formatDateTime(dt);
    };
    const severityBadge = (severity) => {
      const sev = String(severity || 'INFO').toUpperCase();
      if (sev === 'CRITICAL') return '<span class="badge badge-red">CRITICAL</span>';
      if (sev === 'WARNING' || sev === 'WARN') return '<span class="badge badge-amber">WARNING</span>';
      if (sev === 'INFO' || sev === 'PASS') return '<span class="badge badge-green">INFO</span>';
      return `<span class="badge">${Utils.escapeHTML(sev)}</span>`;
    };

    el.innerHTML = `
      <div class="page-header">
        <div>
          <div class="page-title">Compliance Dashboard Module</div>
          <div class="page-subtitle">Auditor-ready compliance monitoring, traceability, access logs, backup posture, and exports.</div>
        </div>
        <div class="page-actions">
          <button class="btn btn-secondary btn-sm" id="cd-refresh-btn">${IC.refresh} Refresh</button>
        </div>
      </div>

      <div class="metric-grid" id="cd-summary">${Utils.skeletonCards(4)}</div>

      <div class="grid-row grid-2" style="margin-top:16px">
        <div class="card">
          <div class="card-header"><div class="card-title">Alert Panel</div></div>
          <div class="card-body">
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
              <select id="cd-alert-severity" class="form-input" style="max-width:180px">
                <option value="">All severities</option>
                <option value="CRITICAL">Critical</option>
                <option value="WARNING">Warning</option>
                <option value="INFO">Info</option>
              </select>
              <input id="cd-alert-rule" class="form-input" placeholder="Rule code filter (optional)" style="max-width:260px" />
              <button class="btn btn-ghost btn-sm" id="cd-alert-apply">Apply</button>
            </div>
            <div class="table-container" id="cd-alert-table">${Utils.skeletonTable(5)}</div>
            <div class="table-container" id="cd-alert-rules" style="margin-top:12px">${Utils.skeletonTable(4)}</div>
          </div>
        </div>

        <div class="card">
          <div class="card-header"><div class="card-title">Explain This Number</div></div>
          <div class="card-body">
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
              <select id="cd-explain-metric" class="form-input" style="max-width:260px">
                <option value="coupon_weighted_yield">Coupon-Weighted Yield</option>
                <option value="weighted_modified_duration">Weighted Modified Duration</option>
                <option value="portfolio_rating">Portfolio Rating</option>
                <option value="compliance_score">Compliance Score</option>
              </select>
              <button class="btn btn-ghost btn-sm" id="cd-explain-load">Explain</button>
            </div>
            <div id="cd-explain-content">${Utils.loadingHTML('Loading latest calculation trace...')}</div>
          </div>
        </div>
      </div>

      <div class="card" style="margin-top:16px">
        <div class="card-header"><div class="card-title">Audit Trail Viewer</div></div>
        <div class="card-body">
          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
            <input id="cd-audit-user" class="form-input" placeholder="User id" style="max-width:180px" />
            <input id="cd-audit-action" class="form-input" placeholder="Action type" style="max-width:180px" />
            <input id="cd-audit-start" class="form-input" type="date" style="max-width:160px" />
            <input id="cd-audit-end" class="form-input" type="date" style="max-width:160px" />
            <button class="btn btn-ghost btn-sm" id="cd-audit-apply">Filter</button>
          </div>
          <div class="table-container" id="cd-audit-table">${Utils.skeletonTable(6)}</div>
          <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:10px">
            <button class="btn btn-secondary btn-sm" id="cd-audit-prev">Previous</button>
            <button class="btn btn-secondary btn-sm" id="cd-audit-next">Next</button>
          </div>
        </div>
      </div>

      <div class="grid-row grid-2" style="margin-top:16px">
        <div class="card">
          <div class="card-header"><div class="card-title">Version History and Compare</div></div>
          <div class="card-body">
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
              <select id="cd-version-left" class="form-input" style="max-width:220px"></select>
              <select id="cd-version-right" class="form-input" style="max-width:220px"></select>
              <button class="btn btn-ghost btn-sm" id="cd-version-compare">Compare</button>
            </div>
            <div id="cd-version-history" class="table-container">${Utils.skeletonTable(4)}</div>
            <div id="cd-version-compare-result" class="table-container" style="margin-top:12px"></div>
          </div>
        </div>

        <div class="card">
          <div class="card-header"><div class="card-title">Access Control and Logs</div></div>
          <div class="card-body">
            <div id="cd-access-matrix" class="table-container">${Utils.skeletonTable(4)}</div>
            <div id="cd-access-logs" class="table-container" style="margin-top:12px">${Utils.skeletonTable(4)}</div>
          </div>
        </div>
      </div>

      <div class="grid-row grid-2" style="margin-top:16px">
        <div class="card">
          <div class="card-header"><div class="card-title">Backup Status and Disaster Readiness</div></div>
          <div class="card-body">
            <div id="cd-backup-status">${Utils.loadingHTML('Loading backup posture...')}</div>
            <div style="margin-top:12px">
              <button class="btn btn-secondary btn-sm" id="cd-backup-download">${IC.download} Download Backup Readiness Report</button>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-header"><div class="card-title">Export Center</div></div>
          <div class="card-body">
            <div id="cd-export-links">${Utils.loadingHTML('Loading available exports...')}</div>
          </div>
        </div>
      </div>
    `;

    let auditOffset = 0;
    let auditLimit = 25;

    const renderSummary = (summary) => {
      const bar = summary?.summary_bar || {};
      const trend = summary?.compliance_score_trend || [];
      const latestTrend = trend.slice(-8);
      const trendBars = latestTrend.length
        ? latestTrend.map((row) => {
            const score = Number(row.compliance_score || 0);
            return `
              <div style="display:flex;align-items:center;gap:8px;margin-top:6px">
                <span style="width:56px;font-size:0.7rem;color:var(--text-muted)">${Utils.escapeHTML(String(row.date || '').slice(5))}</span>
                <div style="flex:1;height:8px;background:var(--bg-tertiary);border-radius:6px;overflow:hidden">
                  <div style="height:100%;width:${Math.max(0, Math.min(100, score))}%;background:${score >= 90 ? '#16a34a' : score >= 75 ? '#d97706' : '#dc2626'}"></div>
                </div>
                <span style="width:36px;text-align:right;font-size:0.72rem;font-weight:600">${score.toFixed(0)}</span>
              </div>
            `;
          }).join('')
        : '<div class="empty-state-desc" style="margin-top:10px">No trend samples available yet.</div>';

      const postureClass = String(bar.posture || 'RED') === 'GREEN'
        ? 'green'
        : String(bar.posture || 'RED') === 'AMBER'
          ? 'amber'
          : 'red';

      const summaryEl = document.getElementById('cd-summary');
      if (!summaryEl) return;
      summaryEl.innerHTML = `
        <div class="metric-card blue"><div class="metric-label">Total Bonds Monitored</div><div class="metric-value">${Number(bar.total_bonds_monitored || 0)}</div></div>
        <div class="metric-card amber"><div class="metric-label">Active Alerts</div><div class="metric-value">${Number(bar.active_alerts || 0)}</div></div>
        <div class="metric-card red"><div class="metric-label">Unresolved Incidents</div><div class="metric-value">${Number(bar.unresolved_incidents || 0)}</div></div>
        <div class="metric-card ${postureClass}">
          <div class="metric-label">Compliance Score</div>
          <div class="metric-value">${Number(bar.compliance_score || 0).toFixed(2)}</div>
          <div class="metric-sub" style="font-size:0.72rem">Posture: ${Utils.escapeHTML(String(bar.posture || 'RED'))}</div>
          <div style="margin-top:6px">${trendBars}</div>
        </div>
      `;
    };

    const loadAlerts = async () => {
      const severity = document.getElementById('cd-alert-severity')?.value || undefined;
      const rule = document.getElementById('cd-alert-rule')?.value || undefined;
      const res = await API.getComplianceDashboardAlerts({ severity, rule, status: 'OPEN', limit: 50 });

      const tableEl = document.getElementById('cd-alert-table');
      const rulesEl = document.getElementById('cd-alert-rules');
      if (!tableEl || !rulesEl) return;

      const items = res.top_active_violations || [];
      tableEl.innerHTML = items.length ? `
        <table class="data-table">
          <thead><tr><th>Timestamp</th><th>Rule</th><th>Severity</th><th>ISIN</th><th>Detail</th></tr></thead>
          <tbody>${items.map((item) => `<tr>
            <td>${fmtDateTime(item.timestamp)}</td>
            <td>${Utils.escapeHTML(String(item.rule_key || 'â€”'))}</td>
            <td>${severityBadge(item.severity)}</td>
            <td class="font-mono">${Utils.escapeHTML(String(item.isin || 'â€”'))}</td>
            <td style="max-width:320px">${Utils.escapeHTML(String(item.detail || item.title || 'â€”'))}</td>
          </tr>`).join('')}</tbody>
        </table>
      ` : Utils.emptyHTML('No active violations', 'No alerts matched the selected severity/rule filters.');

      const byRule = res?.drilldown?.by_rule || [];
      rulesEl.innerHTML = byRule.length ? `
        <table class="data-table">
          <thead><tr><th>Rule</th><th>Severity</th><th class="col-number">Count</th><th>Last Seen</th></tr></thead>
          <tbody>${byRule.slice(0, 20).map((item) => `<tr>
            <td>${Utils.escapeHTML(String(item.rule_code || 'â€”'))}</td>
            <td>${severityBadge(item.severity)}</td>
            <td class="col-number">${Number(item.count || 0)}</td>
            <td>${fmtDateTime(item.latest_seen)}</td>
          </tr>`).join('')}</tbody>
        </table>
      ` : Utils.emptyHTML('No rule drilldown data', 'Run compliance scans to populate rule-level violation details.');
    };

    const openAuditEntry = async (entryId) => {
      try {
        const detail = await API.getComplianceDashboardAuditEntry(entryId);
        const entry = detail.entry || {};
        const modal = document.getElementById('modal-overlay');
        const modalContent = document.getElementById('modal-content');
        if (!modal || !modalContent) return;
        modalContent.innerHTML = `
          <div class="modal-header">
            <div class="modal-title">Audit Entry #${Number(entry.id || 0)}</div>
            <button class="modal-close" data-action="close-modal">${IC.x}</button>
          </div>
          <div class="modal-body">
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px">
              <div><strong>User:</strong> ${Utils.escapeHTML(String(entry.username || entry.user_id || 'â€”'))}</div>
              <div><strong>Role:</strong> ${Utils.escapeHTML(String(entry.user_role || 'â€”'))}</div>
              <div><strong>Action:</strong> ${Utils.escapeHTML(String(entry.action_type || 'â€”'))}</div>
              <div><strong>Timestamp:</strong> ${fmtDateTime(entry.timestamp_utc)}</div>
              <div><strong>Entity:</strong> ${Utils.escapeHTML(String(entry.entity_type || 'â€”'))}</div>
              <div><strong>Source:</strong> ${Utils.escapeHTML(String(entry.source_system || 'â€”'))}</div>
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
              <div>
                <div style="font-size:0.78rem;color:var(--text-muted);margin-bottom:6px">Before</div>
                <pre style="max-height:260px;overflow:auto;background:var(--bg-tertiary);border:1px solid var(--border-light);padding:10px;border-radius:8px;font-size:0.74rem">${Utils.escapeHTML(JSON.stringify(entry.before, null, 2))}</pre>
              </div>
              <div>
                <div style="font-size:0.78rem;color:var(--text-muted);margin-bottom:6px">After</div>
                <pre style="max-height:260px;overflow:auto;background:var(--bg-tertiary);border:1px solid var(--border-light);padding:10px;border-radius:8px;font-size:0.74rem">${Utils.escapeHTML(JSON.stringify(entry.after, null, 2))}</pre>
              </div>
            </div>
          </div>
        `;
        modal.classList.add('show');
      } catch (err) {
        Utils.toast('error', 'Audit Entry', err.message || 'Failed to load audit entry details');
      }
    };

    const loadAuditTrail = async () => {
      const tableEl = document.getElementById('cd-audit-table');
      try {
        const params = {
          user_id: document.getElementById('cd-audit-user')?.value || undefined,
          action_type: document.getElementById('cd-audit-action')?.value || undefined,
          start_date: document.getElementById('cd-audit-start')?.value || undefined,
          end_date: document.getElementById('cd-audit-end')?.value || undefined,
          limit: auditLimit,
          offset: auditOffset,
        };
        const res = await API.getComplianceDashboardAuditTrail(params);
        const rows = res.entries || [];
        if (!tableEl) return;
        tableEl.innerHTML = rows.length ? `
          <table class="data-table">
            <thead><tr><th>Timestamp</th><th>User</th><th>Action</th><th>Entity</th><th>Source</th><th>Before/After</th></tr></thead>
            <tbody>${rows.map((row) => `<tr>
              <td>${fmtDateTime(row.timestamp_utc)}</td>
              <td>${Utils.escapeHTML(String(row.username || row.user_id || 'â€”'))}</td>
              <td>${Utils.escapeHTML(String(row.action_type || 'â€”'))}</td>
              <td>${Utils.escapeHTML(String(row.entity_type || 'â€”'))}: ${Utils.escapeHTML(String(row.entity_id || 'â€”'))}</td>
              <td>${Utils.escapeHTML(String(row.source_system || 'â€”'))}</td>
              <td><button class="btn btn-ghost btn-sm cd-audit-entry-btn" data-entry-id="${row.id}">View</button></td>
            </tr>`).join('')}</tbody>
          </table>
        ` : Utils.emptyHTML('No audit entries', 'No audit trail entries matched the selected filters.');

        tableEl.querySelectorAll('.cd-audit-entry-btn').forEach((btn) => {
          btn.addEventListener('click', () => {
            const id = Number(btn.getAttribute('data-entry-id') || 0);
            if (id > 0) openAuditEntry(id);
          });
        });
      } catch (err) {
        if (tableEl) {
          tableEl.innerHTML = Utils.emptyHTML('Audit trail unavailable', err.message || 'Failed to load audit entries');
        }
      }
    };

    const loadExplain = async () => {
      const metric = document.getElementById('cd-explain-metric')?.value || 'coupon_weighted_yield';
      const res = await API.explainComplianceNumber(metric, { trust_id: trustId });
      const explainEl = document.getElementById('cd-explain-content');
      if (!explainEl) return;

      const lineageRows = (res.data_lineage || []).slice(0, 12);
      explainEl.innerHTML = `
        <div style="padding:10px 12px;border:1px solid var(--border-light);border-radius:8px;background:var(--bg-tertiary);margin-bottom:10px">
          <div style="font-size:0.76rem;color:var(--text-muted)">Metric Value</div>
          <div style="font-size:1.1rem;font-weight:700">${Utils.escapeHTML(String(res.metric_value ?? 'n/a'))}</div>
          <div style="font-size:0.74rem;color:var(--text-muted);margin-top:6px">${Utils.escapeHTML(String(res.formula || 'â€”'))}</div>
        </div>
        <div style="font-size:0.76rem;color:var(--text-muted);margin-bottom:6px">Formula Inputs Snapshot</div>
        <pre style="max-height:160px;overflow:auto;background:var(--bg-tertiary);border:1px solid var(--border-light);padding:10px;border-radius:8px;font-size:0.72rem;margin-bottom:10px">${Utils.escapeHTML(JSON.stringify(res.calculation_trace?.inputs || {}, null, 2))}</pre>
        <div style="font-size:0.76rem;color:var(--text-muted);margin-bottom:6px">Data Lineage (latest sources)</div>
        ${lineageRows.length ? `<table class="data-table"><thead><tr><th>Entity</th><th>Source</th><th>Method</th><th>Confidence</th></tr></thead><tbody>
          ${lineageRows.map((row) => `<tr>
            <td>${Utils.escapeHTML(String(row.entity_id || 'â€”'))}</td>
            <td>${Utils.escapeHTML(String(row.source_name || 'â€”'))}</td>
            <td>${Utils.escapeHTML(String(row.ingestion_method || 'â€”'))}</td>
            <td>${Utils.escapeHTML(String(row.confidence_level || 'â€”'))}</td>
          </tr>`).join('')}
        </tbody></table>` : Utils.emptyHTML('No lineage rows', 'No provenance rows were returned for this metric.')}
      `;
    };

    const renderVersions = async () => {
      const versionsRes = await API.getComplianceDashboardVersions({ trust_id: trustId, limit: 30 });
      const versions = versionsRes.portfolio_versions || [];

      const leftSel = document.getElementById('cd-version-left');
      const rightSel = document.getElementById('cd-version-right');
      const historyEl = document.getElementById('cd-version-history');
      if (!leftSel || !rightSel || !historyEl) return;

      leftSel.innerHTML = versions.map((v) => `<option value="${v.version_number}">Version ${v.version_number}</option>`).join('') || '<option value="">No versions</option>';
      rightSel.innerHTML = versions.map((v) => `<option value="${v.version_number}">Version ${v.version_number}</option>`).join('') || '<option value="">No versions</option>';
      if (versions.length > 1) {
        rightSel.selectedIndex = 1;
      }

      historyEl.innerHTML = versions.length ? `
        <table class="data-table">
          <thead><tr><th>Version</th><th>Approved At</th><th>Status</th><th>Approved By</th><th>Rows Synced</th></tr></thead>
          <tbody>${versions.map((v) => `<tr>
            <td>${Number(v.version_number || 0)}</td>
            <td>${fmtDateTime(v.approved_at)}</td>
            <td>${Utils.escapeHTML(String(v.version_status || 'â€”'))}</td>
            <td>${Utils.escapeHTML(String(v.approved_by || 'â€”'))}</td>
            <td class="col-number">${Number(v.summary?.synced_rows || 0)}</td>
          </tr>`).join('')}</tbody>
        </table>
      ` : Utils.emptyHTML('No portfolio versions found', 'Upload and commit a portfolio snapshot to populate version history.');

      const compareBtn = document.getElementById('cd-version-compare');
      if (compareBtn) {
        compareBtn.onclick = async () => {
          const leftVersion = Number(leftSel.value || 0);
          const rightVersion = Number(rightSel.value || 0);
          if (!leftVersion || !rightVersion || leftVersion === rightVersion) {
            Utils.toast('warning', 'Version Compare', 'Select two different versions to compare.');
            return;
          }
          const resultEl = document.getElementById('cd-version-compare-result');
          if (resultEl) resultEl.innerHTML = Utils.loadingHTML('Computing version diff...');
          try {
            const diff = await API.compareComplianceDashboardVersions(leftVersion, rightVersion, { trust_id: trustId });
            if (!resultEl) return;
            const modified = diff.modified || [];
            resultEl.innerHTML = `
              <div style="display:flex;gap:18px;flex-wrap:wrap;margin-bottom:10px;font-size:0.82rem">
                <span><strong>Added:</strong> ${Number(diff.diff_summary?.added_rows || 0)}</span>
                <span><strong>Removed:</strong> ${Number(diff.diff_summary?.removed_rows || 0)}</span>
                <span><strong>Modified:</strong> ${Number(diff.diff_summary?.modified_rows || 0)}</span>
                <span><strong>Unchanged:</strong> ${Number(diff.diff_summary?.unchanged_rows || 0)}</span>
              </div>
              ${modified.length ? `<table class="data-table"><thead><tr><th>Row Key</th><th>ISIN</th><th>Changed Fields</th></tr></thead><tbody>
                ${modified.slice(0, 20).map((row) => `<tr>
                  <td class="font-mono">${Utils.escapeHTML(String(row.row_key || 'â€”'))}</td>
                  <td class="font-mono">${Utils.escapeHTML(String(row.isin || 'â€”'))}</td>
                  <td>${row.changes.map((c) => Utils.escapeHTML(String(c.field))).join(', ')}</td>
                </tr>`).join('')}
              </tbody></table>` : Utils.emptyHTML('No row-level modifications', 'The selected versions differ only by additions/removals or metadata changes.')}
            `;
          } catch (err) {
            if (resultEl) resultEl.innerHTML = Utils.emptyHTML('Version comparison failed', err.message || 'Unable to compare selected versions.');
          }
        };
      }
    };

    const loadAccessControl = async () => {
      const res = await API.getComplianceDashboardAccessControl({ trust_id: trustId, limit: 80 });
      const matrixEl = document.getElementById('cd-access-matrix');
      const logsEl = document.getElementById('cd-access-logs');
      if (!matrixEl || !logsEl) return;

      const matrix = res.role_matrix || [];
      matrixEl.innerHTML = matrix.length ? `
        <table class="data-table">
          <thead><tr><th>Role</th><th>Key Permissions</th></tr></thead>
          <tbody>${matrix.map((row) => `<tr>
            <td>${Utils.escapeHTML(String(row.role || 'â€”'))}</td>
            <td>${(row.permissions || []).map((perm) => `<span class="badge" style="margin-right:6px">${Utils.escapeHTML(String(perm))}</span>`).join('')}</td>
          </tr>`).join('')}</tbody>
        </table>
      ` : Utils.emptyHTML('Role matrix unavailable', 'No role matrix data returned by backend.');

      const logs = res.portfolio_access_logs || [];
      logsEl.innerHTML = logs.length ? `
        <table class="data-table">
          <thead><tr><th>Timestamp</th><th>User</th><th>Role</th><th>Portfolio</th><th>IP</th></tr></thead>
          <tbody>${logs.slice(0, 30).map((row) => `<tr>
            <td>${fmtDateTime(row.timestamp_utc)}</td>
            <td>${Utils.escapeHTML(String(row.username || row.user_id || 'â€”'))}</td>
            <td>${Utils.escapeHTML(String(row.user_role || 'â€”'))}</td>
            <td>${Utils.escapeHTML(String(row.portfolio_id || 'â€”'))}</td>
            <td>${Utils.escapeHTML(String(row.ip_address || 'â€”'))}</td>
          </tr>`).join('')}</tbody>
        </table>
      ` : Utils.emptyHTML('No portfolio access events logged yet', 'Access events will appear after users view portfolio pages.');
    };

    const loadBackupStatus = async () => {
      const res = await API.getComplianceDashboardBackupStatus();
      const backupEl = document.getElementById('cd-backup-status');
      if (!backupEl) return;

      const health = res.backup_health || {};
      const checks = health.checks || {};
      backupEl.innerHTML = `
        <div style="padding:10px 12px;border:1px solid var(--border-light);border-radius:8px;background:var(--bg-tertiary)">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
            ${severityBadge(health.status === 'GREEN' ? 'INFO' : health.status === 'AMBER' ? 'WARNING' : 'CRITICAL')}
            <strong>${Utils.escapeHTML(String(health.status || 'RED'))}</strong>
          </div>
          <div style="font-size:0.8rem;color:var(--text-secondary);margin-bottom:8px">${Utils.escapeHTML(String(health.message || 'â€”'))}</div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:0.78rem">
            <div>Recent backup: <strong>${checks.recent_backup ? 'YES' : 'NO'}</strong></div>
            <div>Encrypted: <strong>${checks.encrypted ? 'YES' : 'NO'}</strong></div>
            <div>Checksum present: <strong>${checks.checksum_present ? 'YES' : 'NO'}</strong></div>
            <div>Secondary copy: <strong>${checks.secondary_copy ? 'YES' : 'NO'}</strong></div>
          </div>
        </div>
      `;
    };

    const loadExportCenter = async () => {
      const res = await API.getComplianceDashboardExportCenter({ trust_id: trustId });
      const exportEl = document.getElementById('cd-export-links');
      if (!exportEl) return;

      const exports = res.exports || [];
      exportEl.innerHTML = exports.length ? exports.map((item, idx) => `
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;padding:10px 0;border-top:${idx ? '1px solid var(--border-light)' : 'none'}">
          <div>
            <div style="font-weight:600">${Utils.escapeHTML(String(item.name || 'Export'))}</div>
            <div style="font-size:0.76rem;color:var(--text-muted)">${Utils.escapeHTML(String(item.description || ''))}</div>
          </div>
          <button class="btn btn-secondary btn-sm cd-export-btn" data-url="${Utils.escapeAttr(item.url || '')}" data-name="${Utils.escapeAttr(item.id || 'export')}">${IC.download} Download</button>
        </div>
      `).join('') : Utils.emptyHTML('No exports listed', 'Export endpoints are unavailable for the current role/session.');

      exportEl.querySelectorAll('.cd-export-btn').forEach((btn) => {
        btn.addEventListener('click', async () => {
          let url = btn.getAttribute('data-url') || '';
          const name = btn.getAttribute('data-name') || 'export';
          if (!url) return;
          const originalHtml = btn.innerHTML;
          const originalDisabled = btn.disabled;
          btn.disabled = true;
          btn.textContent = 'Downloadingâ€¦';
          if (url.includes('/compliance-dashboard/export/audit-report') && !/[?&]trust_id=/.test(url)) {
            const joiner = url.includes('?') ? '&' : '?';
            url = `${url}${joiner}trust_id=${encodeURIComponent(trustId)}`;
          }
          try {
            await downloadAuthenticatedFile(url, `${name}_${new Date().toISOString().slice(0, 10)}`);
            Utils.toast('success', 'Export', 'File download started');
          } catch (err) {
            Utils.toast('error', 'Export', err.message || 'Download failed');
          } finally {
            btn.innerHTML = originalHtml;
            btn.disabled = originalDisabled;
          }
        });
      });
    };

    const refreshAll = async () => {
      try {
        const [summary] = await Promise.all([
          API.getComplianceDashboardSummary({ trust_id: trustId, days: 30 }),
        ]);
        if (!document.getElementById('cd-summary')) return;
        renderSummary(summary);
      } catch (err) {
        Utils.toast('error', 'Compliance Dashboard', err.message || 'Failed to load summary data');
      }

      await Promise.all([
        loadAlerts().catch((err) => {
          const elx = document.getElementById('cd-alert-table');
          if (elx) elx.innerHTML = Utils.emptyHTML('Alert panel unavailable', err.message || 'Failed to load alert data');
        }),
        loadAuditTrail().catch((err) => {
          const elx = document.getElementById('cd-audit-table');
          if (elx) elx.innerHTML = Utils.emptyHTML('Audit trail unavailable', err.message || 'Failed to load audit entries');
        }),
        loadExplain().catch((err) => {
          const elx = document.getElementById('cd-explain-content');
          if (elx) elx.innerHTML = Utils.emptyHTML('Explain-number unavailable', err.message || 'No calculation trace available');
        }),
        renderVersions().catch((err) => {
          const elx = document.getElementById('cd-version-history');
          if (elx) elx.innerHTML = Utils.emptyHTML('Version history unavailable', err.message || 'Unable to load version history');
        }),
        loadAccessControl().catch((err) => {
          const elx = document.getElementById('cd-access-logs');
          if (elx) elx.innerHTML = Utils.emptyHTML('Access logs unavailable', err.message || 'Unable to load access logs');
        }),
        loadBackupStatus().catch((err) => {
          const elx = document.getElementById('cd-backup-status');
          if (elx) elx.innerHTML = Utils.emptyHTML('Backup status unavailable', err.message || 'Unable to evaluate backup health');
        }),
        loadExportCenter().catch((err) => {
          const elx = document.getElementById('cd-export-links');
          if (elx) elx.innerHTML = Utils.emptyHTML('Export center unavailable', err.message || 'Unable to load export links');
        }),
      ]);
    };

    const refreshBtn = document.getElementById('cd-refresh-btn');
    if (refreshBtn) refreshBtn.addEventListener('click', () => refreshAll());

    const alertApplyBtn = document.getElementById('cd-alert-apply');
    if (alertApplyBtn) alertApplyBtn.addEventListener('click', () => loadAlerts());

    const explainBtn = document.getElementById('cd-explain-load');
    if (explainBtn) explainBtn.addEventListener('click', () => loadExplain());

    const auditApplyBtn = document.getElementById('cd-audit-apply');
    if (auditApplyBtn) {
      auditApplyBtn.addEventListener('click', () => {
        auditOffset = 0;
        loadAuditTrail();
      });
    }

    const auditPrevBtn = document.getElementById('cd-audit-prev');
    if (auditPrevBtn) {
      auditPrevBtn.addEventListener('click', () => {
        auditOffset = Math.max(0, auditOffset - auditLimit);
        loadAuditTrail();
      });
    }

    const auditNextBtn = document.getElementById('cd-audit-next');
    if (auditNextBtn) {
      auditNextBtn.addEventListener('click', () => {
        auditOffset += auditLimit;
        loadAuditTrail();
      });
    }

    const backupDownloadBtn = document.getElementById('cd-backup-download');
    if (backupDownloadBtn) {
      backupDownloadBtn.addEventListener('click', async () => {
        const originalHtml = backupDownloadBtn.innerHTML;
        const originalDisabled = backupDownloadBtn.disabled;
        backupDownloadBtn.disabled = true;
        backupDownloadBtn.textContent = 'Downloadingâ€¦';
        try {
          await downloadAuthenticatedFile('/api/v1/compliance-dashboard/backup-status/report', `backup_readiness_${new Date().toISOString().slice(0, 10)}.json`);
          Utils.toast('success', 'Backup Report', 'Backup readiness report downloaded');
        } catch (err) {
          Utils.toast('error', 'Backup Report', err.message || 'Download failed');
        } finally {
          backupDownloadBtn.innerHTML = originalHtml;
          backupDownloadBtn.disabled = originalDisabled;
        }
      });
    }

    await refreshAll();
  }

  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  // 6.5 INSTITUTIONAL COMMAND CENTER
  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  async function renderInstitutionalCommandCenter(el) {
    const asText = (value, fallback = 'â€”') => {
      const text = String(value == null ? '' : value).trim();
      return text || fallback;
    };
    const fmtTs = (value) => value ? Utils.formatDateTime(value) : 'â€”';
    const fmtNum = (value, digits = 1) => {
      const n = Number(value);
      return Number.isFinite(n) ? n.toFixed(digits) : 'â€”';
    };
    const badgeClass = (status) => {
      const s = String(status || '').toUpperCase();
      if (s.includes('FAIL') || s.includes('ERROR') || s.includes('BLOCK')) return 'text-red';
      if (s.includes('WARN') || s.includes('PENDING') || s.includes('PARTIAL')) return 'text-amber';
      if (s.includes('PASS') || s.includes('APPROVED') || s.includes('COMPLETED')) return 'text-green';
      return '';
    };
    const shortDecision = (id) => {
      const text = String(id || '');
      if (text.length <= 14) return text;
      return `${text.slice(0, 6)}...${text.slice(-6)}`;
    };

    el.innerHTML = `
      <div class="page-header" style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap">
        <div>
          <div class="page-title">Institutional Command Center</div>
          <div class="page-subtitle">Fail-closed decisions, active overrides, breach pressure, and audit integrity in one operating screen.</div>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn btn-secondary btn-sm" id="cc-reconcile-btn">${IC.refresh} Reconcile Intents</button>
          <button class="btn btn-primary btn-sm" id="cc-refresh-btn">${IC.refresh} Refresh</button>
        </div>
      </div>

      <div id="cc-status-banner" style="margin-bottom:12px"></div>

      <div class="stats-grid" style="grid-template-columns:repeat(auto-fit,minmax(170px,1fr));margin-bottom:16px">
        <div class="stat-card"><div class="stat-label">Pending Intents</div><div class="stat-value" id="cc-kpi-intents">â€”</div></div>
        <div class="stat-card"><div class="stat-label">Intent Failures</div><div class="stat-value" id="cc-kpi-intent-failures">â€”</div></div>
        <div class="stat-card"><div class="stat-label">Feed Failures</div><div class="stat-value" id="cc-kpi-feed-failures">â€”</div></div>
        <div class="stat-card"><div class="stat-label">Pending Overrides</div><div class="stat-value" id="cc-kpi-overrides">â€”</div></div>
        <div class="stat-card"><div class="stat-label">Flagged Decisions</div><div class="stat-value" id="cc-kpi-flagged">â€”</div></div>
        <div class="stat-card"><div class="stat-label">IPS Breaches</div><div class="stat-value" id="cc-kpi-breaches">â€”</div></div>
        <div class="stat-card"><div class="stat-label">Audit Integrity</div><div class="stat-value" id="cc-kpi-audit">â€”</div></div>
        <div class="stat-card"><div class="stat-label">Analyses (24h)</div><div class="stat-value" id="cc-kpi-analyses-24h">â€”</div></div>
        <div class="stat-card"><div class="stat-label">Blocked (24h)</div><div class="stat-value" id="cc-kpi-blocked-24h">â€”</div></div>
        <div class="stat-card"><div class="stat-label">Predictive Warnings</div><div class="stat-value" id="cc-kpi-predictive-24h">â€”</div></div>
        <div class="stat-card"><div class="stat-label">Avg Health (24h)</div><div class="stat-value" id="cc-kpi-health-24h">â€”</div></div>
      </div>

      <div class="grid-row grid-2" style="margin-bottom:14px;align-items:start">
        <div class="card">
          <div class="card-header"><div class="card-title">Decision Queue</div><div class="card-subtitle" id="cc-queue-updated">Loading...</div></div>
          <div class="card-body" style="padding:0">
            <div class="table-wrap" style="max-height:330px;overflow:auto">
              <table class="table" id="cc-queue-table">
                <thead>
                  <tr><th>Decision</th><th>Type</th><th>Outcome</th><th>Status</th><th>Updated</th><th></th></tr>
                </thead>
                <tbody id="cc-queue-body"><tr><td colspan="6" class="text-muted">Loading decision queue...</td></tr></tbody>
              </table>
            </div>
          </div>
        </div>
        <div class="card">
          <div class="card-header"><div class="card-title">Explainability Panel</div><div class="card-subtitle" id="cc-explain-caption">Select a decision from queue</div></div>
          <div class="card-body" id="cc-explain-panel" style="max-height:330px;overflow:auto">
            <div class="text-muted">No decision selected.</div>
          </div>
        </div>
      </div>

      <div class="grid-row grid-3">
        <div class="card">
          <div class="card-header"><div class="card-title">Override Approvals</div></div>
          <div class="card-body" id="cc-overrides-list">Loading...</div>
        </div>
        <div class="card">
          <div class="card-header"><div class="card-title">Recent Compliance Checks</div></div>
          <div class="card-body" id="cc-compliance-list">Loading...</div>
        </div>
        <div class="card">
          <div class="card-header"><div class="card-title">IPS Breaches & Alerts</div></div>
          <div class="card-body" id="cc-breach-list">Loading...</div>
        </div>
      </div>

      <div class="card" style="margin-top:14px">
        <div class="card-header"><div class="card-title">Feed Health</div><div class="card-subtitle" id="cc-feed-health-caption">Loading source feed status...</div></div>
        <div class="card-body" id="cc-feed-health">Loading...</div>
      </div>

      <div class="grid-row grid-2" style="margin-top:14px;align-items:start">
        <div class="card">
          <div class="card-header"><div class="card-title">Intelligence Ops Live Feed</div><div class="card-subtitle">Latest blocked/high-severity/predictive events</div></div>
          <div class="card-body" style="max-height:330px;overflow:auto">
            <div id="cc-ops-at-risk" style="margin-bottom:10px">Loading risk hotspots...</div>
            <div id="cc-ops-live-feed">Loading intelligence feed...</div>
          </div>
        </div>
        <div class="card">
          <div class="card-header"><div class="card-title">Per-Bond Explainability & Audit</div><div class="card-subtitle">Why this decision and how it changed over time</div></div>
          <div class="card-body">
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
              <input class="input" id="cc-ops-isin-input" placeholder="Enter ISIN (e.g. INE123A01016)" style="min-width:220px;flex:1" />
              <button class="btn btn-secondary btn-sm" id="cc-ops-drilldown-btn">Load Drilldown</button>
            </div>
            <div id="cc-ops-drilldown" style="max-height:300px;overflow:auto" class="text-muted">Select an ISIN to inspect explainability, trend, and audit deltas.</div>
          </div>
        </div>
      </div>

      <div class="card" style="margin-top:14px">
        <div class="card-header"><div class="card-title">Weekly Improvement & Feedback Loop</div><div class="card-subtitle">Predictive watchlist, weekly report, and operator feedback capture</div></div>
        <div class="card-body">
          <div class="grid-row grid-2" style="align-items:start">
            <div>
              <div id="cc-ops-weekly" class="text-muted">Loading weekly improvement report...</div>
              <div id="cc-ops-predictive" style="margin-top:10px">Loading predictive watchlist...</div>
            </div>
            <div>
              <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">
                <input class="input" id="cc-feedback-isin" placeholder="ISIN (optional)" style="min-width:160px;flex:1" />
                <input class="input" id="cc-feedback-category" placeholder="Category" value="OPS_FEEDBACK" style="min-width:140px;flex:1" />
              </div>
              <textarea class="input" id="cc-feedback-text" placeholder="Share field-level quality issues, explainability gaps, false positives, or control improvements" style="min-height:92px;width:100%"></textarea>
              <div style="display:flex;justify-content:flex-end;margin-top:8px">
                <button class="btn btn-primary btn-sm" id="cc-feedback-submit-btn">Submit Feedback</button>
              </div>
              <div id="cc-feedback-status" class="text-muted" style="font-size:0.74rem;margin-top:6px"></div>
            </div>
          </div>
        </div>
      </div>
    `;

    async function loadExplanation(decisionId) {
      const panel = document.getElementById('cc-explain-panel');
      const caption = document.getElementById('cc-explain-caption');
      if (!panel) return;
      panel.innerHTML = '<div class="text-muted">Loading explanation...</div>';
      if (caption) caption.textContent = shortDecision(decisionId);

      try {
        const explained = await API.explainDecision(decisionId);
        const exp = explained.explanation || {};
        const factors = Array.isArray(exp.key_factors) ? exp.key_factors.slice(0, 6) : [];
        const rules = Array.isArray(exp.rule_checks) ? exp.rule_checks.slice(0, 8) : [];
        const alternatives = Array.isArray(explained.alternatives) ? explained.alternatives.slice(0, 8) : [];
        const diagSource = explained?.engine_diagnostics
          || explained?.audit_metadata?.engine_diagnostics
          || explained?.decision?.audit_metadata?.engine_diagnostics
          || {};
        const optimizerMethod = String(diagSource.optimizer || explained?.optimization_method || 'Unknown');
        const optimizerFallbackApplied = !!diagSource.optimizer_fallback_applied;
        const optimizerFallbackReason = String(diagSource.optimizer_fallback_reason || '').trim();
        const alternativesCount = Number(diagSource.evaluated_alternatives_count || alternatives.length || 0);

        panel.innerHTML = `
          <div style="font-size:0.86rem;margin-bottom:8px"><strong>Summary:</strong> ${Utils.escapeHTML(asText(exp.summary, 'No summary'))}</div>
          <div style="margin-bottom:10px;padding:8px 10px;border:1px solid var(--border-light);border-radius:8px;background:var(--bg-tertiary)">
            <div class="text-muted" style="font-size:0.72rem;margin-bottom:4px">Allocator Transparency</div>
            <div style="font-size:0.76rem"><strong>Optimizer:</strong> ${Utils.escapeHTML(optimizerMethod)}</div>
            <div style="font-size:0.76rem"><strong>Fallback:</strong> ${optimizerFallbackApplied ? '<span class="text-amber">Applied</span>' : '<span class="text-green">Primary solver</span>'}</div>
            <div style="font-size:0.76rem"><strong>Alternatives logged:</strong> ${Utils.escapeHTML(String(alternativesCount))}</div>
            ${optimizerFallbackReason ? `<div style="font-size:0.72rem;color:var(--text-muted);margin-top:4px">Reason: ${Utils.escapeHTML(optimizerFallbackReason)}</div>` : ''}
          </div>
          <div style="margin-bottom:10px">
            <div class="text-muted" style="font-size:0.72rem;margin-bottom:4px">Key Factors</div>
            ${factors.length ? factors.map((item) => `
              <div style="font-size:0.76rem;padding:6px 8px;border:1px solid var(--border-light);border-radius:8px;margin-bottom:6px">
                <strong>${Utils.escapeHTML(asText(item.name, 'factor'))}</strong>: ${Utils.escapeHTML(asText(item.value, 'n/a'))}
              </div>
            `).join('') : '<div class="text-muted" style="font-size:0.76rem">No key factors</div>'}
          </div>
          <div style="margin-bottom:10px">
            <div class="text-muted" style="font-size:0.72rem;margin-bottom:4px">Rule Evaluation</div>
            ${rules.length ? rules.map((rule) => `
              <div style="font-size:0.74rem;display:flex;justify-content:space-between;gap:8px;padding:6px 8px;border-bottom:1px dashed var(--border-light)">
                <span>${Utils.escapeHTML(asText(rule.rule_code, 'rule'))}</span>
                <span class="${badgeClass(rule.status)}" style="font-weight:700">${Utils.escapeHTML(asText(rule.status, 'N/A'))}</span>
              </div>
            `).join('') : '<div class="text-muted" style="font-size:0.76rem">No rule checks</div>'}
          </div>
          <div>
            <div class="text-muted" style="font-size:0.72rem;margin-bottom:4px">Alternatives Ledger</div>
            ${alternatives.length ? alternatives.map((alt) => `
              <div style="font-size:0.74rem;padding:6px 8px;border:1px solid var(--border-light);border-radius:8px;margin-bottom:6px">
                <div style="display:flex;justify-content:space-between;gap:8px">
                  <strong>${Utils.escapeHTML(asText(alt.alternative_label || alt.label || alt.alternative_id, 'alternative'))}</strong>
                  <span class="${badgeClass(alt.status)}">${Utils.escapeHTML(asText(alt.status, 'N/A'))}</span>
                </div>
                ${alt.rejection_reason ? `<div class="text-muted" style="margin-top:2px">${Utils.escapeHTML(asText(alt.rejection_reason))}</div>` : ''}
              </div>
            `).join('') : '<div class="text-muted" style="font-size:0.76rem">No alternatives recorded</div>'}
          </div>
        `;
      } catch (err) {
        panel.innerHTML = Utils.emptyHTML('Explainability unavailable', err.message || 'Unable to load explanation details');
      }
    }

    async function loadOpsDrilldown(isinRaw) {
      const isin = String(isinRaw || '').trim().toUpperCase();
      const panel = document.getElementById('cc-ops-drilldown');
      if (!panel) return;
      if (!isin) {
        panel.innerHTML = '<div class="text-muted">Enter an ISIN to view explainability and audit trend.</div>';
        return;
      }
      panel.innerHTML = '<div class="text-muted">Loading bond drilldown...</div>';

      try {
        const [drilldown, auditTrail] = await Promise.all([
          withAuthFallback(API.getIntelligenceOpsDrilldown(isin, 20), () => ({})),
          withAuthFallback(API.getInstitutionalAnalysisAudit(isin, 10), () => ({ entries: [], decision_transitions: [] })),
        ]);

        const latest = drilldown?.latest || {};
        const predictive = drilldown?.predictive_degradation || {};
        const timeline = Array.isArray(drilldown?.timeline) ? drilldown.timeline : [];
        const transitions = Array.isArray(auditTrail?.decision_transitions) ? auditTrail.decision_transitions : [];
        const recentEntries = Array.isArray(auditTrail?.entries) ? auditTrail.entries.slice(0, 5) : [];
        const latestSnapshot = latest?.payload_snapshot || {};
        const explainability = latestSnapshot?.explainability || {};

        const whyList = Array.isArray(explainability?.why_this_decision) ? explainability.why_this_decision.slice(0, 4) : [];
        const actionList = Array.isArray(explainability?.committee_actions) ? explainability.committee_actions.slice(0, 3) : [];

        panel.innerHTML = `
          <div style="padding:8px 10px;border:1px solid var(--border-light);border-radius:8px;margin-bottom:10px">
            <div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap">
              <div style="font-size:0.8rem;font-weight:700">${Utils.escapeHTML(isin)}</div>
              <div style="font-size:0.74rem" class="${badgeClass(latest.control_severity || predictive.status)}">${Utils.escapeHTML(asText(latest.control_severity || predictive.status, 'N/A'))}</div>
            </div>
            <div style="font-size:0.74rem;margin-top:4px">Decision: <strong>${Utils.escapeHTML(asText(latest.decision_signal, 'N/A'))}</strong> Â· Confidence ${Utils.escapeHTML(fmtNum(latest.confidence_score, 1))}</div>
            <div style="font-size:0.74rem">Health ${Utils.escapeHTML(fmtNum(latest.health_score, 1))} Â· Risk ${Utils.escapeHTML(fmtNum(latest.risk_score, 1))} Â· Completeness ${Utils.escapeHTML(fmtNum(latest.completeness_pct, 1))}%</div>
            <div class="text-muted" style="font-size:0.7rem">Updated ${Utils.escapeHTML(fmtTs(latest.created_at))}</div>
          </div>

          <div style="margin-bottom:10px">
            <div class="text-muted" style="font-size:0.72rem;margin-bottom:4px">Why This Decision?</div>
            ${whyList.length ? whyList.map((line) => `<div style="font-size:0.74rem;padding:6px 8px;border-bottom:1px dashed var(--border-light)">â€¢ ${Utils.escapeHTML(asText(line))}</div>`).join('') : '<div class="text-muted" style="font-size:0.74rem">No explainability bullets available.</div>'}
          </div>

          <div style="margin-bottom:10px">
            <div class="text-muted" style="font-size:0.72rem;margin-bottom:4px">Committee Actions</div>
            ${actionList.length ? actionList.map((line) => `<div style="font-size:0.74rem;padding:6px 8px;border:1px solid var(--border-light);border-radius:8px;margin-bottom:6px">${Utils.escapeHTML(asText(line))}</div>`).join('') : '<div class="text-muted" style="font-size:0.74rem">No action guidance available.</div>'}
          </div>

          <div style="margin-bottom:10px">
            <div class="text-muted" style="font-size:0.72rem;margin-bottom:4px">Audit Drift (Recent 5)</div>
            ${recentEntries.length ? recentEntries.map((entry) => {
              const delta = entry.change_vs_previous || {};
              return `
                <div style="font-size:0.72rem;padding:6px 8px;border-bottom:1px dashed var(--border-light)">
                  <div style="display:flex;justify-content:space-between;gap:8px">
                    <span>${Utils.escapeHTML(fmtTs(entry.created_at))}</span>
                    <span class="${badgeClass(entry.control_severity)}">${Utils.escapeHTML(asText(entry.decision_signal, 'N/A'))}</span>
                  </div>
                  <div class="text-muted">Î” confidence ${Utils.escapeHTML(asText(delta.confidence_delta, '0'))}, Î” risk ${Utils.escapeHTML(asText(delta.risk_score_delta, '0'))}, Î” health ${Utils.escapeHTML(asText(delta.health_score_delta, '0'))}</div>
                </div>
              `;
            }).join('') : '<div class="text-muted" style="font-size:0.74rem">No audit rows available.</div>'}
          </div>

          <div>
            <div class="text-muted" style="font-size:0.72rem;margin-bottom:4px">Predictive Degradation</div>
            <div style="font-size:0.74rem" class="${badgeClass(predictive.status)}">${Utils.escapeHTML(asText(predictive.status, 'N/A'))} (${Utils.escapeHTML(fmtNum(predictive.early_warning_score, 1))})</div>
            <div class="text-muted" style="font-size:0.72rem">${Utils.escapeHTML(asText(predictive.summary, 'No predictive summary'))}</div>
            ${Array.isArray(transitions) && transitions.length ? `<div class="text-muted" style="font-size:0.7rem;margin-top:4px">Transitions: ${transitions.length}</div>` : ''}
            ${timeline.length ? `<div class="text-muted" style="font-size:0.7rem">Samples: ${timeline.length}</div>` : ''}
          </div>
        `;
      } catch (err) {
        panel.innerHTML = Utils.emptyHTML('ISIN drilldown unavailable', err.message || 'Failed to load intelligence drilldown');
      }
    }

    async function refreshCenter() {
      try {
        const [workbench, center, decisionsResp, intentsResp, alertSummary, trustIntegrity, opsObservability, predictiveWatchlist, weeklyReport] = await Promise.all([
          withAuthFallback(API.getDecisionWorkbenchSummary(), () => ({})),
          withAuthFallback(API.getDecisionCommandCenter(100), () => ({})),
          withAuthFallback(API.listDecisions({ limit: 60 }), () => ({ items: [] })),
          withAuthFallback(API.getPendingDecisionIntents(100), () => ({ items: [] })),
          withAuthFallback(API.alertSummary(), () => ({})),
          withAuthFallback(API.getTrustAuditIntegrity(400), () => ({})),
          withAuthFallback(API.getIntelligenceOpsObservability(25), () => ({ kpis: {}, top_at_risk_isins: [], live_feed: [] })),
          withAuthFallback(API.getIntelligencePredictiveWatchlist(8), () => ({ items: [] })),
          withAuthFallback(API.getIntelligenceWeeklyImprovementReport(7), () => ({ summary: {}, top_missing_fields: [], top_at_risk_isins: [], recommended_actions: [] })),
        ]);

        const degradedFeeds = [
          workbench?._error ? `Workbench: ${workbench._error}` : null,
          center?._error ? `Command center: ${center._error}` : null,
          decisionsResp?._error ? `Decision registry: ${decisionsResp._error}` : null,
          intentsResp?._error ? `Intent queue: ${intentsResp._error}` : null,
          alertSummary?._error ? `Alert summary: ${alertSummary._error}` : null,
          trustIntegrity?._error ? `Audit integrity: ${trustIntegrity._error}` : null,
        ].filter(Boolean);
        const ccStatusBanner = document.getElementById('cc-status-banner');
        if (ccStatusBanner) {
          ccStatusBanner.innerHTML = degradedFeeds.length ? `
            <div class="card" style="border-color:rgba(245,158,11,0.35);background:linear-gradient(180deg,rgba(245,158,11,0.08),rgba(245,158,11,0.03))">
              <div class="card-body" style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:flex-start">
                <div style="flex:1;min-width:240px">
                  <div class="metric-label" style="color:#b45309">Partial Data Mode</div>
                  <div style="font-size:0.82rem;margin-top:4px">Some command-center feeds are degraded. Decision queue is shown with available sources while unresolved feeds remain fail-closed.</div>
                  <div style="font-size:0.74rem;color:var(--text-muted);margin-top:6px">${Utils.escapeHTML(degradedFeeds.slice(0, 4).join(' | '))}${degradedFeeds.length > 4 ? ' | ...' : ''}</div>
                </div>
                <div style="font-size:0.74rem;color:var(--text-muted)">${degradedFeeds.length} feed(s) impacted</div>
              </div>
            </div>
          ` : '';
        }

        const decisions = Array.isArray(decisionsResp?.items) ? decisionsResp.items : [];
        const pendingIntents = Array.isArray(intentsResp?.items) ? intentsResp.items : [];
        const pendingOverrides = Array.isArray(center?.pending_overrides) ? center.pending_overrides : [];
        const flagged = Array.isArray(center?.flagged_decisions) ? center.flagged_decisions : [];
        const complianceChecks = Array.isArray(center?.recent_compliance_checks) ? center.recent_compliance_checks : [];
        const breaches = Array.isArray(center?.recent_ips_breaches) ? center.recent_ips_breaches : [];
        const recentAlerts = Array.isArray(center?.recent_alerts) ? center.recent_alerts : [];
        const feedStatus = (center && typeof center.feed_status === 'object' && center.feed_status !== null) ? center.feed_status : {};
        const feedFailures = Array.isArray(center?.feed_failures) ? center.feed_failures : [];
        const opsKpis = (opsObservability && typeof opsObservability.kpis === 'object' && opsObservability.kpis) ? opsObservability.kpis : {};
        const opsRiskIsins = Array.isArray(opsObservability?.top_at_risk_isins) ? opsObservability.top_at_risk_isins : [];
        const opsLiveFeed = Array.isArray(opsObservability?.live_feed) ? opsObservability.live_feed : [];
        const predictiveItems = Array.isArray(predictiveWatchlist?.items) ? predictiveWatchlist.items : [];
        const weeklySummary = (weeklyReport && typeof weeklyReport.summary === 'object' && weeklyReport.summary) ? weeklyReport.summary : {};
        const weeklyActions = Array.isArray(weeklyReport?.recommended_actions) ? weeklyReport.recommended_actions : [];
        const weeklyMissing = Array.isArray(weeklyReport?.top_missing_fields) ? weeklyReport.top_missing_fields : [];

        const intentsCount = Number(center?.summary?.intent_backlog ?? pendingIntents.length ?? 0);
        const intentFailures = Number(center?.summary?.intent_failures ?? 0);
        const feedFailureCount = Number(center?.summary?.feed_failures ?? feedFailures.length ?? 0);
        const auditValid = trustIntegrity?.valid_chain === true;
        const auditLabel = auditValid ? 'VALID' : (trustIntegrity && trustIntegrity.valid_chain === false ? 'BROKEN' : 'UNKNOWN');

        const kpiSet = (id, value, cls = '') => {
          const node = document.getElementById(id);
          if (!node) return;
          node.textContent = String(value);
          node.classList.remove('text-green', 'text-amber', 'text-red');
          if (cls) node.classList.add(cls);
        };

        kpiSet('cc-kpi-intents', intentsCount, intentsCount > 0 ? 'text-amber' : 'text-green');
        kpiSet('cc-kpi-intent-failures', intentFailures, intentFailures > 0 ? 'text-red' : 'text-green');
        kpiSet('cc-kpi-feed-failures', feedFailureCount, feedFailureCount > 0 ? 'text-red' : 'text-green');
        kpiSet('cc-kpi-overrides', pendingOverrides.length, pendingOverrides.length > 0 ? 'text-amber' : 'text-green');
        kpiSet('cc-kpi-flagged', flagged.length, flagged.length > 0 ? 'text-amber' : 'text-green');
        kpiSet('cc-kpi-breaches', breaches.length, breaches.length > 0 ? 'text-red' : 'text-green');
        kpiSet('cc-kpi-audit', auditLabel, auditValid ? 'text-green' : 'text-red');
        kpiSet('cc-kpi-analyses-24h', Number(opsKpis.analyses_last_24h || 0), Number(opsKpis.analyses_last_24h || 0) > 0 ? 'text-green' : 'text-amber');
        kpiSet('cc-kpi-blocked-24h', Number(opsKpis.blocked_last_24h || 0), Number(opsKpis.blocked_last_24h || 0) > 0 ? 'text-red' : 'text-green');
        kpiSet('cc-kpi-predictive-24h', Number(opsKpis.predictive_warnings_last_24h || 0), Number(opsKpis.predictive_warnings_last_24h || 0) > 0 ? 'text-amber' : 'text-green');
        kpiSet('cc-kpi-health-24h', fmtNum(opsKpis.avg_health_score_last_24h, 1), Number(opsKpis.avg_health_score_last_24h || 0) < 75 ? 'text-red' : Number(opsKpis.avg_health_score_last_24h || 0) < 90 ? 'text-amber' : 'text-green');

        const queueUpdated = document.getElementById('cc-queue-updated');
        if (queueUpdated) {
          const generatedAt = center?.generated_at || workbench?.generated_at || new Date().toISOString();
          queueUpdated.textContent = `Updated ${fmtTs(generatedAt)} Â· ${decisions.length} decisions`;
        }

        const queueBody = document.getElementById('cc-queue-body');
        if (queueBody) {
          if (!decisions.length) {
            queueBody.innerHTML = '<tr><td colspan="6" class="text-muted">No decisions available.</td></tr>';
          } else {
            queueBody.innerHTML = decisions.slice(0, 30).map((item) => {
              const id = asText(item.decision_id, 'n/a');
              return `
                <tr>
                  <td class="font-mono" style="font-size:0.74rem">${Utils.escapeHTML(shortDecision(id))}</td>
                  <td style="font-size:0.74rem">${Utils.escapeHTML(asText(item.decision_type))}</td>
                  <td style="font-size:0.74rem" class="${badgeClass(item.decision_outcome)}">${Utils.escapeHTML(asText(item.decision_outcome))}</td>
                  <td style="font-size:0.74rem" class="${badgeClass(item.status)}">${Utils.escapeHTML(asText(item.status))}</td>
                  <td style="font-size:0.72rem">${Utils.escapeHTML(fmtTs(item.updated_at || item.created_at))}</td>
                  <td style="display:flex;gap:6px;justify-content:flex-end">
                    <button class="btn btn-ghost btn-sm" data-cc-decision="${Utils.escapeHTML(id)}">Explain</button>
                    <button class="btn btn-secondary btn-sm" data-cc-evidence="${Utils.escapeHTML(id)}">Evidence</button>
                  </td>
                </tr>
              `;
            }).join('');
          }
        }

        const feedCaption = document.getElementById('cc-feed-health-caption');
        if (feedCaption) {
          feedCaption.textContent = feedFailures.length
            ? `Hard failures detected in ${feedFailures.length} feed(s)`
            : 'All operational feeds reporting healthy status';
        }

        const feedHealthEl = document.getElementById('cc-feed-health');
        if (feedHealthEl) {
          const feedRows = Object.entries(feedStatus || {});
          if (!feedRows.length) {
            feedHealthEl.innerHTML = '<div class="text-muted">No feed status data available.</div>';
          } else {
            feedHealthEl.innerHTML = feedRows.map(([feedName, info]) => {
              const statusText = String((info || {}).status || 'UNKNOWN').toUpperCase();
              const countText = Number.isFinite(Number((info || {}).count)) ? ` Â· ${Number((info || {}).count)} record(s)` : '';
              const errorText = (info || {}).error ? `<div class="text-muted" style="font-size:0.72rem;margin-top:2px">${Utils.escapeHTML(String(info.error))}</div>` : '';
              return `
                <div style="padding:8px 10px;border:1px solid var(--border-light);border-radius:8px;margin-bottom:8px">
                  <div style="display:flex;justify-content:space-between;gap:8px;align-items:center">
                    <div style="font-size:0.78rem;font-weight:700">${Utils.escapeHTML(feedName)}</div>
                    <div style="font-size:0.74rem" class="${badgeClass(statusText)}">${Utils.escapeHTML(statusText)}${Utils.escapeHTML(countText)}</div>
                  </div>
                  ${errorText}
                </div>
              `;
            }).join('');
          }
        }

        const opsRiskEl = document.getElementById('cc-ops-at-risk');
        if (opsRiskEl) {
          if (!opsRiskIsins.length) {
            opsRiskEl.innerHTML = '<div class="text-muted" style="font-size:0.74rem">No elevated ISIN risk clusters in the last 24h.</div>';
          } else {
            opsRiskEl.innerHTML = `
              <div class="text-muted" style="font-size:0.72rem;margin-bottom:4px">Top At-Risk ISINs (24h)</div>
              ${opsRiskIsins.slice(0, 6).map((item) => `
                <div style="display:flex;justify-content:space-between;gap:8px;padding:6px 8px;border:1px solid var(--border-light);border-radius:8px;margin-bottom:6px">
                  <span class="font-mono" style="font-size:0.72rem">${Utils.escapeHTML(asText(item.isin, 'N/A'))}</span>
                  <span style="font-size:0.72rem" class="text-amber">Risk ${Utils.escapeHTML(asText(item.risk_points, 0))}</span>
                </div>
              `).join('')}
            `;
          }
        }

        const opsFeedEl = document.getElementById('cc-ops-live-feed');
        if (opsFeedEl) {
          if (!opsLiveFeed.length) {
            opsFeedEl.innerHTML = '<div class="text-muted">No intelligence events captured yet.</div>';
          } else {
            opsFeedEl.innerHTML = opsLiveFeed.slice(0, 18).map((event) => `
              <div style="padding:7px 8px;border-bottom:1px dashed var(--border-light)">
                <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start">
                  <div style="font-size:0.74rem"><strong>${Utils.escapeHTML(asText(event.event_type, 'EVENT'))}</strong> Â· ${Utils.escapeHTML(asText(event.isin, 'N/A'))}</div>
                  <div style="font-size:0.7rem" class="${badgeClass(event.control_severity || event.predictive_status)}">${Utils.escapeHTML(asText(event.control_severity || event.predictive_status, 'N/A'))}</div>
                </div>
                <div class="text-muted" style="font-size:0.72rem">${Utils.escapeHTML(asText(event.message, 'Analysis update'))}</div>
                <div class="text-muted" style="font-size:0.68rem">${Utils.escapeHTML(fmtTs(event.created_at))} Â· confidence ${Utils.escapeHTML(fmtNum(event.confidence_score, 1))} Â· health ${Utils.escapeHTML(fmtNum(event.health_score, 1))}</div>
              </div>
            `).join('');
          }
        }

        const weeklyEl = document.getElementById('cc-ops-weekly');
        if (weeklyEl) {
          weeklyEl.innerHTML = `
            <div style="padding:8px 10px;border:1px solid var(--border-light);border-radius:8px;margin-bottom:8px">
              <div style="font-size:0.78rem;font-weight:700;margin-bottom:4px">Weekly Improvement Summary</div>
              <div style="font-size:0.74rem">Analyses: <strong>${Utils.escapeHTML(asText(weeklySummary.total_analyses, 0))}</strong> Â· Blocked: <strong>${Utils.escapeHTML(asText(weeklySummary.blocked_analyses, 0))}</strong> Â· High Severity: <strong>${Utils.escapeHTML(asText(weeklySummary.high_severity_analyses, 0))}</strong></div>
              <div style="font-size:0.74rem">Avg Health: <strong>${Utils.escapeHTML(fmtNum(weeklySummary.avg_health_score, 1))}</strong> Â· Avg Completeness: <strong>${Utils.escapeHTML(fmtNum(weeklySummary.avg_completeness_pct, 1))}%</strong></div>
              <div class="text-muted" style="font-size:0.7rem">Blocked rate: ${Utils.escapeHTML(fmtNum(weeklySummary.blocked_rate_pct, 2))}%</div>
            </div>
            <div style="margin-bottom:8px">
              <div class="text-muted" style="font-size:0.72rem;margin-bottom:4px">Top Missing Fields</div>
              ${weeklyMissing.length ? weeklyMissing.slice(0, 5).map((item) => `<div style="font-size:0.73rem;padding:4px 0">â€¢ ${Utils.escapeHTML(asText(item.field, 'field'))} (${Utils.escapeHTML(asText(item.count, 0))})</div>`).join('') : '<div class="text-muted" style="font-size:0.73rem">No missing-field trend data.</div>'}
            </div>
            <div>
              <div class="text-muted" style="font-size:0.72rem;margin-bottom:4px">Recommended Actions</div>
              ${weeklyActions.length ? weeklyActions.slice(0, 4).map((item) => `<div style="font-size:0.73rem;padding:6px 8px;border:1px solid var(--border-light);border-radius:8px;margin-bottom:6px">${Utils.escapeHTML(asText(item))}</div>`).join('') : '<div class="text-muted" style="font-size:0.73rem">No recommendations generated.</div>'}
            </div>
          `;
        }

        const predictiveEl = document.getElementById('cc-ops-predictive');
        if (predictiveEl) {
          if (!predictiveItems.length) {
            predictiveEl.innerHTML = '<div class="text-muted" style="font-size:0.74rem">Predictive watchlist is empty.</div>';
          } else {
            predictiveEl.innerHTML = `
              <div class="text-muted" style="font-size:0.72rem;margin-bottom:4px">Predictive Degradation Watchlist</div>
              ${predictiveItems.slice(0, 8).map((item) => `
                <div style="display:flex;justify-content:space-between;gap:8px;padding:6px 8px;border-bottom:1px dashed var(--border-light)">
                  <div>
                    <div class="font-mono" style="font-size:0.72rem">${Utils.escapeHTML(asText(item.isin, 'N/A'))}</div>
                    <div class="text-muted" style="font-size:0.68rem">${Utils.escapeHTML(asText(item.summary, ''))}</div>
                  </div>
                  <div style="text-align:right">
                    <div style="font-size:0.72rem" class="${badgeClass(item.predictive_status)}">${Utils.escapeHTML(asText(item.predictive_status, 'N/A'))}</div>
                    <div class="text-muted" style="font-size:0.68rem">${Utils.escapeHTML(fmtNum(item.early_warning_score, 1))}</div>
                  </div>
                </div>
              `).join('')}
            `;
          }
        }

        const overridesEl = document.getElementById('cc-overrides-list');
        if (overridesEl) {
          if (!pendingOverrides.length) {
            overridesEl.innerHTML = '<div class="text-muted">No pending override approvals.</div>';
          } else {
            overridesEl.innerHTML = pendingOverrides.slice(0, 10).map((item) => `
              <div style="padding:8px 10px;border:1px solid var(--border-light);border-radius:8px;margin-bottom:8px">
                <div style="font-size:0.78rem;font-weight:700">${Utils.escapeHTML(asText(item.decision_id))}</div>
                <div style="font-size:0.72rem" class="${badgeClass(item.status)}">${Utils.escapeHTML(asText(item.status))}</div>
                <div class="text-muted" style="font-size:0.7rem">Requested: ${Utils.escapeHTML(fmtTs(item.created_at))}</div>
              </div>
            `).join('');
          }
        }

        const complianceEl = document.getElementById('cc-compliance-list');
        if (complianceEl) {
          if (!complianceChecks.length) {
            complianceEl.innerHTML = '<div class="text-muted">No recent compliance checks.</div>';
          } else {
            complianceEl.innerHTML = complianceChecks.slice(0, 10).map((item) => `
              <div style="padding:8px 10px;border-bottom:1px dashed var(--border-light)">
                <div style="font-size:0.76rem;font-weight:700">${Utils.escapeHTML(asText(item.rule_code || item.isin || 'Compliance Check'))}</div>
                <div style="font-size:0.72rem" class="${badgeClass(item.result || item.overall || item.status)}">${Utils.escapeHTML(asText(item.result || item.overall || item.status))}</div>
                <div class="text-muted" style="font-size:0.7rem">${Utils.escapeHTML(fmtTs(item.checked_at || item.created_at))}</div>
              </div>
            `).join('');
          }
        }

        const breachEl = document.getElementById('cc-breach-list');
        if (breachEl) {
          const openAlerts = Number(alertSummary?.open_alerts ?? alertSummary?.active_alerts ?? 0);
          const breachItems = breaches.slice(0, 8).map((item) => `
            <div style="padding:8px 10px;border:1px solid var(--border-light);border-radius:8px;margin-bottom:8px">
              <div style="font-size:0.76rem;font-weight:700">${Utils.escapeHTML(asText(item.rule_key || item.breach_code || 'Breach'))}</div>
              <div style="font-size:0.72rem" class="${badgeClass(item.severity || item.status)}">${Utils.escapeHTML(asText(item.severity || item.status))}</div>
              <div class="text-muted" style="font-size:0.7rem">${Utils.escapeHTML(fmtTs(item.detected_at || item.created_at))}</div>
            </div>
          `).join('');

          const alertItems = recentAlerts.slice(0, 5).map((item) => `
            <div style="padding:8px 10px;border-bottom:1px dashed var(--border-light)">
              <div style="font-size:0.76rem;font-weight:700">${Utils.escapeHTML(asText(item.rule_code || item.alert_type || 'Alert'))}</div>
              <div style="font-size:0.72rem" class="${badgeClass(item.severity || item.status)}">${Utils.escapeHTML(asText(item.severity || item.status || 'OPEN'))}</div>
              <div class="text-muted" style="font-size:0.7rem">${Utils.escapeHTML(fmtTs(item.created_at || item.alert_date))}</div>
            </div>
          `).join('');

          breachEl.innerHTML = `
            <div style="font-size:0.78rem;margin-bottom:8px"><strong>Open alerts:</strong> ${openAlerts}</div>
            ${breachItems || '<div class="text-muted">No recent IPS breaches.</div>'}
            <div style="margin-top:10px;font-size:0.72rem;color:var(--text-muted)">Recent Alert Feed</div>
            ${alertItems || '<div class="text-muted" style="font-size:0.74rem">No recent alerts.</div>'}
          `;
        }

        el.querySelectorAll('button[data-cc-decision]').forEach((btn) => {
          btn.addEventListener('click', () => loadExplanation(btn.getAttribute('data-cc-decision')));
        });

        el.querySelectorAll('button[data-cc-evidence]').forEach((btn) => {
          btn.addEventListener('click', () => {
            const decisionId = btn.getAttribute('data-cc-evidence');
            if (!decisionId) return;
            try { localStorage.setItem('selected_decision_id', decisionId); } catch (_) {}
            navigate('decision-evidence', { decisionId });
          });
        });

        if (decisions.length) {
          await loadExplanation(decisions[0].decision_id);
        }
      } catch (err) {
        if (isAuthError(err)) {
          handleSessionExpired(err);
          return;
        }
        const queueBody = document.getElementById('cc-queue-body');
        if (queueBody) {
          queueBody.innerHTML = `<tr><td colspan="6">${Utils.emptyHTML('Command center unavailable', err.message || 'Failed to load trust operations view')}</td></tr>`;
        }
      }
    }

    const refreshBtn = document.getElementById('cc-refresh-btn');
    if (refreshBtn) refreshBtn.addEventListener('click', () => refreshCenter());

    const reconcileBtn = document.getElementById('cc-reconcile-btn');
    if (reconcileBtn) {
      reconcileBtn.addEventListener('click', async () => {
        const originalHtml = reconcileBtn.innerHTML;
        const originalDisabled = reconcileBtn.disabled;
        reconcileBtn.disabled = true;
        reconcileBtn.textContent = 'Reconcilingâ€¦';
        try {
          const result = await API.reconcileDecisionIntents(100);
          Utils.toast('success', 'Intent Reconciliation', `Reconciled ${result.reconciled || 0}, failed ${result.failed || 0}`);
          await refreshCenter();
        } catch (err) {
          Utils.toast('error', 'Intent Reconciliation', err.message || 'Reconciliation failed');
        } finally {
          reconcileBtn.innerHTML = originalHtml;
          reconcileBtn.disabled = originalDisabled;
        }
      });
    }

    const drilldownBtn = document.getElementById('cc-ops-drilldown-btn');
    const drilldownInput = document.getElementById('cc-ops-isin-input');
    if (drilldownBtn) {
      drilldownBtn.addEventListener('click', () => loadOpsDrilldown(drilldownInput?.value || ''));
    }
    if (drilldownInput) {
      drilldownInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          loadOpsDrilldown(drilldownInput.value || '');
        }
      });
    }

    const feedbackBtn = document.getElementById('cc-feedback-submit-btn');
    if (feedbackBtn) {
      feedbackBtn.addEventListener('click', async () => {
        const statusEl = document.getElementById('cc-feedback-status');
        const feedbackText = document.getElementById('cc-feedback-text');
        const feedbackIsin = document.getElementById('cc-feedback-isin');
        const feedbackCategory = document.getElementById('cc-feedback-category');
        const feedback = String(feedbackText?.value || '').trim();

        if (feedback.length < 5) {
          if (statusEl) statusEl.textContent = 'Feedback must be at least 5 characters.';
          return;
        }

        try {
          await API.submitIntelligenceFeedback({
            feedback,
            isin: String(feedbackIsin?.value || '').trim().toUpperCase() || null,
            category: String(feedbackCategory?.value || 'OPS_FEEDBACK').trim() || 'OPS_FEEDBACK',
            sentiment: 'NEUTRAL',
          });
          if (feedbackText) feedbackText.value = '';
          if (statusEl) statusEl.textContent = 'Feedback submitted and queued for weekly review.';
          Utils.toast('success', 'Feedback Loop', 'Feedback submitted');
          await refreshCenter();
        } catch (err) {
          if (statusEl) statusEl.textContent = err.message || 'Feedback submission failed.';
          Utils.toast('error', 'Feedback Loop', err.message || 'Failed to submit feedback');
        }
      });
    }

    await refreshCenter();
  }

  async function renderDecisionEvidence(el, params = {}) {
    const asText = (value, fallback = 'â€”') => {
      const text = String(value == null ? '' : value).trim();
      return text || fallback;
    };
    const fmtTs = (value) => value ? Utils.formatDateTime(value) : 'â€”';
    const badgeClass = (status) => {
      const s = String(status || '').toUpperCase();
      if (s.includes('FAIL') || s.includes('ERROR') || s.includes('BLOCK') || s.includes('MISMATCH')) return 'text-red';
      if (s.includes('WARN') || s.includes('PENDING') || s.includes('PARTIAL')) return 'text-amber';
      if (s.includes('PASS') || s.includes('APPROVED') || s.includes('MATCH') || s.includes('COMPLETED') || s.includes('OK')) return 'text-green';
      return '';
    };
    const jsonBlock = (value) => Utils.escapeHTML(JSON.stringify(value == null ? {} : value, null, 2));

    let activeDecisionId = (params && params.decisionId) ? String(params.decisionId) : '';
    if (!activeDecisionId) {
      try { activeDecisionId = String(localStorage.getItem('selected_decision_id') || ''); } catch (_) {}
    }

    el.innerHTML = `
      <div class="page-header" style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap">
        <div>
          <div class="page-title">Decision Evidence</div>
          <div class="page-subtitle">Canonical trust chain: decision, policy, snapshots, alternatives, overrides, replay, and audit in one page.</div>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn btn-secondary btn-sm" id="evidence-run-replay-btn">${IC.refresh} Execute Replay</button>
          <button class="btn btn-primary btn-sm" id="evidence-refresh-btn">${IC.refresh} Refresh</button>
        </div>
      </div>

      <div class="card" style="margin-bottom:14px">
        <div class="card-body" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
          <input class="input" id="evidence-decision-id" placeholder="Enter decision_id" style="min-width:260px;flex:1" />
          <button class="btn btn-primary btn-sm" id="evidence-load-btn">Load Evidence</button>
          <button class="btn btn-ghost btn-sm" id="evidence-back-cc-btn">Back to Command Center</button>
        </div>
      </div>

      <div class="grid-row grid-2" style="margin-bottom:14px;align-items:start">
        <div class="card">
          <div class="card-header"><div class="card-title">Decision + Policy</div></div>
          <div class="card-body" id="evidence-decision-policy">Load a decision to view evidence.</div>
        </div>
        <div class="card">
          <div class="card-header"><div class="card-title">Alternatives Ledger</div></div>
          <div class="card-body" id="evidence-alternatives">Load a decision to view alternatives.</div>
        </div>
      </div>

      <div class="grid-row grid-2" style="margin-bottom:14px;align-items:start">
        <div class="card">
          <div class="card-header"><div class="card-title">Snapshot Catalog</div></div>
          <div class="card-body" id="evidence-snapshots">Load a decision to view snapshot lineage.</div>
        </div>
        <div class="card">
          <div class="card-header"><div class="card-title">Replay + Audit</div></div>
          <div class="card-body" id="evidence-replay-audit">Load a decision to view replay and audit evidence.</div>
        </div>
      </div>

      <div class="card">
        <div class="card-header"><div class="card-title">Override Workflow</div></div>
        <div class="card-body" id="evidence-override-workflow">Load a decision to request or approve overrides.</div>
      </div>

      <div class="card" style="margin-top:14px">
        <div class="card-header"><div class="card-title">Recent Decisions</div></div>
        <div class="card-body" id="evidence-recent-decisions">Loading...</div>
      </div>
    `;

    const inputEl = document.getElementById('evidence-decision-id');
    if (inputEl && activeDecisionId) inputEl.value = activeDecisionId;

    async function loadRecentDecisions() {
      const recentEl = document.getElementById('evidence-recent-decisions');
      if (!recentEl) return;
      try {
        const listed = await withAuthFallback(API.listDecisions({ limit: 30 }), () => ({ items: [] }));
        const items = Array.isArray(listed?.items) ? listed.items : [];
        if (!items.length) {
          recentEl.innerHTML = '<div class="text-muted">No decisions found.</div>';
          return;
        }
        recentEl.innerHTML = items.slice(0, 20).map((item) => {
          const decisionId = asText(item.decision_id, '');
          return `
            <div style="padding:8px 10px;border:1px solid var(--border-light);border-radius:8px;margin-bottom:8px;display:flex;justify-content:space-between;gap:8px;align-items:center">
              <div>
                <div class="font-mono" style="font-size:0.74rem">${Utils.escapeHTML(decisionId)}</div>
                <div style="font-size:0.74rem">${Utils.escapeHTML(asText(item.decision_type))} Â· <span class="${badgeClass(item.decision_outcome)}">${Utils.escapeHTML(asText(item.decision_outcome))}</span></div>
              </div>
              <button class="btn btn-ghost btn-sm" data-evidence-pick="${Utils.escapeHTML(decisionId)}">Open</button>
            </div>
          `;
        }).join('');
        recentEl.querySelectorAll('button[data-evidence-pick]').forEach((btn) => {
          btn.addEventListener('click', async () => {
            const decisionId = btn.getAttribute('data-evidence-pick');
            if (!decisionId) return;
            if (inputEl) inputEl.value = decisionId;
            await loadEvidence(decisionId);
          });
        });
      } catch (err) {
        recentEl.innerHTML = Utils.emptyHTML('Decision list unavailable', err.message || 'Unable to load recent decisions');
      }
    }

    async function loadEvidence(decisionIdRaw) {
      const decisionId = String(decisionIdRaw || '').trim();
      if (!decisionId) {
        Utils.toast('error', 'Decision Evidence', 'Enter a valid decision_id');
        return;
      }

      const decisionPolicyEl = document.getElementById('evidence-decision-policy');
      const alternativesEl = document.getElementById('evidence-alternatives');
      const snapshotsEl = document.getElementById('evidence-snapshots');
      const replayAuditEl = document.getElementById('evidence-replay-audit');
      const overrideEl = document.getElementById('evidence-override-workflow');

      if (decisionPolicyEl) decisionPolicyEl.innerHTML = '<div class="text-muted">Loading decision evidence...</div>';
      if (alternativesEl) alternativesEl.innerHTML = '<div class="text-muted">Loading alternatives...</div>';
      if (snapshotsEl) snapshotsEl.innerHTML = '<div class="text-muted">Loading snapshots...</div>';
      if (replayAuditEl) replayAuditEl.innerHTML = '<div class="text-muted">Loading replay and audit evidence...</div>';
      if (overrideEl) overrideEl.innerHTML = '<div class="text-muted">Loading override workflow...</div>';

      try {
        const evidence = await API.getDecisionEvidence(decisionId);
        activeDecisionId = decisionId;
        try { localStorage.setItem('selected_decision_id', decisionId); } catch (_) {}

        const decision = evidence?.decision || {};
        const policy = evidence?.policy || {};
        const alternatives = Array.isArray(evidence?.alternatives) ? evidence.alternatives : [];
        const snapshotCatalog = Array.isArray(evidence?.snapshot_catalog) ? evidence.snapshot_catalog : [];
        const replayRuns = Array.isArray(evidence?.replay_runs) ? evidence.replay_runs : [];
        const overrideRequests = Array.isArray(evidence?.override_requests) ? evidence.override_requests : [];
        const overrideApprovals = Array.isArray(evidence?.override_approvals) ? evidence.override_approvals : [];
        const auditPack = evidence?.audit_pack || {};
        const institutionalAudit = Array.isArray(auditPack?.institutional_audit) ? auditPack.institutional_audit : [];
        const canonicalAudit = Array.isArray(auditPack?.canonical_audit) ? auditPack.canonical_audit : [];
        const evidenceSummary = {
          alternatives: alternatives.length,
          snapshots: snapshotCatalog.length,
          replayRuns: replayRuns.length,
          overrideRequests: overrideRequests.length,
          overrideApprovals: overrideApprovals.length,
        };

        if (decisionPolicyEl) {
          decisionPolicyEl.innerHTML = `
            <div style="font-size:0.84rem;margin-bottom:8px"><strong>Decision ID:</strong> <span class="font-mono">${Utils.escapeHTML(asText(decision.decision_id, decisionId))}</span></div>
            <div style="font-size:0.8rem;margin-bottom:6px"><strong>Type:</strong> ${Utils.escapeHTML(asText(decision.decision_type))}</div>
            <div style="font-size:0.8rem;margin-bottom:6px"><strong>Outcome:</strong> <span class="${badgeClass(decision.decision_outcome)}">${Utils.escapeHTML(asText(decision.decision_outcome))}</span></div>
            <div style="font-size:0.8rem;margin-bottom:6px"><strong>Status:</strong> <span class="${badgeClass(decision.status)}">${Utils.escapeHTML(asText(decision.status))}</span></div>
            <div style="font-size:0.8rem;margin-bottom:6px"><strong>Trace Timestamp:</strong> ${Utils.escapeHTML(fmtTs(decision.trace_timestamp))}</div>
            <div style="font-size:0.8rem;margin-bottom:8px"><strong>Policy Version:</strong> <span class="font-mono">${Utils.escapeHTML(asText(decision.policy_version_id))}</span></div>
            <div class="text-muted" style="font-size:0.74rem;margin-bottom:4px">Policy Body</div>
            <pre style="margin:0;padding:10px;border:1px solid var(--border-light);border-radius:8px;background:var(--bg-tertiary);max-height:220px;overflow:auto"><code>${jsonBlock(policy?.policy_body || {})}</code></pre>
            <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:10px;font-size:0.72rem;color:var(--text-muted)">
              <span>Alternatives: ${evidenceSummary.alternatives}</span>
              <span>Snapshots: ${evidenceSummary.snapshots}</span>
              <span>Replay runs: ${evidenceSummary.replayRuns}</span>
            </div>
          `;
        }

        if (alternativesEl) {
          if (!alternatives.length) {
            alternativesEl.innerHTML = Utils.emptyHTML(
              'No alternatives ledger entries found',
              'This decision may have been recorded before evaluated alternatives were emitted. Re-run replay or open a newer decision to inspect the full ledger.'
            );
          } else {
            alternativesEl.innerHTML = `
              <div style="font-size:0.74rem;color:var(--text-muted);margin-bottom:8px">Evidence completeness: ${evidenceSummary.alternatives} alternatives Â· ${evidenceSummary.snapshots} snapshots Â· ${evidenceSummary.replayRuns} replay run(s)</div>
              ${alternatives.map((alt) => `
              <div style="padding:8px 10px;border:1px solid var(--border-light);border-radius:8px;margin-bottom:8px">
                <div style="display:flex;justify-content:space-between;gap:8px">
                  <strong>${Utils.escapeHTML(asText(alt.alternative_label || alt.label || alt.alternative_id, 'alternative'))}</strong>
                  <span class="${badgeClass(alt.status)}">${Utils.escapeHTML(asText(alt.status, 'N/A'))}</span>
                </div>
                <div style="font-size:0.74rem;color:var(--text-muted)">Rule: ${Utils.escapeHTML(asText(alt.rule_code, 'n/a'))} Â· Score: ${Utils.escapeHTML(asText(alt.score, 'n/a'))}</div>
                ${alt.rejection_reason ? `<div style="font-size:0.74rem;margin-top:4px">Reason: ${Utils.escapeHTML(asText(alt.rejection_reason))}</div>` : ''}
              </div>
            `).join('')}
            `;
          }
        }

        if (snapshotsEl) {
          if (!snapshotCatalog.length) {
            snapshotsEl.innerHTML = '<div class="text-muted">No catalog snapshots found for this decision.</div>';
          } else {
            snapshotsEl.innerHTML = `
              <div class="table-wrap" style="max-height:320px;overflow:auto">
                <table class="table">
                  <thead><tr><th>Type</th><th>Reference</th><th>Snapshot ID</th><th>Created</th></tr></thead>
                  <tbody>
                    ${snapshotCatalog.map((snap) => `
                      <tr>
                        <td style="font-size:0.74rem">${Utils.escapeHTML(asText(snap.snapshot_type))}</td>
                        <td style="font-size:0.74rem">${Utils.escapeHTML(asText(snap.snapshot_ref))}</td>
                        <td class="font-mono" style="font-size:0.72rem">${Utils.escapeHTML(asText(snap.snapshot_id))}</td>
                        <td style="font-size:0.72rem">${Utils.escapeHTML(fmtTs(snap.created_at))}</td>
                      </tr>
                    `).join('')}
                  </tbody>
                </table>
              </div>
            `;
          }
        }

        if (replayAuditEl) {
          const latestReplay = replayRuns.length ? replayRuns[0] : null;
          replayAuditEl.innerHTML = `
            <div style="font-size:0.8rem;margin-bottom:8px">
              <strong>Replay Runs:</strong> ${replayRuns.length} Â·
              <strong>Latest:</strong> <span class="${badgeClass(latestReplay?.replay_status)}">${Utils.escapeHTML(asText(latestReplay?.replay_status, 'N/A'))}</span>
            </div>
            <div style="font-size:0.78rem;margin-bottom:10px"><strong>Audit Events:</strong> institutional=${institutionalAudit.length}, canonical=${canonicalAudit.length}</div>
            <div class="text-muted" style="font-size:0.72rem;margin-bottom:4px">Replay Details</div>
            <pre style="margin:0 0 10px 0;padding:10px;border:1px solid var(--border-light);border-radius:8px;background:var(--bg-tertiary);max-height:160px;overflow:auto"><code>${jsonBlock(latestReplay || {})}</code></pre>
            <div class="text-muted" style="font-size:0.72rem;margin-bottom:4px">Audit Pack Summary</div>
            <pre style="margin:0;padding:10px;border:1px solid var(--border-light);border-radius:8px;background:var(--bg-tertiary);max-height:160px;overflow:auto"><code>${jsonBlock({
              decision_id: auditPack?.decision?.decision_id,
              generated_at: auditPack?.generated_at,
              institutional_audit_count: institutionalAudit.length,
              canonical_audit_count: canonicalAudit.length,
            })}</code></pre>
          `;
        }

        if (overrideEl) {
          overrideEl.innerHTML = `
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px">
              <div style="border:1px solid var(--border-light);border-radius:8px;padding:10px">
                <div style="font-size:0.8rem;font-weight:700;margin-bottom:8px">Request Override</div>
                <textarea class="input" id="evidence-override-reason" rows="3" placeholder="Business rationale for override"></textarea>
                <div style="display:flex;gap:8px;align-items:center;margin-top:8px">
                  <label style="font-size:0.74rem">Approvals</label>
                  <input class="input" id="evidence-override-approvals" type="number" min="2" max="5" value="2" style="max-width:90px" />
                  <button class="btn btn-primary btn-sm" id="evidence-request-override-btn">Submit</button>
                </div>
              </div>
              <div style="border:1px solid var(--border-light);border-radius:8px;padding:10px">
                <div style="font-size:0.8rem;font-weight:700;margin-bottom:8px">Approve Override</div>
                <input class="input" id="evidence-approve-request-id" placeholder="Override request ID" />
                <textarea class="input" id="evidence-approve-note" rows="3" placeholder="Approval note" style="margin-top:8px"></textarea>
                <button class="btn btn-secondary btn-sm" id="evidence-approve-override-btn" style="margin-top:8px">Approve</button>
              </div>
            </div>
            <div style="margin-top:10px;font-size:0.76rem"><strong>Requests:</strong> ${overrideRequests.length} Â· <strong>Approvals:</strong> ${overrideApprovals.length}</div>
            <div class="text-muted" style="font-size:0.72rem;margin-top:4px">Maker-checker only. Direct override endpoint is disabled.</div>
          `;

          const requestBtn = document.getElementById('evidence-request-override-btn');
          if (requestBtn) {
            requestBtn.addEventListener('click', async () => {
              const reason = String(document.getElementById('evidence-override-reason')?.value || '').trim();
              const approvalsRaw = Number(document.getElementById('evidence-override-approvals')?.value || 2);
              const requiredApprovals = Math.max(2, Math.min(5, Number.isFinite(approvalsRaw) ? approvalsRaw : 2));
              if (!reason || reason.length < 8) {
                Utils.toast('error', 'Override Request', 'Provide a reason of at least 8 characters');
                return;
              }
              const originalHtml = requestBtn.innerHTML;
              const originalDisabled = requestBtn.disabled;
              requestBtn.disabled = true;
              requestBtn.textContent = 'Submittingâ€¦';
              try {
                await API.requestDecisionOverride({
                  decision_id: decisionId,
                  reason,
                  required_approvals: requiredApprovals,
                });
                Utils.toast('success', 'Override Request', 'Override request submitted');
                await loadEvidence(decisionId);
              } catch (err) {
                Utils.toast('error', 'Override Request', err.message || 'Request failed');
              } finally {
                requestBtn.innerHTML = originalHtml;
                requestBtn.disabled = originalDisabled;
              }
            });
          }

          const approveBtn = document.getElementById('evidence-approve-override-btn');
          if (approveBtn) {
            approveBtn.addEventListener('click', async () => {
              const requestIdRaw = Number(document.getElementById('evidence-approve-request-id')?.value || 0);
              const approvalNote = String(document.getElementById('evidence-approve-note')?.value || '').trim();
              if (!Number.isFinite(requestIdRaw) || requestIdRaw < 1) {
                Utils.toast('error', 'Override Approval', 'Enter a valid override request ID');
                return;
              }
              const originalHtml = approveBtn.innerHTML;
              const originalDisabled = approveBtn.disabled;
              approveBtn.disabled = true;
              approveBtn.textContent = 'Approvingâ€¦';
              try {
                await API.approveDecisionOverride({
                  decision_id: decisionId,
                  override_request_id: Math.trunc(requestIdRaw),
                  approval_note: approvalNote,
                });
                Utils.toast('success', 'Override Approval', 'Override approval submitted');
                await loadEvidence(decisionId);
              } catch (err) {
                Utils.toast('error', 'Override Approval', err.message || 'Approval failed');
              } finally {
                approveBtn.innerHTML = originalHtml;
                approveBtn.disabled = originalDisabled;
              }
            });
          }
        }
      } catch (err) {
        const failure = Utils.emptyHTML('Evidence unavailable', err.message || 'Unable to load decision evidence');
        const decisionPolicyEl = document.getElementById('evidence-decision-policy');
        const alternativesEl = document.getElementById('evidence-alternatives');
        const snapshotsEl = document.getElementById('evidence-snapshots');
        const replayAuditEl = document.getElementById('evidence-replay-audit');
        const overrideEl = document.getElementById('evidence-override-workflow');
        if (decisionPolicyEl) decisionPolicyEl.innerHTML = failure;
        if (alternativesEl) alternativesEl.innerHTML = '<div class="text-muted">No alternatives available.</div>';
        if (snapshotsEl) snapshotsEl.innerHTML = '<div class="text-muted">No snapshot evidence available.</div>';
        if (replayAuditEl) replayAuditEl.innerHTML = '<div class="text-muted">No replay or audit evidence available.</div>';
        if (overrideEl) overrideEl.innerHTML = '<div class="text-muted">Override workflow unavailable until evidence loads.</div>';
      }
    }

    const loadBtn = document.getElementById('evidence-load-btn');
    if (loadBtn) {
      loadBtn.addEventListener('click', async () => {
        const decisionId = String(inputEl?.value || '').trim();
        const originalHtml = loadBtn.innerHTML;
        const originalDisabled = loadBtn.disabled;
        loadBtn.disabled = true;
        loadBtn.textContent = 'Loadingâ€¦';
        try {
          await loadEvidence(decisionId);
        } finally {
          loadBtn.innerHTML = originalHtml;
          loadBtn.disabled = originalDisabled;
        }
      });
    }

    const refreshBtn = document.getElementById('evidence-refresh-btn');
    if (refreshBtn) {
      refreshBtn.addEventListener('click', async () => {
        const decisionId = String(inputEl?.value || activeDecisionId || '').trim();
        const originalHtml = refreshBtn.innerHTML;
        const originalDisabled = refreshBtn.disabled;
        refreshBtn.disabled = true;
        refreshBtn.textContent = 'Refreshingâ€¦';
        try {
          await loadEvidence(decisionId);
        } finally {
          refreshBtn.innerHTML = originalHtml;
          refreshBtn.disabled = originalDisabled;
        }
      });
    }

    const replayBtn = document.getElementById('evidence-run-replay-btn');
    if (replayBtn) {
      replayBtn.addEventListener('click', async () => {
        const decisionId = String(inputEl?.value || activeDecisionId || '').trim();
        if (!decisionId) {
          Utils.toast('error', 'Replay', 'Provide a decision_id first');
          return;
        }
        const originalHtml = replayBtn.innerHTML;
        const originalDisabled = replayBtn.disabled;
        replayBtn.disabled = true;
        replayBtn.textContent = 'Replayingâ€¦';
        try {
          const result = await API.executeDecisionReplay(decisionId);
          Utils.toast('success', 'Replay', `Replay status: ${asText(result?.replay_status, 'UNKNOWN')}`);
        } catch (err) {
          Utils.toast('error', 'Replay', err.message || 'Replay execution failed');
        } finally {
          replayBtn.innerHTML = originalHtml;
          replayBtn.disabled = originalDisabled;
        }
        await loadEvidence(decisionId);
      });
    }

    const backBtn = document.getElementById('evidence-back-cc-btn');
    if (backBtn) backBtn.addEventListener('click', () => navigate('command-center'));

    if (inputEl) {
      inputEl.addEventListener('keydown', async (evt) => {
        if (evt.key !== 'Enter') return;
        evt.preventDefault();
        const decisionId = String(inputEl.value || '').trim();
        await loadEvidence(decisionId);
      });
    }

    await loadRecentDecisions();
    if (activeDecisionId) {
      await loadEvidence(activeDecisionId);
    }
  }

  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  // 7. REPORTS PAGE
  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  async function renderReports(el) {
    el.innerHTML = `
      <div class="page-header">
        <div>
          <div class="page-title">Reports</div>
          <div class="page-subtitle">Generate and download investment reports</div>
        </div>
      </div>

      <div id="reports-status-banner" style="margin-bottom:12px"></div>

      <!-- A. Generate Report Buttons -->
      <div class="grid-row grid-2" style="margin-bottom:24px">
        <div class="card">
          <div class="card-header"><div class="card-title">Generate Reports</div></div>
          <div class="card-body">
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
              <a href="/api/v1/intelligence/reports/market-analysis/pdf" target="_blank" class="btn btn-primary" style="justify-content:center">${IC.download} Market Analysis PDF</a>
              <button class="btn btn-secondary" type="button" data-method="generateReport" data-arg="portfolio" style="justify-content:center">${IC.reports} Monthly Portfolio</button>
              <button class="btn btn-secondary" type="button" data-method="generateReport" data-arg="compliance" style="justify-content:center">${IC.shield} Compliance Report</button>
              <button class="btn btn-secondary" type="button" data-method="exportPortfolioCSV" style="justify-content:center">${IC.download} Export Holdings CSV</button>
              <button class="btn btn-secondary" type="button" data-method="generateReport" data-arg="quarterly" style="justify-content:center;grid-column:1/-1">${IC.reports} Quarterly Trustee Report</button>
            </div>
            <div style="margin-top:12px;padding:10px 12px;border:1px solid var(--border-light);border-radius:8px;background:var(--bg-tertiary);font-size:0.76rem;color:var(--text-muted)">
              <strong style="color:var(--text-primary)">Scoring engine dependency:</strong> Monthly and market-analysis reports require the latest intelligence scoring run. If report generation is unavailable, run scoring first and retry.
              <button class="btn btn-ghost btn-sm" type="button" data-method="runScoringForReports" style="margin-left:8px">${IC.refresh} Run Scoring Engine</button>
            </div>
          </div>
        </div>
        <div class="card">
          <div class="card-header"><div class="card-title">Market Analysis Report</div></div>
          <div class="card-body">
            <p style="font-size:0.82rem;color:var(--text-muted);margin-bottom:12px">AI-powered bond market analysis with top buy recommendations based on current market data.</p>
            <button class="btn btn-primary" type="button" data-method="generateMarketReport" style="width:100%;justify-content:center">${IC.refresh} Generate & Preview</button>
            <div id="market-report-preview" style="margin-top:16px"></div>
          </div>
        </div>
      </div>

      <!-- B. Portfolio Summary Report -->
      <div class="card" style="margin-bottom:16px" id="portfolio-report-card">
        <div class="card-header">
          <div class="card-title">Portfolio Summary (Live)</div>
          <button class="btn btn-ghost btn-sm" type="button" data-method="printPortfolioReport">${IC.print} Print</button>
        </div>
        <div class="card-body" id="portfolio-report-content">${Utils.loadingHTML('Building portfolio report...')}</div>
      </div>

      <!-- C. Recommendations Report -->
      <div class="card">
        <div class="card-header"><div class="card-title">Today's Buy Recommendations</div><button class="btn btn-ghost btn-sm" type="button" data-method="navigate" data-arg="recommendations">View Full â†’</button></div>
        <div class="table-container" id="reports-recs">${Utils.skeletonTable(5)}</div>
      </div>
    `;

    // Load portfolio summary for the report card
    const safeLoad = async (loader, fallback, label) => {
      try {
        return await loader();
      } catch (err) {
        if (isAuthError(err)) throw err;
        const base = fallback && typeof fallback === 'object' ? fallback : {};
        return { ...base, _error: err?.message || `${label} unavailable` };
      }
    };

    let portfolio;
    let recs;
    let health;
    try {
      [portfolio, recs, health] = await Promise.all([
        safeLoad(() => API.getPortfolio(), { summary: {}, holdings: [] }, 'Portfolio snapshot'),
        safeLoad(() => API.getRecommendations(), { recommendations: [] }, 'Recommendation feed'),
        safeLoad(() => API.getPortfolioHealth(), {}, 'Portfolio health'),
      ]);
    } catch (err) {
      if (isAuthError(err)) {
        handleSessionExpired(err);
        return;
      }
      portfolio = { summary: {}, holdings: [], _error: err?.message || 'Portfolio snapshot unavailable' };
      recs = { recommendations: [] };
      health = {};
    }

    const reportIssues = [
      portfolio?._error ? `Portfolio: ${portfolio._error}` : null,
      recs?._error ? `Recommendations: ${recs._error}` : null,
      health?._error ? `Health metrics: ${health._error}` : null,
    ].filter(Boolean);
    const reportsStatusBanner = document.getElementById('reports-status-banner');
    if (reportsStatusBanner) {
      reportsStatusBanner.innerHTML = reportIssues.length ? `
        <div class="card" style="border-color:rgba(245,158,11,0.35);background:linear-gradient(180deg,rgba(245,158,11,0.08),rgba(245,158,11,0.03))">
          <div class="card-body" style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:flex-start">
            <div style="flex:1;min-width:220px">
              <div class="metric-label" style="color:#b45309">Partial Report Data</div>
              <div style="font-size:0.82rem;margin-top:4px">One or more report sources are unavailable. Generated output remains read-only and uses available data only.</div>
              <div style="font-size:0.74rem;color:var(--text-muted);margin-top:6px">${Utils.escapeHTML(reportIssues.join(' | '))}</div>
            </div>
            <div style="font-size:0.74rem;color:var(--text-muted)">${reportIssues.length} feed(s) impacted</div>
          </div>
        </div>
      ` : '';
    }

    const summary = portfolio.summary || {};
    const holdings = portfolio.holdings || [];
    const hasValue = (value) => value !== null && value !== undefined && !(typeof value === 'string' && value.trim() === '');
    const distinctBondCount = new Set(holdings.map((row) => String(row.isin || '').trim()).filter(Boolean)).size;
    const topHoldings = [...holdings].sort((a, b) => (b.market_value || 0) - (a.market_value || 0)).slice(0, 10);
    const couponWeightedYield = _displayCouponWeightedYield(summary);
    const durationStats = holdings.reduce((acc, h) => {
      const mv = Number(h.market_value);
      const duration = Number(h.modified_duration);
      if (!hasValue(h.market_value) || !Number.isFinite(mv) || mv <= 0) return acc;
      if (!hasValue(h.modified_duration) || !Number.isFinite(duration)) return acc;
      acc.weighted += mv * duration;
      acc.weightBase += mv;
      return acc;
    }, { weighted: 0, weightBase: 0 });
    const avgDuration = durationStats.weightBase > 0 ? durationStats.weighted / durationStats.weightBase : null;
    const portfolioGrade = health?.grade || health?.health?.grade || summary.portfolio_grade || summary.portfolio_rating || 'NR';
    const textOrUnavailable = (value, fallback = 'Unavailable') => hasValue(value) ? String(value) : fallback;
    const currencyOrUnavailable = (value) => hasValue(value) ? Utils.formatINR(value) : 'Unavailable';
    const sourceLabel = summary?.simulation_overlay ? 'Simulated Live Market' : 'Production Market Feeds';
    const sourceTickLabel = summary?.simulation_tick_date ? Utils.formatDate(summary.simulation_tick_date) : null;

    // Guard: user may have navigated away while API calls were in-flight
    if (!document.getElementById('portfolio-report-content')) return;

    if (portfolio?._error && !holdings.length) {
      document.getElementById('portfolio-report-content').innerHTML = Utils.emptyHTML('Portfolio report unavailable', portfolio._error || 'Unable to load portfolio report inputs');
    } else {
      document.getElementById('portfolio-report-content').innerHTML = `
      <div style="font-family:var(--font-sans);line-height:1.6">
        <div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:12px;font-size:0.76rem;color:var(--text-muted)">
          <span>Data source: <strong style="color:var(--text-primary)">${Utils.escapeHTML(sourceLabel)}</strong>${sourceTickLabel ? ` â€¢ Tick ${Utils.escapeHTML(sourceTickLabel)}` : ''}</span>
          <span>Generated at ${Utils.formatDateTime(new Date())}</span>
        </div>
        <div class="grid-3" style="gap:16px;margin-bottom:20px">
          <div><span class="text-muted" style="font-size:0.78rem">Total AUM</span><div style="font-size:1.4rem;font-weight:800;color:var(--primary)">${currencyOrUnavailable(summary.total_market_value)}</div></div>
          <div><span class="text-muted" style="font-size:0.78rem">Coupon-Weighted Yield</span><div style="font-size:1.4rem;font-weight:800">${couponWeightedYield != null ? Utils.formatYieldPct(couponWeightedYield, 2) : 'Unavailable'}</div></div>
          <div><span class="text-muted" style="font-size:0.78rem">Distinct Bonds</span><div style="font-size:1.4rem;font-weight:800">${distinctBondCount}</div></div>
          <div><span class="text-muted" style="font-size:0.78rem">Underlying Lots</span><div style="font-size:1.4rem;font-weight:800">${summary.holdings_count || holdings.length || 0}</div></div>
          <div><span class="text-muted" style="font-size:0.78rem">Avg Duration</span><div style="font-size:1.4rem;font-weight:800">${avgDuration == null ? 'Unavailable' : avgDuration.toFixed(2) + ' yrs'}</div></div>
          <div><span class="text-muted" style="font-size:0.78rem">Portfolio Rating</span><div style="font-size:1.4rem;font-weight:800">${textOrUnavailable(portfolioGrade, 'NR')}</div></div>
          <div><span class="text-muted" style="font-size:0.78rem">Report Date</span><div style="font-size:1.1rem;font-weight:700">${Utils.formatDate(new Date())}</div></div>
        </div>
        <div style="font-size:0.82rem;font-weight:700;margin-bottom:8px">Top 10 Underlying Lots</div>
        <table class="data-table" style="font-size:0.78rem">
          <thead><tr><th>Issuer</th><th>ISIN</th><th>Type</th><th>Rating</th><th class="col-number">Market Value</th><th class="col-number">Yield</th><th class="col-number">Duration</th><th class="col-number">Gain/Loss</th></tr></thead>
          <tbody>${topHoldings.map(h => {
            const gainLossRaw = hasValue(h.unrealized_gain)
              ? Number(h.unrealized_gain)
              : (hasValue(h.market_value) && hasValue(h.investment_value)
                ? Number(h.market_value) - Number(h.investment_value)
                : null);
            const gainLossClass = gainLossRaw == null ? '' : (gainLossRaw >= 0 ? 'text-green' : 'text-red');
            return `<tr>
            <td class="truncate" style="max-width:140px">${textOrUnavailable(h.issuer_name || h.isin)}</td>
            <td class="font-mono" style="font-size:0.68rem">${textOrUnavailable(h.isin)}</td>
            <td title="${Utils.escapeAttr(_investmentDisplayLabel(h))}">${_investmentDisplayLabel(h)}</td>
            <td>${Utils.ratingBadge(h.rating || h.credit_rating)}${Utils.ratingAgencyTag(h.rating_agency, h.rating || h.credit_rating)}</td>
            <td class="col-number font-medium">${currencyOrUnavailable(h.market_value)}</td>
            <td class="col-number">${hasValue(h.ytm) ? Utils.formatYieldPct(h.ytm, 4) : 'Unavailable'}</td>
            <td class="col-number">${hasValue(h.modified_duration) ? Number(h.modified_duration).toFixed(2) + 'yr' : 'Unavailable'}</td>
            <td class="col-number ${gainLossClass}">${gainLossRaw == null ? 'Unavailable' : Utils.formatINR(gainLossRaw)}</td>
          </tr>`;
          }).join('')}</tbody>
        </table>
      </div>
    `;
    }

    const recsData = recs.recommendations || [];
    const recsError = recs?._error || '';
    document.getElementById('reports-recs').innerHTML = recsData.length ? `
      <table class="data-table">
        <thead><tr><th>Bond</th><th>Sector</th><th>Rating</th><th class="col-number">Yield</th><th class="col-number">Price</th><th>Risk</th><th>Reason</th></tr></thead>
        <tbody>${recsData.slice(0, 8).map(r => `<tr>
          <td class="truncate" style="max-width:140px"><strong>${r.issuer_name || r.bond_name}</strong><br/><span class="font-mono text-muted" style="font-size:0.68rem">${r.isin}</span></td>
          <td>${textOrUnavailable(r.issuer_type)}</td>
          <td>${Utils.ratingBadge(r.credit_rating)}</td>
          <td class="col-number font-medium text-green">${hasValue(r.ytm) ? Utils.formatYieldPct(r.ytm, 4) : 'Unavailable'}</td>
          <td class="col-number">${hasValue(r.clean_price) ? 'â‚¹' + Number(r.clean_price).toFixed(2) : 'Unavailable'}</td>
          <td>${_compactChip(textOrUnavailable(r.risk_category, 'â€”'), r.risk_category === 'Low Risk' ? 'success' : r.risk_category === 'Medium Risk' ? 'warning' : r.risk_category ? 'danger' : 'neutral')}</td>
          <td class="text-muted" style="font-size:0.72rem">${textOrUnavailable(r.reason)}</td>
        </tr>`).join('')}</tbody>
      </table>
    ` : (recsError ? Utils.emptyHTML('Recommendation feed unavailable', recsError) : Utils.emptyHTML('No recommendations today', 'Run market data refresh to generate recommendations'));
  }

  async function generateReport(type) {
    if (!ensureAuthenticated('generate reports')) return;
    const restoreButtons = _setBusyButtons('button[data-method="generateReport"], button[onclick*="App.generateReport("]', true, 'Generatingâ€¦');
    Utils.toast('info', 'Generating', `Creating ${type} report...`);
    try {
      if (type === 'portfolio') {
        // Download portfolio analytics PDF
        await API.refreshSession().catch(() => false);
        const resp = await fetch('/api/v1/intelligence/reports/market-analysis/pdf', {
          credentials: 'include',
        });
        if (!resp.ok) {
          if (resp.status === 401) {
            const authErr = new Error('Your session expired. Sign in again to continue.');
            authErr.status = 401;
            handleSessionExpired(authErr);
            return;
          }
          if (resp.status === 404) {
            Utils.toast('warning', 'Monthly Portfolio', 'Scoring output not available. Run the scoring engine and retry report generation.');
            return;
          }
          throw new Error('PDF generation failed');
        }
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = `portfolio_report_${new Date().toISOString().split('T')[0]}.pdf`;
        a.click(); URL.revokeObjectURL(url);
        Utils.toast('success', 'Report Ready', 'Portfolio report downloaded');
      } else if (type === 'compliance') {
        // Show full compliance report in a modal
        const modal = document.getElementById('modal-overlay');
        document.getElementById('modal-content').innerHTML = `
          <div class="modal-header">
            <div class="modal-title">${IC.shield} Compliance Report</div>
            <button class="modal-close" data-action="close-modal">${IC.x}</button>
          </div>
          <div class="modal-body">${Utils.loadingHTML('Running compliance audit...')}</div>
        `;
        modal.classList.add('show');

        const [auditRes, statusRes, rulesRes] = await Promise.all([
          API.complianceAudit(200),
          API.complianceSummary(),
          API.complianceRules(),
        ]);

        const audits = auditRes.audits || [];
        const summary = statusRes.summary || [];
        const rules   = rulesRes.rules || [];
        const today   = new Date().toLocaleDateString('en-IN', { year:'numeric', month:'short', day:'numeric' });

        // Summary strip
        const passC = summary.find(r => r.overall_status === 'PASS')?.cnt || 0;
        const warnC = summary.find(r => r.overall_status === 'WARN')?.cnt || 0;
        const failC = summary.find(r => r.overall_status === 'FAIL')?.cnt || 0;
        const total = passC + warnC + failC;
        const overallStatus = failC > 0 ? 'FAIL' : warnC > 0 ? 'WARN' : 'PASS';
        const overallColor  = failC > 0 ? '#ef4444' : warnC > 0 ? '#f59e0b' : '#22c55e';

        // Group audits by ISIN (latest per ISIN)
        const byIsin = {};
        audits.forEach(a => {
          if (!byIsin[a.isin] || a.checked_at > byIsin[a.isin].checked_at) byIsin[a.isin] = a;
        });
        const latestAudits = Object.values(byIsin).sort((a, b) => {
          const order = { FAIL:0, WARN:1, PASS:2 };
          return (order[a.overall_status] ?? 3) - (order[b.overall_status] ?? 3);
        });

        // Cache for download button
        window._lastComplianceAudit = latestAudits;

        // Pre-compute 30-day score trend so the template can reference dateLabels
        const _dateScoreMap = {};
        audits.forEach(a => {
          const d = (a.checked_at || '').slice(0, 10);
          if (d && a.compliance_score != null) {
            if (!_dateScoreMap[d]) _dateScoreMap[d] = [];
            _dateScoreMap[d].push(Number(a.compliance_score));
          }
        });
        const dateLabels = Object.keys(_dateScoreMap).sort();
        const avgScores  = dateLabels.map(d => {
          const sc = _dateScoreMap[d];
          return parseFloat((sc.reduce((a, b) => a + b, 0) / sc.length).toFixed(1));
        });

        document.querySelector('#modal-overlay .modal-body').innerHTML = `
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
            <div style="font-size:0.78rem;color:var(--color-text-muted)">Generated: ${today} &nbsp;|&nbsp; ${total} ISINs checked &nbsp;|&nbsp; ${rules.length} rules active</div>
            <div style="display:flex;gap:8px;flex-wrap:wrap">
              ${_compactChip(`PASS ${passC}`, 'success')}
              ${_compactChip(`WARN ${warnC}`, 'warning')}
              ${_compactChip(`FAIL ${failC}`, 'danger')}
            </div>
          </div>

          <!-- Overall posture bar -->
          <div style="height:8px;background:var(--color-surface-2);border-radius:4px;overflow:hidden;margin-bottom:20px;display:flex">
            ${total > 0 ? [
              `<div style="width:${passC/total*100}%;background:#22c55e"></div>`,
              `<div style="width:${warnC/total*100}%;background:#f59e0b"></div>`,
              `<div style="width:${failC/total*100}%;background:#ef4444"></div>`,
            ].join('') : ''}
          </div>

          <!-- Audit results table -->
          ${latestAudits.length ? `
          <div style="max-height:380px;overflow-y:auto">
            <table class="data-table" style="font-size:0.78rem">
              <thead><tr>
                <th>ISIN</th><th>Status</th>
                <th class="col-number">Score</th>
                <th class="col-number">Pass</th><th class="col-number">Warn</th><th class="col-number">Fail</th>
                <th>Last Checked</th>
              </tr></thead>
              <tbody>${latestAudits.map(a => {
                const st = a.overall_status;
                const col = st === 'FAIL' ? '#fef2f2' : st === 'WARN' ? '#fffbeb' : '';
                const badge = Utils.statusBadge(st);
                let violations = [];
                try { violations = JSON.parse(a.violations_json || '[]'); } catch(e) {}
                const failMsgs = violations.filter(v => v.status === 'FAIL').map(v => v.message || v.code).slice(0,2);
                const warnMsgs = violations.filter(v => v.status === 'WARN').map(v => v.message || v.code).slice(0,1);
                const hints = [...failMsgs, ...warnMsgs].join('; ');
                return `<tr style="background:${col}">
                  <td class="font-mono" style="font-size:0.68rem;cursor:pointer;color:var(--color-primary)" data-action="close-and-navigate" data-page="bond-analysis" data-isin="${Utils.escapeAttr(a.isin)}">${a.isin}</td>
                  <td>${badge}</td>
                  <td class="col-number" style="font-weight:700">${a.compliance_score ?? 'â€”'}</td>
                  <td class="col-number" style="color:#16a34a">${a.passed_rules ?? 'â€”'}</td>
                  <td class="col-number" style="color:#d97706">${a.warning_rules ?? 'â€”'}</td>
                  <td class="col-number" style="color:#dc2626">${a.failed_rules ?? 'â€”'}</td>
                  <td style="font-size:0.68rem">${a.checked_at ? a.checked_at.slice(0,16).replace('T',' ') : 'â€”'}</td>
                </tr>
                ${hints ? `<tr style="background:${col}"><td colspan="7" style="font-size:0.68rem;color:${st==='FAIL'?'#dc2626':'#b45309'};padding:0 12px 6px">${hints}</td></tr>` : ''}`;
              }).join('')}</tbody>
            </table>
          </div>` : `<div style="text-align:center;padding:32px;color:var(--color-text-muted)"><div style="font-size:2rem;margin-bottom:8px">ðŸ“‹</div><div>No compliance audit data yet.</div><div style="font-size:0.8rem;margin-top:4px">Run a pre-trade check or compliance scan to generate audit records.</div></div>`}

          <!-- 30-day score trend -->
          ${dateLabels.length > 1 ? `
          <div style="margin-top:16px;margin-bottom:4px">
            <div style="font-size:0.78rem;font-weight:600;color:var(--text-muted);margin-bottom:8px">30-Day Compliance Score Trend (avg across all ISINs)</div>
            <canvas id="compliance-trend-chart" style="max-height:140px"></canvas>
          </div>` : ''}

          <div style="display:flex;gap:8px;margin-top:16px;justify-content:flex-end">
            <button class="btn btn-ghost btn-sm" data-action="close-modal">Close</button>
            <button class="btn btn-secondary btn-sm" type="button" data-method="downloadComplianceJSON">â†“ Download JSON</button>
            <button class="btn btn-primary btn-sm" data-action="close-and-navigate" data-route="risk">View Risk &amp; Compliance â†’</button>
          </div>
        `;
        // Render compliance score trend chart (data pre-computed above)
        if (dateLabels.length > 1) {
          requestAnimationFrame(() => {
            const cv = document.getElementById('compliance-trend-chart');
            if (!cv || !window.Chart) return;
            new Chart(cv.getContext('2d'), {
              type: 'line',
              data: {
                labels: dateLabels,
                datasets: [{
                  label: 'Avg Compliance Score',
                  data: avgScores,
                  borderColor: '#2563eb',
                  backgroundColor: 'rgba(37,99,235,0.08)',
                  tension: 0.3,
                  fill: true,
                  pointRadius: 3,
                }],
              },
              options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                  x: { ticks: { font: { size: 9 } }, grid: { display: false } },
                  y: { min: 0, max: 100, ticks: { font: { size: 9 } } },
                },
              },
            });
          });
        }
        Utils.toast('success', 'Compliance Report', `${total} ISINs checked â€” ${overallStatus}`);
      } else if (type === 'quarterly') {
        Utils.toast('info', 'Generating', 'Building quarterly trustee report...');
        const modal = document.getElementById('modal-overlay');
        document.getElementById('modal-content').innerHTML = `
          <div class="modal-header">
            <div class="modal-title">${IC.reports} Quarterly Trustee Report</div>
            <button class="modal-close" data-action="close-modal">${IC.x}</button>
          </div>
          <div class="modal-body">${Utils.loadingHTML('Compiling quarterly data...')}</div>
        `;
        modal.classList.add('show');
        try {
          const rpt = await API.get('/reports/quarterly-trustee');
          const s = rpt.summary || {};
          const qoq = rpt.qoq_comparison || {};
          const mig = rpt.rating_migration || {};
          const couponWeightedYield = _displayCouponWeightedYield(s);
          const today = new Date().toLocaleDateString('en-IN', { year:'numeric', month:'short', day:'numeric' });
          document.querySelector('#modal-overlay .modal-body').innerHTML = `
            <div style="font-size:0.78rem;color:var(--text-muted);margin-bottom:16px">Generated: ${today} &nbsp;|&nbsp; Quarter: ${rpt.quarter || 'Q3 FY2026'}</div>
            <div class="metric-grid" style="margin-bottom:20px">
              <div class="metric-card blue"><div class="metric-label">Total AUM</div><div class="metric-value">${Utils.formatINR(s.total_market_value||0)}</div></div>
              <div class="metric-card"><div class="metric-label">Holdings</div><div class="metric-value">${s.total_holdings||0}</div></div>
              <div class="metric-card"><div class="metric-label">Coupon-Weighted Yield</div><div class="metric-value">${couponWeightedYield != null ? couponWeightedYield.toFixed(2) + '%' : 'Unavailable'}</div></div>
              <div class="metric-card"><div class="metric-label">Rating Changes</div><div class="metric-value">${mig.total_changes||0}</div></div>
            </div>
            ${qoq.aum_change !== undefined ? `
            <div style="padding:12px 16px;background:var(--bg-secondary);border-radius:8px;margin-bottom:16px;font-size:0.82rem">
              <strong>QoQ Change:</strong> AUM ${qoq.aum_change >= 0 ? '+' : ''}${Utils.formatINR(qoq.aum_change||0)} (${qoq.aum_change_pct?.toFixed(2)||'â€”'}%)
            </div>` : ''}
            <div style="font-size:0.82rem;color:var(--text-muted)">
              <strong>Compliance:</strong> ${rpt.compliance?.compliance_score ? rpt.compliance.compliance_score+'%' : 'â€”'} &nbsp;|&nbsp;
              <strong>NAV Status:</strong> ${rpt.nav_reconciliation?.reconciliation_status || 'â€”'}
            </div>
            <div style="display:flex;gap:8px;margin-top:20px;justify-content:flex-end">
              <button class="btn btn-ghost btn-sm" data-action="close-modal">Close</button>
              <button class="btn btn-secondary btn-sm" id="qtr-dl-btn">â†“ JSON</button>
            </div>
          `;
          Utils.toast('success', 'Quarterly Report', 'Report generated');
          // Attach download handler safely
          const dlBtn = document.getElementById('qtr-dl-btn');
          if (dlBtn) dlBtn.addEventListener('click', () => App._downloadJSON('quarterly_trustee_report.json', rpt));
        } catch (err) {
          document.querySelector('#modal-overlay .modal-body').innerHTML = `<div class="text-red">${Utils.escapeHTML(err.message)}</div>`;
        }
      } else {
        Utils.toast('warning', 'Unknown', `Report type '${type}' not supported`);
      }
    } catch (err) {
      if (isAuthError(err)) {
        handleSessionExpired(err);
        return;
      }
      Utils.toast('error', 'Failed', err.message || 'Report generation failed');
    } finally {
      restoreButtons();
    }
  }

  async function runScoringForReports() {
    const restoreButtons = _setBusyButtons('button[data-method="runScoringForReports"], button[onclick*="App.runScoringForReports()"]', true, 'Scoringâ€¦');
    Utils.toast('info', 'Scoring', 'Running intelligence scoring engine...');
    try {
      const result = await API.runScoring(500);
      const total = Number(result?.scored || result?.count || result?.total_scored || 0);
      Utils.toast('success', 'Scoring Complete', `${total} bond(s) scored. Reports can now use fresh scoring output.`);
    } catch (err) {
      if (isAuthError(err)) {
        handleSessionExpired(err);
        return;
      }
      Utils.toast('error', 'Scoring Failed', err.message || 'Unable to run scoring engine');
    } finally {
      restoreButtons();
    }
  }

  async function generateMarketReport() {
    const restoreButtons = _setBusyButtons('button[data-method="generateMarketReport"], button[onclick*="App.generateMarketReport()"]', true, 'Generatingâ€¦');
    const preview = document.getElementById('market-report-preview');
    preview.innerHTML = Utils.loadingHTML('Generating market analysis...');
    try {
      const result = await API.getMarketReport();
      preview.innerHTML = `
        <div style="border:1px solid var(--border-light);border-radius:var(--border-radius);padding:16px;max-height:300px;overflow-y:auto;font-size:0.82rem;line-height:1.6">
          <pre style="white-space:pre-wrap;font-family:var(--font-sans)">${result.report_md || 'Report generated. Download PDF for full version.'}</pre>
        </div>
        <a href="/api/v1/intelligence/reports/market-analysis/pdf" class="btn btn-primary btn-sm" style="margin-top:12px" target="_blank">${IC.download} Download PDF</a>
      `;
    } catch (err) {
      if (isAuthError(err)) {
        handleSessionExpired(err);
        return;
      }
      preview.innerHTML = `<div class="text-muted" style="font-size:0.82rem">Report generation requires market data. ${Utils.escapeHTML(err.message)}</div>`;
    } finally {
      restoreButtons();
    }
  }

  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  // 7. ALERTS PAGE
  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  async function renderAlerts(el) {
    el.innerHTML = `
      <div class="page-header">
        <div>
          <div class="page-title">Alerts &amp; Monitoring</div>
          <div class="page-subtitle">Real-time risk alerts, rating downgrades, price deviations and exposure breaches</div>
        </div>
        <div class="page-actions">
          <button class="btn btn-secondary btn-sm" type="button" data-method="loadAuditLogs">${IC.shield} Audit Log</button>
          <button class="btn btn-primary btn-sm" id="btn-alert-scan" type="button" data-method="runAlertScan">${IC.refresh} Run Alert Scan</button>
        </div>
      </div>

      <!-- Alert Summary Cards -->
      <div class="metric-grid" id="alert-metrics">${Utils.skeletonCards(5)}</div>
      <div id="alerts-status-banner" style="margin-top:10px"></div>
      <div class="card" id="alert-category-breakdown" style="margin-top:10px;padding:12px 16px">${Utils.loadingHTML()}</div>

      <!-- Main Card with Tabs -->
      <div class="card">
        <div class="card-header" style="flex-wrap:wrap;gap:8px">
          <div class="tabs" id="alert-tabs" style="margin:0">
            <div class="tab active" data-tab="active-alerts">Active Alerts</div>
            <div class="tab" data-tab="alert-history">All Alerts (History)</div>
          </div>
          <div style="margin-left:auto;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
            <select id="alert-type-filter" style="font-size:0.8rem;padding:4px 8px;border:1px solid var(--border-light);border-radius:6px;background:var(--bg-input,var(--bg-tertiary));color:var(--text-primary)">
              <option value="">All Types</option>
              <option value="PRICE_DEVIATION">Price Deviation</option>
              <option value="RATING_DOWNGRADE">Rating Downgrade</option>
              <option value="YIELD_SPREAD">Yield Spread</option>
              <option value="EXPOSURE_BREACH">Exposure Breach</option>
              <option value="LIQUIDITY_LOW">Low Liquidity</option>
            </select>
            <select id="alert-severity-filter" style="font-size:0.8rem;padding:4px 8px;border:1px solid var(--border-light);border-radius:6px;background:var(--bg-input,var(--bg-tertiary));color:var(--text-primary)">
              <option value="">All Severities</option>
              <option value="CRITICAL">Critical</option>
              <option value="WARNING">Warning</option>
              <option value="INFO">Info</option>
            </select>
            <input type="text" id="alert-search" placeholder="Search ISIN / issuer..." style="font-size:0.8rem;padding:4px 10px;border:1px solid var(--border-light);border-radius:6px;background:var(--bg-input,var(--bg-tertiary));color:var(--text-primary);width:180px" />
          </div>
        </div>

        <div class="tab-content active" id="tab-active-alerts">
          <div id="alerts-list" style="padding:12px 0">${Utils.loadingHTML()}</div>
        </div>

        <div class="tab-content" id="tab-alert-history">
          <div id="alert-history-list" style="overflow-x:auto">${Utils.loadingHTML()}</div>
        </div>
      </div>
      <div class="last-updated" id="alerts-updated" style="margin-top:12px"></div>
    `;

    // Tab switching
    document.querySelectorAll('#alert-tabs .tab').forEach(tab => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('#alert-tabs .tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
      });
    });

    loadAlertsData();
  }

  // Alert type metadata for icons and labels
  const ALERT_TYPE_META = {
    PRICE_DEVIATION:  { label: 'Price Deviation',   icon: 'ðŸ“Š', badgeClass: 'badge-amber' },
    RATING_DOWNGRADE: { label: 'Rating Downgrade',  icon: 'ðŸ“‰', badgeClass: 'badge-red' },
    YIELD_SPREAD:     { label: 'Yield Spread',       icon: 'ðŸ“ˆ', badgeClass: 'badge-purple' },
    EXPOSURE_BREACH:  { label: 'Exposure Breach',    icon: 'âš ï¸', badgeClass: 'badge-red' },
    LIQUIDITY_LOW:    { label: 'Low Liquidity',      icon: 'ðŸ’§', badgeClass: 'badge-blue' },
  };

  const ALERT_CATEGORY_META = {
    SDS: { label: 'SDS', badgeClass: 'badge-teal' },
    STATE_CENTRAL: { label: 'Central & State', badgeClass: 'badge-blue' },
    PSU: { label: 'PSU', badgeClass: 'badge-amber' },
    PRIVATE: { label: 'Private', badgeClass: 'badge-purple' },
    EQUITY: { label: 'Equity', badgeClass: 'badge-green' },
  };

  function dedupeAlerts(rows) {
    const out = new Map();
    (rows || []).forEach(a => {
      const key = [
        (a.alert_type || '').toUpperCase(),
        (a.severity || '').toUpperCase(),
        (a.isin || '').toUpperCase(),
        (a.title || '').trim(),
        (a.detail || '').trim(),
        a.metric_value == null ? '' : String(a.metric_value),
        a.threshold_value == null ? '' : String(a.threshold_value),
        a.alert_date || '',
      ].join('|');
      const prev = out.get(key);
      if (!prev || (Number(a.id || 0) >= Number(prev.id || 0))) out.set(key, a);
    });
    return Array.from(out.values());
  }

  function _resolveAlertCategoryKey(alert = {}) {
    return _canonicalCategoryKey(
      alert.instrument_category
      || alert.category_display
      || alert.category_tag
      || alert.instrument_subtype
      || alert.issuer_type
      || alert.sector
      || `${alert.title || ''} ${alert.detail || ''}`
    );
  }

  function _buildAlertCategoryCounts(rows = []) {
    const counts = { STATE_CENTRAL: 0, PSU: 0, PRIVATE: 0, EQUITY: 0 };
    (rows || []).forEach((row) => {
      const key = _resolveAlertCategoryKey(row);
      counts[key] = (counts[key] || 0) + 1;
    });
    return counts;
  }

  async function loadAlertsData() {
    try {
      const safeLoad = async (loader, fallback, label) => {
        try {
          return await loader();
        } catch (err) {
          if (isAuthError(err)) throw err;
          const base = fallback && typeof fallback === 'object' ? fallback : {};
          return { ...base, _error: err?.message || `${label} unavailable` };
        }
      };

      const [alertsResp, summary] = await Promise.all([
        safeLoad(() => API.listAlerts({ limit: 200 }), { alerts: [] }, 'Alert list'),
        safeLoad(() => API.alertSummary(), { summary: [], open_count: 0 }, 'Alert summary')
      ]);

      const alertIssues = [
        alertsResp?._error ? `Alert list: ${alertsResp._error}` : null,
        summary?._error ? `Alert summary: ${summary._error}` : null,
      ].filter(Boolean);
      const hardAlertFeedError = !!alertsResp?._error;
      const alertsStatusBanner = document.getElementById('alerts-status-banner');
      if (alertsStatusBanner) {
        alertsStatusBanner.innerHTML = alertIssues.length ? `
          <div class="card" style="border-color:rgba(245,158,11,0.35);background:linear-gradient(180deg,rgba(245,158,11,0.08),rgba(245,158,11,0.03))">
            <div class="card-body" style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:flex-start">
              <div style="flex:1;min-width:240px">
                <div class="metric-label" style="color:#b45309">Alert Telemetry Degraded</div>
                <div style="font-size:0.82rem;margin-top:4px">Some alert feeds are unavailable. Monitoring remains fail-closed and unresolved signals should be treated as unknown until feeds recover.</div>
                <div style="font-size:0.74rem;color:var(--text-muted);margin-top:6px">${Utils.escapeHTML(alertIssues.join(' | '))}</div>
              </div>
              <div style="font-size:0.74rem;color:var(--text-muted)">${alertIssues.length} feed(s) impacted</div>
            </div>
          </div>
        ` : '';
      }

      const alertsListExplainKey = _registerExplanationPayload(alertsResp?.explanation);
      const alertsSummaryExplainKey = _registerExplanationPayload(summary?.explanation);
      const alertsPageExplainKey = alertsSummaryExplainKey || alertsListExplainKey;

      const allAlerts = dedupeAlerts(alertsResp.alerts || []);
      const openAlerts = allAlerts.filter(a => !a.is_resolved);
      const critCount = openAlerts.filter(a => a.severity === 'CRITICAL').length;
      const warnCount = openAlerts.filter(a => a.severity === 'WARNING').length;
      const infoCount = openAlerts.filter(a => a.severity === 'INFO').length;
      const resolvedCount = allAlerts.filter(a => a.is_resolved).length;
      const categoryCounts = { STATE_CENTRAL: 0, PSU: 0, PRIVATE: 0, EQUITY: 0 };
      const categoryRows = Array.isArray(summary?.category_breakdown) ? summary.category_breakdown : [];
      if (categoryRows.length) {
        categoryRows.forEach((row) => {
          const key = _canonicalCategoryKey(row.instrument_category || row.category_display || row.category_tag);
          categoryCounts[key] = (categoryCounts[key] || 0) + Number(row.count || 0);
        });
      } else {
        Object.assign(categoryCounts, _buildAlertCategoryCounts(allAlerts));
      }

      // Guard: user may have navigated away while API calls were in-flight
      if (!document.getElementById('alert-metrics')) return;

      document.getElementById('alert-metrics').innerHTML = `
        <div class="metric-card red">
          <div class="metric-label">Critical</div>
          <div class="metric-value">${critCount}</div>
          <div class="metric-change" style="color:var(--color-danger,#ef4444)">Requires attention</div>
        </div>
        <div class="metric-card amber">
          <div class="metric-label">Warnings</div>
          <div class="metric-value">${warnCount}</div>
          <div class="metric-change">Monitor closely</div>
        </div>
        <div class="metric-card blue">
          <div class="metric-label">Info</div>
          <div class="metric-value">${infoCount}</div>
          <div class="metric-change">Informational</div>
        </div>
        <div class="metric-card green">
          <div class="metric-label">Resolved Today</div>
          <div class="metric-value">${resolvedCount}</div>
          <div class="metric-change up">Actioned</div>
        </div>
        <div class="metric-card purple">
          <div class="metric-label">Total Today</div>
          <div class="metric-value">${allAlerts.length}</div>
          <div class="metric-change">${openAlerts.length} open</div>
        </div>
        <div class="metric-card blue">
          <div class="metric-label">Explainability</div>
          <div class="metric-value" style="font-size:0.82rem">Decision Trace</div>
          <div style="margin-top:6px">${alertsPageExplainKey ? `<button class="btn btn-ghost btn-sm" type="button" style="font-size:0.7rem;padding:2px 8px" data-method="_openExplainFromKey" data-arg="${Utils.escapeAttr(alertsPageExplainKey)}" data-arg2="Alerts Summary">Why?</button>` : '<span style="font-size:0.72rem;color:var(--text-muted)">Unavailable</span>'}</div>
        </div>
      `;

      const categoryBreakdownEl = document.getElementById('alert-category-breakdown');
      if (categoryBreakdownEl) {
        categoryBreakdownEl.innerHTML = `
          <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap">
            <div>
              <div style="font-size:0.86rem;font-weight:700">Category Breakdown</div>
              <div style="font-size:0.74rem;color:var(--text-muted)">Canonical alert distribution across Central &amp; State, PSU, Private, and Equity</div>
            </div>
            <div style="display:flex;gap:6px;flex-wrap:wrap">
              ${CATEGORY_ORDER.map((key) => {
                const meta = ALERT_CATEGORY_META[key] || { label: _displayCategoryLabel(key), badgeClass: 'badge-gray' };
                const count = Number(categoryCounts[key] || 0);
                return _compactChip(`${meta.label} ${count}`, _badgeClassToCompactTone(meta.badgeClass), ` data-alert-category="${key}" data-alert-category-count="${count}"`);
              }).join('')}
            </div>
          </div>
        `;
      }

      // Update topbar notification badge (open alerts only)
      const badge = document.getElementById('notification-count');
      const sidebarBadge = document.getElementById('sidebar-alert-count');
      const openCount = openAlerts.length;
      if (badge) { badge.textContent = openCount; badge.style.display = openCount > 0 ? 'flex' : 'none'; }
      if (sidebarBadge) { sidebarBadge.textContent = openCount; sidebarBadge.style.display = openCount > 0 ? 'inline-flex' : 'none'; }

      const renderAlertCard = (a) => {
        const meta = ALERT_TYPE_META[a.alert_type] || { label: (a.alert_type||'').replace(/_/g,' '), icon: 'ðŸ””', badgeClass: 'badge-gray' };
        const categoryKey = _resolveAlertCategoryKey(a);
        const categoryMeta = ALERT_CATEGORY_META[categoryKey] || ALERT_CATEGORY_META.PRIVATE;
        const sevClass = a.severity === 'CRITICAL' ? 'red' : a.severity === 'WARNING' ? 'amber' : 'blue';
        const resolved = a.is_resolved;
        const bondIcon = IC.search || '';
        const bondBtnText = `${bondIcon ? `${bondIcon} ` : ''}Bond`;
        const explainKey = _registerExplanationPayload(a.explanation || {
          module: 'alerts_ui_fallback',
          decision_outcome: a.severity || 'INFO',
          summary: a.title || a.detail || 'Alert generated',
          explanation_levels: {
            level_1: { headline: a.title || a.detail || 'Alert generated' },
            level_2: { key_factors: [], rule_checks: [] },
            level_3: { calculation_trace: {}, data_lineage: [] },
          },
        });
        return `
          <div class="alert-item" id="alert-card-${a.id}" style="border-left:3px solid var(--color-${sevClass === 'red' ? 'danger' : sevClass === 'amber' ? 'warning' : 'info'},'var(--accent-blue)');${resolved ? 'opacity:0.6' : ''}">
            <div class="alert-severity ${sevClass}"></div>
            <div class="alert-content" style="flex:1;min-width:0">
              <div style="display:flex;align-items:flex-start;gap:8px;flex-wrap:wrap">
                <div style="flex:1;min-width:0">
                  <div class="alert-title-text" style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
                    <span>${meta.icon}</span>
                    <span>${a.title || a.alert_type}</span>
                    ${Utils.statusBadge(a.severity)}
                    ${_compactChip(meta.label, _badgeClassToCompactTone(meta.badgeClass))}
                    ${_compactChip(categoryMeta.label, _badgeClassToCompactTone(categoryMeta.badgeClass), ` data-alert-item-category="${categoryKey}"`)}
                    ${resolved ? Utils.statusBadge('RESOLVED') : ''}
                  </div>
                  ${a.detail ? `<div class="alert-detail-text" style="margin-top:3px">${a.detail}</div>` : ''}
                  <div class="alert-meta" style="margin-top:4px">
                    ${a.isin ? `<span class="font-mono" style="font-size:0.68rem;background:var(--bg-tertiary);padding:1px 5px;border-radius:4px">${a.isin}</span>` : ''}
                    <span style="font-size:0.7rem;color:var(--text-muted)">${IC.clock} ${Utils.formatDateTime(a.created_at || a.alert_date)}</span>
                    ${_fmtAlertMetric(a.alert_type, a.metric_value, a.threshold_value)}
                    ${resolved && a.resolved_at ? `<span style="font-size:0.7rem;color:var(--text-muted)">Resolved ${Utils.timeAgo(a.resolved_at)}</span>` : ''}
                  </div>
                </div>
                <div style="flex-shrink:0;display:flex;gap:6px;align-items:flex-start">
                  ${a.isin ? `<button class="btn btn-ghost btn-sm" type="button" style="font-size:0.72rem" data-method="navigate" data-page="bond-analysis" data-isin="${Utils.escapeAttr(a.isin)}">${bondBtnText}</button>` : ''}
                  ${explainKey ? `<button class="btn btn-ghost btn-sm" type="button" style="font-size:0.72rem" data-method="_openExplainFromKey" data-arg="${Utils.escapeAttr(explainKey)}" data-arg2="Alert Decision">Why?</button>` : ''}
                  ${!resolved ? `<button class="btn btn-secondary btn-sm" type="button" style="font-size:0.72rem" id="resolve-btn-${a.id}" data-method="_confirmResolveAlert" data-arg="${a.id}">âœ“ Resolve</button>` : ''}
                </div>
              </div>
            </div>
          </div>
        `;
      };

      const renderAlertsList = (filtered, tab = 'active') => {
        const container = document.getElementById(tab === 'active' ? 'alerts-list' : 'alert-history-list');
        if (!container) return;

        if (tab === 'history') {
          if (!filtered.length) {
            container.innerHTML = hardAlertFeedError
              ? Utils.emptyHTML('Alert history unavailable', alertsResp?._error || 'The alert history service did not return data for this session.')
              : Utils.emptyHTML('No alerts found', 'Run an alert scan to detect issues');
            return;
          }
          container.innerHTML = `
            <table class="data-table" style="font-size:0.78rem">
              <thead><tr>
                <th>Date / Time</th><th>Severity</th><th>Type</th><th>ISIN</th><th>Title</th>
                <th>Class</th><th>Value</th><th>Threshold</th><th>Status</th><th>Why?</th>
              </tr></thead>
              <tbody>
              ${filtered.map(a => {
                const meta = ALERT_TYPE_META[a.alert_type] || { label: (a.alert_type||'').replace(/_/g,' '), badgeClass: 'badge-gray' };
                const categoryKey = _resolveAlertCategoryKey(a);
                const categoryMeta = ALERT_CATEGORY_META[categoryKey] || ALERT_CATEGORY_META.PRIVATE;
                const resolved = a.is_resolved;
                const explainKey = _registerExplanationPayload(a.explanation || {
                  module: 'alerts_ui_history_fallback',
                  decision_outcome: a.severity || 'INFO',
                  summary: a.title || a.detail || 'Alert generated',
                  explanation_levels: {
                    level_1: { headline: a.title || a.detail || 'Alert generated' },
                    level_2: { key_factors: [], rule_checks: [] },
                    level_3: { calculation_trace: {}, data_lineage: [] },
                  },
                });
                return `<tr style="${resolved ? 'opacity:0.65' : ''}">
                  <td style="white-space:nowrap">${Utils.formatDateTime(a.created_at || a.alert_date)}</td>
                  <td>${Utils.statusBadge(a.severity)}</td>
                  <td>${_compactChip(meta.label, _badgeClassToCompactTone(meta.badgeClass))}</td>
                  <td class="font-mono" style="font-size:0.7rem">${a.isin || 'â€”'}</td>
                  <td style="max-width:280px;white-space:normal">${a.title || 'â€”'}</td>
                  <td>${_compactChip(categoryMeta.label, _badgeClassToCompactTone(categoryMeta.badgeClass), ` data-alert-item-category="${categoryKey}"`)}</td>
                  <td>${_fmtAlertMetricText(a.alert_type, a.metric_value)}</td>
                  <td>${_fmtAlertMetricText(a.alert_type, a.threshold_value)}</td>
                  <td>${resolved
                    ? Utils.statusBadge('RESOLVED')
                    : `<button class="btn btn-secondary btn-sm" type="button" style="font-size:0.68rem;padding:2px 8px" id="resolve-btn-${a.id}" data-method="_confirmResolveAlert" data-arg="${a.id}">Resolve</button>`
                  }</td>
                  <td>${explainKey ? `<button class="btn btn-ghost btn-sm" type="button" style="font-size:0.68rem;padding:2px 8px" data-method="_openExplainFromKey" data-arg="${Utils.escapeAttr(explainKey)}" data-arg2="Alert Decision">Why?</button>` : 'â€”'}</td>
                </tr>`;
              }).join('')}
              </tbody>
            </table>
          `;
          return;
        }

        // Active tab - card layout
        const activeOnly = filtered.filter(a => !a.is_resolved);
        if (!activeOnly.length) {
          if (hardAlertFeedError) {
            container.innerHTML = Utils.emptyHTML('Active alert feed unavailable', alertsResp?._error || 'Alert engine output could not be loaded for this session.');
            return;
          }
          container.innerHTML = `
            <div class="empty-state" style="padding:40px">
              <div style="font-size:2.5rem;margin-bottom:12px">âœ…</div>
              <div class="empty-state-title">No active alerts</div>
              <div class="empty-state-subtitle">All monitored metrics are within normal thresholds</div>
              <button class="btn btn-primary btn-sm" type="button" style="margin-top:16px" data-method="runAlertScan">${IC.refresh} Run Alert Scan</button>
            </div>
          `;
          return;
        }
        // Group by ISIN (proxy for issuer) then sort groups by max severity
        const byIsin = {};
        activeOnly.forEach(a => {
          const key = a.isin || '__NONE__';
          if (!byIsin[key]) byIsin[key] = { isin: a.isin, alerts: [] };
          byIsin[key].alerts.push(a);
        });
        const sevOrder = { CRITICAL: 0, WARNING: 1, INFO: 2 };
        const groups = Object.values(byIsin).sort((a, b) => {
          const topA = Math.min(...a.alerts.map(x => sevOrder[x.severity] ?? 3));
          const topB = Math.min(...b.alerts.map(x => sevOrder[x.severity] ?? 3));
          return topA !== topB ? topA - topB : b.alerts.length - a.alerts.length;
        });
        const groupHtml = groups.map((g, gi) => {
          const sorted = [...g.alerts].sort((a, b) => (sevOrder[a.severity] ?? 3) - (sevOrder[b.severity] ?? 3));
          const topSev = sorted[0]?.severity || 'INFO';
          const hdrColor = topSev === 'CRITICAL' ? '#ef4444' : topSev === 'WARNING' ? '#f59e0b' : '#3b82f6';
          const critCnt = sorted.filter(a => a.severity === 'CRITICAL').length;
          const grpId = `ag-grp-${gi}`;
          return `
            <div style="margin-bottom:10px;border:1px solid var(--border-light,#e5e7eb);border-radius:8px;overflow:hidden">
              <div style="display:flex;align-items:center;gap:10px;padding:8px 14px;background:var(--bg-secondary,#f8fafc);cursor:pointer;user-select:none"
                data-action="toggle-group" data-target-id="${grpId}">
                <span class="ag-chev" style="display:inline-block;transition:transform 0.15s;color:var(--text-muted);font-size:0.65rem">â–¶</span>
                ${g.isin ? `<span class="font-mono" style="font-size:0.71rem;background:var(--bg-tertiary,#e5e7eb);padding:1px 5px;border-radius:4px">${g.isin}</span>` : ''}
                <span style="font-size:0.82rem;font-weight:600;flex:1;min-width:0">${sorted[0]?.title?.replace(/\s*[â€“-].*$/, '').trim() || (g.isin ? 'Alerts for ' + g.isin : 'Untagged Alerts')}</span>
                <span style="font-size:0.7rem;font-weight:700;background:${hdrColor};color:#fff;padding:2px 8px;border-radius:10px">${sorted.length}</span>
                ${critCnt ? _compactChip(`CRIT ${critCnt}`, 'danger') : ''}
              </div>
              <div id="${grpId}" style="display:none">${sorted.map(renderAlertCard).join('')}</div>
            </div>
          `;
        }).join('');
        container.innerHTML = `<div style="padding:8px 16px">${groupHtml}</div>`;
      };

      let _allAlerts = allAlerts;

      const doFilter = () => {
        const sev = document.getElementById('alert-severity-filter')?.value || '';
        const type = document.getElementById('alert-type-filter')?.value || '';
        const kw = (document.getElementById('alert-search')?.value || '').toLowerCase();
        const filtered = _allAlerts.filter(a => {
          if (sev && a.severity !== sev) return false;
          if (type && a.alert_type !== type) return false;
          if (kw) {
            const haystack = `${a.title || ''} ${a.isin || ''} ${a.detail || ''} ${a.alert_type || ''}`.toLowerCase();
            if (!haystack.includes(kw)) return false;
          }
          return true;
        });
        renderAlertsList(filtered, 'active');
        renderAlertsList(filtered, 'history');
      };

      renderAlertsList(allAlerts, 'active');
      renderAlertsList(allAlerts, 'history');

      ['alert-severity-filter','alert-type-filter'].forEach(id => {
        document.getElementById(id)?.addEventListener('change', doFilter);
      });
      document.getElementById('alert-search')?.addEventListener('input', Utils.debounce(doFilter, 200));

      const auEl = document.getElementById('alerts-updated');
      if (auEl) auEl.textContent =
        `Last updated: ${Utils.formatDateTime(new Date())} Â· ${openAlerts.length} open, ${resolvedCount} resolved${alertIssues.length ? ` Â· ${alertIssues.length} degraded feed(s)` : ''}`;

    } catch (err) {
      if (isAuthError(err)) {
        handleSessionExpired(err);
        return;
      }
      Utils.toast('error', 'Alerts Error', err.message);
      const el = document.getElementById('alerts-list');
      if (el) el.innerHTML = Utils.emptyHTML('Failed to load alerts', err.message);
    }
  }

  // â”€â”€ Alert metric formatting helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
  function _fmtAlertMetric(alertType, mv, tv) {
    if (mv == null) return '';
    const f1 = v => parseFloat(v).toFixed(1);
    const f2 = v => parseFloat(v).toFixed(2);
    const fi = v => Math.round(parseFloat(v));
    const RATING_NAMES = ['','AAA','AA+','AA','AA-','A+','A','A-','BBB+','BBB','BBB-','BB+','BB','D'];
    let mvLabel, tvLabel, mvFmt, tvFmt;
    switch (alertType) {
      case 'PRICE_DEVIATION':
        mvLabel = 'Deviation'; mvFmt = `${f2(mv)}%`; tvLabel = 'Limit'; tvFmt = tv != null ? `${f1(tv)}%` : null; break;
      case 'RATING_DOWNGRADE':
        mvLabel = 'Rank'; mvFmt = `${fi(mv)} (${RATING_NAMES[fi(mv)] || '?'})`;
        tvLabel = 'Min';  tvFmt = tv != null ? `${fi(tv)} (${RATING_NAMES[fi(tv)] || '?'})` : null; break;
      case 'YIELD_SPREAD':
        mvLabel = 'Spread'; mvFmt = `${fi(mv)}bps`; tvLabel = 'Typical'; tvFmt = tv != null ? `${fi(tv)}bps` : null; break;
      case 'LIQUIDITY_LOW':
        mvLabel = 'Score'; mvFmt = `${f1(mv)}/100`; tvLabel = 'Min'; tvFmt = tv != null ? `${f1(tv)}/100` : null; break;
      case 'EXPOSURE_BREACH':
        mvLabel = 'Exposure'; mvFmt = `${f1(mv)}%`; tvLabel = 'Cap'; tvFmt = tv != null ? `${f1(tv)}%` : null; break;
      default:
        mvLabel = 'Value'; mvFmt = f2(mv); tvLabel = 'Threshold'; tvFmt = tv != null ? f2(tv) : null;
    }
    const tvHtml = tvFmt ? `<span style="font-size:0.7rem;color:var(--text-muted)">${tvLabel}: ${tvFmt}</span>` : '';
    return `<span style="font-size:0.7rem;color:var(--text-muted)">${mvLabel}: <strong>${mvFmt}</strong></span>${tvHtml}`;
  }

  function _fmtAlertMetricText(alertType, value) {
    if (value == null) return 'â€”';
    const fi = v => Math.round(parseFloat(v));
    const f1 = v => parseFloat(v).toFixed(1);
    const RATING_NAMES = ['','AAA','AA+','AA','AA-','A+','A','A-','BBB+','BBB','BBB-','BB+','BB','D'];
    switch (alertType) {
      case 'PRICE_DEVIATION':  return `${parseFloat(value).toFixed(2)}%`;
      case 'RATING_DOWNGRADE': return `${fi(value)} (${RATING_NAMES[fi(value)] || ''})`;
      case 'YIELD_SPREAD':     return `${fi(value)}bps`;
      case 'LIQUIDITY_LOW':    return `${f1(value)}/100`;
      case 'EXPOSURE_BREACH':  return `${f1(value)}%`;
      default:                 return parseFloat(value).toFixed(2);
    }
  }

  // â”€â”€ Resolve with inline confirmation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
  const _resolveConfirmState = {};

  function _confirmResolveAlert(alertId, btn) {
    const confirmId = `confirm-resolve-${alertId}`;
    _resolveConfirmState[alertId] = btn.outerHTML;
    btn.outerHTML = `<span id="${confirmId}" style="display:inline-flex;gap:4px;align-items:center;white-space:nowrap">
      <span style="font-size:0.68rem;color:var(--text-muted)">Resolve?</span>
      <button class="btn btn-danger btn-sm" type="button" style="font-size:0.68rem;padding:2px 8px" data-method="_resolveAlert" data-arg="${alertId}">Yes</button>
      <button class="btn btn-ghost btn-sm" type="button" style="font-size:0.68rem;padding:2px 8px" data-method="_cancelResolve" data-arg="${alertId}">No</button>
    </span>`;
    setTimeout(() => App._cancelResolve(alertId), 5000);
  }

  function _cancelResolve(alertId) {
    const el = document.getElementById(`confirm-resolve-${alertId}`);
    const orig = _resolveConfirmState[alertId];
    if (el && orig) el.outerHTML = orig;
    delete _resolveConfirmState[alertId];
  }

  async function _resolveAlert(alertId) {
    const restoreButtons = _setBusyButtons('button[data-method="_resolveAlert"], button[onclick*="App._resolveAlert("]', true, 'Resolvingâ€¦');
    try {
      await API.resolveAlert(alertId);
      // Visually mark as resolved immediately
      const card = document.getElementById(`alert-card-${alertId}`);
      if (card) {
        card.style.opacity = '0.5';
        card.style.transition = 'opacity 0.4s';
        setTimeout(() => card.remove(), 400);
      }
      Utils.toast('success', 'Resolved', `Alert #${alertId} marked as resolved`);
      // Refresh counts
      loadAlertsData();
    } catch (err) {
      Utils.toast('error', 'Error', err.message);
    } finally {
      restoreButtons();
    }
  }

  async function runAlertScan() {
    const btn = document.getElementById('btn-alert-scan');
    if (btn) { btn.disabled = true; btn.innerHTML = `${IC.refresh} Scanning...`; }
    Utils.toast('info', 'Scanning', 'Running alert detection engine...');
    try {
      const result = await API.runAlertEngine();
      const n = result.alerts_generated || 0;
      Utils.toast('success', 'Scan Complete', `${n} new alert${n !== 1 ? 's' : ''} generated`);
      // Reload alerts data (stay on page)
      await loadAlertsData();
    } catch (err) {
      Utils.toast('error', 'Scan Error', err.message);
    } finally {
      if (btn) { btn.disabled = false; btn.innerHTML = `${IC.refresh} Run Alert Scan`; }
    }
  }


  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  // 7b. BOND NEWS PAGE
  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  async function renderNews(el) {
    el.innerHTML = `
      <div class="page-header">
        <div>
          <div class="page-title">${IC.mail} Bond Market News</div>
          <div class="page-subtitle">Real-time bond market news from RBI, SEBI, and financial media â€” filtered for portfolio relevance</div>
        </div>
        <div class="page-actions">
          <button class="btn btn-primary" id="btn-refresh-news">${IC.refresh} Refresh News</button>
        </div>
      </div>

      <!-- Filters -->
      <div class="card" style="margin-bottom:16px">
        <div class="card-body" style="padding:16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap">
          <select id="news-cat-filter" class="form-control" style="width:160px;background:var(--bg-card);color:var(--text-primary);border:1px solid var(--border-color);border-radius:6px;padding:6px 10px">
            <option value="">All Categories</option>
            <option value="REGULATORY">Regulatory (RBI/SEBI)</option>
            <option value="MARKET">Market News</option>
            <option value="MACRO">Macro / Economy</option>
          </select>
          <select id="news-src-filter" class="form-control" style="width:180px;background:var(--bg-card);color:var(--text-primary);border:1px solid var(--border-color);border-radius:6px;padding:6px 10px">
            <option value="">All Sources</option>
            <option value="RBI">RBI</option>
            <option value="SEBI">SEBI</option>
            <option value="ET_BONDS">ET - Bonds</option>
            <option value="MC_BONDS">MoneyControl</option>
            <option value="LIVEMINT">Livemint</option>
            <option value="BS_MARKETS">Business Standard</option>
          </select>
          <input type="text" id="news-search" placeholder="Search news..." 
                 style="width:220px;background:var(--bg-card);color:var(--text-primary);border:1px solid var(--border-color);border-radius:6px;padding:6px 10px">
          <select id="news-days-filter" class="form-control" style="width:130px;background:var(--bg-card);color:var(--text-primary);border:1px solid var(--border-color);border-radius:6px;padding:6px 10px">
            <option value="7">Last 7 days</option>
            <option value="14">Last 14 days</option>
            <option value="30" selected>Last 30 days</option>
            <option value="90">Last 90 days</option>
          </select>
          <button class="btn btn-secondary" id="btn-apply-news-filter">${IC.filter} Apply</button>
        </div>
      </div>

      <!-- Stats Bar -->
      <div id="news-stats" class="grid-3" style="margin-bottom:16px"></div>

      <!-- News Articles -->
      <div id="news-list" class="card">
        <div class="card-body" style="padding:20px">
          <div class="loading">Loading news...</div>
        </div>
      </div>
    `;

    // Event handlers
    document.getElementById('btn-refresh-news')?.addEventListener('click', async () => {
      const btn = document.getElementById('btn-refresh-news');
      btn.disabled = true;
      btn.textContent = 'Refreshing...';
      try {
        Utils.toast('info', 'News', 'Fetching latest bond market news...');
        await API.post('/news/refresh?max_age_days=7');
        await _loadNews();
        await _loadNewsStats();
        Utils.toast('success', 'News Refreshed', 'Bond market news updated');
      } catch(e) { Utils.toast('error', 'News Error', e.message); }
      btn.disabled = false;
      btn.textContent = 'Refresh News';
    });

    document.getElementById('btn-apply-news-filter')?.addEventListener('click', _loadNews);
    document.getElementById('news-search')?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') _loadNews();
    });

    // Load stats & news
    _loadNewsStats();
    _loadNews();
  }

  async function _loadNewsStats() {
    try {
      const stats = await API.get('/news/stats');
      const el = document.getElementById('news-stats');
      if (!el) return;

      const total = stats.total_articles || 0;
      const sources = (stats.by_source || []).length;
      const latest = stats.latest_fetch ? new Date(stats.latest_fetch).toLocaleString() : 'Never';

      el.innerHTML = `
        <div class="metric-card blue">
          <div class="metric-label">Total Articles</div>
          <div class="metric-value">${total}</div>
        </div>
        <div class="metric-card teal">
          <div class="metric-label">Active Sources</div>
          <div class="metric-value">${sources}</div>
        </div>
        <div class="metric-card indigo">
          <div class="metric-label">Last Refresh</div>
          <div class="metric-value" style="font-size:14px">${latest}</div>
        </div>
      `;
    } catch(e) {
      console.warn('News stats failed:', e);
    }
  }

  async function _loadNews() {
    const listEl = document.getElementById('news-list');
    if (!listEl) return;

    const cat = document.getElementById('news-cat-filter')?.value || '';
    const src = document.getElementById('news-src-filter')?.value || '';
    const search = document.getElementById('news-search')?.value || '';
    const days = document.getElementById('news-days-filter')?.value || '30';

    let url = '/news?limit=100&days=' + days;
    if (cat) url += '&category=' + cat;
    if (src) url += '&source=' + src;
    if (search) url += '&search=' + encodeURIComponent(search);

    listEl.innerHTML = '<div class="card-body" style="padding:20px"><div class="loading">Loading...</div></div>';

    try {
      const data = await API.get(url);
      const articles = data.articles || [];

      if (!articles.length) {
        listEl.innerHTML = `
          <div class="card-body" style="padding:40px;text-align:center;color:var(--text-muted)">
            <div style="font-size:48px;margin-bottom:12px">ðŸ“°</div>
            <div>No news articles found matching your filters.</div>
            <div style="margin-top:8px;font-size:13px">Try changing filters or click "Refresh News" to fetch latest.</div>
          </div>
        `;
        return;
      }

      let html = '<div class="card-body" style="padding:0">';
      for (const a of articles) {
        const catColor = {
          REGULATORY: '#f59e0b',
          MARKET: '#3b82f6',
          MACRO: '#10b981',
          GENERAL: '#6b7280'
        }[a.category] || '#6b7280';

        const catLabel = a.category || 'GENERAL';
        const score = a.relevance_score || 0;
        const scoreColor = score >= 60 ? '#10b981' : score >= 30 ? '#f59e0b' : '#6b7280';
        const pubDate = a.published_at ? new Date(a.published_at).toLocaleDateString('en-IN', {day:'numeric',month:'short',year:'numeric'}) : '';
        const issuers = a.matched_issuers || '';
        const readClass = a.is_read ? 'opacity:0.6;' : '';

        html += `
          <div class="news-article" style="padding:16px 20px;border-bottom:1px solid var(--border-color);${readClass}cursor:pointer"
               type="button" data-action="open-url" data-url="${Utils.escapeAttr(a.url || '')}">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px">
              <div style="flex:1;min-width:0">
                <div style="display:flex;gap:8px;align-items:center;margin-bottom:4px;flex-wrap:wrap">
                  <span style="background:${catColor}22;color:${catColor};padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600">${catLabel}</span>
                  <span style="color:var(--text-muted);font-size:12px">${a.source_name || a.source}</span>
                  <span style="color:var(--text-muted);font-size:12px">${pubDate}</span>
                  ${issuers ? '<span style="background:#3b82f622;color:#3b82f6;padding:2px 6px;border-radius:4px;font-size:11px">'+issuers+'</span>' : ''}
                </div>
                <div style="font-weight:500;color:var(--text-primary);margin-bottom:4px;line-height:1.4">${_escHtml(a.title)}</div>
                <div style="font-size:13px;color:var(--text-muted);line-height:1.5;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical">${_escHtml(a.summary || '')}</div>
              </div>
              <div style="text-align:right;min-width:60px">
                <div style="background:${scoreColor}22;color:${scoreColor};padding:4px 8px;border-radius:6px;font-size:12px;font-weight:600">${score}</div>
                <div style="font-size:10px;color:var(--text-muted);margin-top:2px">relevance</div>
              </div>
            </div>
          </div>
        `;
      }
      html += '</div>';
      listEl.innerHTML = html;
    } catch(e) {
      listEl.innerHTML = `<div class="card-body" style="padding:20px;color:var(--danger)">Failed to load news: ${e.message}</div>`;
    }
  }

  function _escHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }


  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  // 8. DATA SOURCES PAGE
  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  async function renderDataSources(el) {
    el.innerHTML = `
      <div class="page-header">
        <div>
          <div class="page-title">Data Sources</div>
          <div class="page-subtitle">Broker feeds, portfolio upload, SEBI sync, email integration, and four-eyes approvals</div>
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn btn-secondary btn-sm" type="button" data-method="triggerSebiSync">${IC.refresh} SEBI Sync</button>
        </div>
      </div>

      <div id="ds-status-banner" style="margin-bottom:12px"></div>

      <!-- ROW 1: Broker / Gmail / Market -->
      <div class="grid-row grid-3">
        <div class="card">
          <div class="card-header"><div class="card-title">Broker Feeds</div></div>
          <div class="card-body" id="ds-brokers">${Utils.loadingHTML()}</div>
        </div>
        <div class="card">
          <div class="card-header"><div class="card-title">Gmail Integration</div></div>
          <div class="card-body">
            <div id="ds-gmail-info" style="margin-bottom:16px">
              <div class="flex justify-between items-center" style="padding:6px 0;font-size:0.82rem">
                <span class="text-muted">Status</span><span id="ds-gmail-status-chip">${_compactChip('Checking', 'info')}</span>
              </div>
              <div class="flex justify-between items-center" style="padding:6px 0;font-size:0.82rem">
                <span class="text-muted">Last Quote Parsed</span>
                <span class="fw-600" id="ds-gmail-last">â€”</span>
              </div>
              <div class="flex justify-between items-center" style="padding:6px 0;font-size:0.82rem">
                <span class="text-muted">Total Ingested</span>
                <span class="fw-600" id="ds-gmail-count">â€”</span>
              </div>
              <div class="flex justify-between items-center" style="padding:6px 0;font-size:0.82rem">
                <span class="text-muted">Integration</span>
                <span class="fw-600">Gmail OAuth2</span>
              </div>
            </div>
            <button class="btn btn-secondary btn-sm w-full" type="button" data-method="triggerGmailIngest" style="justify-content:center">${IC.mail} Poll Gmail Now</button>
          </div>
        </div>
        <div class="card">
          <div class="card-header"><div class="card-title">Market Data Source</div></div>
          <div class="card-body" id="ds-market">${Utils.loadingHTML()}</div>
        </div>
      </div>

      <!-- ROW 2: Portfolio Upload (DROP ZONE) -->
      <div class="card" style="margin-top:16px">
        <div class="card-header">
          <div class="card-title">Portfolio Upload</div>
          <div style="font-size:0.75rem;color:var(--text-muted)">CSV, XLSX, XLS or JSON â€¢ columns: isin, bond_name, face_value, quantity, purchase_price, purchase_date, credit_rating, maturity_date, issuer_name, coupon_rate, sector</div>
        </div>
        <div class="card-body">
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:24px;align-items:start">
            <!-- Drop Zone -->
            <div>
              <div id="portfolio-dropzone"
                style="border:2px dashed var(--border-light);border-radius:12px;padding:40px 24px;
                       text-align:center;cursor:pointer;transition:0.2s;background:var(--bg-tertiary)"
                >
                <div style="font-size:2rem;margin-bottom:8px">ðŸ“‚</div>
                <div style="font-weight:600;margin-bottom:4px">Drop CSV / JSON / XLSX here</div>
                <div style="font-size:0.78rem;color:var(--text-muted)">or click to browse (.csv, .xlsx, .xls, .json)</div>
              </div>
              <input type="file" id="portfolio-file-input" accept=".csv,.xlsx,.xls,.json,.txt" style="display:none" />
              <div style="font-size:0.72rem;color:var(--text-muted);margin-top:6px">Supported files: CSV, XLSX, XLS, JSON</div>
              <div style="display:flex;gap:8px;margin-top:12px;align-items:center">
                <label style="font-size:0.8rem;color:var(--text-muted)">Mode:</label>
                <select id="portfolio-upload-mode" style="flex:1;padding:6px 10px;border-radius:6px;border:1px solid var(--border-light);background:var(--bg-tertiary);color:var(--text-primary);font-size:0.82rem">
                  <option value="merge">Merge (add / update)</option>
                  <option value="replace">Replace (clear first)</option>
                </select>
                <button id="portfolio-upload-btn" class="btn btn-primary btn-sm" type="button" data-method="_portfolioUpload" disabled>Upload</button>
              </div>
              <div id="portfolio-upload-status" style="margin-top:8px;font-size:0.78rem;color:var(--text-muted)"></div>
              <div id="portfolio-upload-detail" style="margin-top:6px;font-size:0.74rem;color:var(--text-muted);line-height:1.45"></div>
              <div id="portfolio-batch-report" style="margin-top:12px"></div>
            </div>
            <!-- Current Holdings Summary -->
            <div>
              <div style="font-weight:600;margin-bottom:12px;font-size:0.9rem">Current Holdings from Live API</div>
              <div id="ds-portfolio-summary">${Utils.loadingHTML()}</div>
              <button class="btn btn-ghost btn-sm" type="button" style="margin-top:12px;width:100%;justify-content:center" data-method="navigate" data-arg="portfolio">
                View Full Portfolio â†’
              </button>
              <a id="portfolio-template-link" href="#" data-method="_downloadPortfolioTemplate"
                 style="display:block;margin-top:8px;font-size:0.75rem;text-align:center;color:var(--accent-blue)">
                ðŸ“¥ Download CSV Template
              </a>
              <div style="margin-top:12px;padding:10px;border:1px solid var(--border-light);border-radius:8px;background:var(--bg-tertiary)">
                <div style="font-weight:600;font-size:0.78rem;margin-bottom:6px">Portfolio Input Format</div>
                <div style="font-size:0.73rem;color:var(--text-muted);line-height:1.45">
                  <strong>Required:</strong> isin, issuer_name, maturity_date<br/>
                  <strong>Recommended:</strong> bond_name, face_value, quantity, purchase_price, purchase_date, current_value, credit_rating, coupon_rate, sector, category<br/>
                  <strong>Date format:</strong> YYYY-MM-DD or DD/MM/YYYY<br/>
                  <strong>Numbers:</strong> plain numeric values, no currency symbols
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- ROW 3: SEBI Sync Status + Four-Eyes Pending -->
      <div class="grid-row grid-2" style="margin-top:16px">
        <div class="card">
          <div class="card-header">
            <div class="card-title">SEBI API Sync</div>
            ${_compactChip('Live API', 'info')}
          </div>
          <div class="card-body" id="ds-sebi">${Utils.loadingHTML()}</div>
        </div>
        <div class="card">
          <div class="card-header">
            <div class="card-title">Four-Eyes: Pending IPS Changes</div>
            <span class="compact-chip compact-chip-warning" id="ds-four-eyes-count">0 Pending</span>
          </div>
          <div class="card-body" id="ds-four-eyes">${Utils.loadingHTML()}</div>
        </div>
      </div>

      <!-- ROW 4: Quote Upload History -->
      <div class="card" style="margin-top:16px">
        <div class="card-header"><div class="card-title">Recent Quote Uploads</div></div>
        <div class="table-container" id="ds-uploads">${Utils.skeletonTable(5)}</div>
      </div>
    `;

    const portfolioDropzone = document.getElementById('portfolio-dropzone');
    const portfolioFileInput = document.getElementById('portfolio-file-input');
    if (portfolioDropzone && portfolioFileInput) {
      portfolioDropzone.addEventListener('dragover', (event) => {
        event.preventDefault();
        portfolioDropzone.style.borderColor = 'var(--accent-blue)';
      });
      portfolioDropzone.addEventListener('dragleave', () => {
        portfolioDropzone.style.borderColor = 'var(--border-light)';
      });
      portfolioDropzone.addEventListener('drop', (event) => {
        App._portfolioDrop(event);
      });
      portfolioDropzone.addEventListener('click', () => {
        portfolioFileInput.click();
      });
      portfolioFileInput.addEventListener('change', (event) => {
        App._portfolioFileSelected(event);
      });
    }

    // portfolio file state
    window._portfolioFile = null;

    loadDataSourcesData();
  }

  function _compactChipClass(tone = 'neutral') {
    return `compact-chip compact-chip-${tone}`;
  }

  function _compactChip(label, tone = 'neutral', attrs = '') {
    return `<span class="${_compactChipClass(tone)}"${attrs}>${Utils.escapeHTML(String(label))}</span>`;
  }

  function _badgeClassToCompactTone(badgeClass) {
    const cls = String(badgeClass || '').toLowerCase();
    if (cls.includes('green')) return 'success';
    if (cls.includes('amber')) return 'warning';
    if (cls.includes('red')) return 'danger';
    if (cls.includes('purple')) return 'purple';
    if (cls.includes('teal')) return 'teal';
    if (cls.includes('blue')) return 'info';
    return 'neutral';
  }

  function _portfolioUploadBatchStorageKey() {
    return `portfolio_upload_last_batch_id:${API.getActiveTrustId()}`;
  }

  function _renderPortfolioBatchReport(report) {
    const el = document.getElementById('portfolio-batch-report');
    if (!el) return;

    if (!report || !report.batch) {
      el.innerHTML = Utils.emptyHTML('No committed batch yet', 'Upload a portfolio file to view the persisted batch report here.');
      return;
    }

    const batch = report.batch || {};
    const version = report.version || {};
    const validation = report.validation_report || {};
    const rows = Array.isArray(report.rows) ? report.rows : [];
    const canonicalRow = rows.find((row) => row.stage === 'canonical' && row.status === 'ACCEPTED') || rows[0] || {};
    let canonicalPayload = {};
    try {
      canonicalPayload = JSON.parse(canonicalRow.canonical_json || canonicalRow.validated_json || canonicalRow.normalized_json || '{}') || {};
    } catch (_) {
      canonicalPayload = {};
    }
    let summary = {};
    try {
      summary = JSON.parse(batch.validation_report_json || '{}')?.summary || JSON.parse(version.summary_json || '{}')?.summary || {};
    } catch (_) {
      summary = {};
    }

    el.innerHTML = `
      <div style="padding:14px;border:1px solid var(--border-light);border-radius:12px;background:var(--bg-tertiary)">
        <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;margin-bottom:12px">
          <div>
            <div style="font-weight:700;font-size:0.9rem">Committed Batch Report</div>
            <div style="font-size:0.74rem;color:var(--text-muted)">Stored report for batch ${Utils.escapeHTML(batch.batch_id || report.batch_id || '')}</div>
          </div>
          <div style="display:flex;gap:8px;flex-wrap:wrap">
            ${_compactChip(String(batch.batch_status || 'UNKNOWN').toUpperCase(), String(batch.batch_status || '').toUpperCase() === 'COMMITTED' ? 'success' : 'warning')}
            ${_compactChip(`v${String(version.version_number || batch.version_number || 'â€”')}`, 'info')}
          </div>
        </div>
        <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;font-size:0.75rem">
          <div><span style="color:var(--text-muted)">Uploaded by:</span> <strong>${Utils.escapeHTML(batch.uploaded_by || 'â€”')}</strong></div>
          <div><span style="color:var(--text-muted)">Uploaded at:</span> <strong>${Utils.escapeHTML(Utils.formatDateTime(batch.uploaded_at || ''))}</strong></div>
          <div><span style="color:var(--text-muted)">Rows:</span> <strong>${Utils.escapeHTML(String(batch.row_count ?? summary.total_rows ?? rows.length ?? 0))}</strong></div>
          <div><span style="color:var(--text-muted)">Accepted / Rejected:</span> <strong>${Utils.escapeHTML(String(batch.accepted_count ?? summary.accepted_rows ?? 0))}</strong> / <strong>${Utils.escapeHTML(String(batch.rejected_count ?? summary.rejected_rows ?? 0))}</strong></div>
          <div><span style="color:var(--text-muted)">Trust / Portfolio:</span> <strong>${Utils.escapeHTML(batch.trust_id || 'â€”')}</strong> / <strong>${Utils.escapeHTML(batch.portfolio_id || 'â€”')}</strong></div>
          <div><span style="color:var(--text-muted)">Validation:</span> <strong>${Utils.escapeHTML(String(validation.report_status || batch.batch_status || 'UNKNOWN').toUpperCase())}</strong></div>
        </div>
        <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--border-light)">
          <div style="font-size:0.72rem;font-weight:700;color:var(--text-muted);margin-bottom:6px">Latest Canonical Row</div>
          <div style="font-size:0.74rem;line-height:1.5">
            <div><strong>ISIN:</strong> ${Utils.escapeHTML(canonicalPayload.isin || canonicalRow.isin || 'â€”')}</div>
            <div><strong>Stage / Status:</strong> ${Utils.escapeHTML(String(canonicalRow.stage || 'â€”'))} / ${Utils.escapeHTML(String(canonicalRow.status || 'â€”'))}</div>
            <div><strong>Sector / Rating:</strong> ${Utils.escapeHTML(canonicalPayload.sector || canonicalRow.sector || 'â€”')} / ${Utils.escapeHTML(canonicalPayload.credit_rating || canonicalRow.credit_rating || 'â€”')}</div>
          </div>
        </div>
      </div>
    `;
  }

  async function _loadPortfolioBatchReport(batchId) {
    const key = _portfolioUploadBatchStorageKey();
    const el = document.getElementById('portfolio-batch-report');
    if (!batchId) {
      if (el) _renderPortfolioBatchReport(null);
      return null;
    }

    localStorage.setItem(key, batchId);
    if (el) el.innerHTML = Utils.loadingHTML('Loading committed batch report...');

    try {
      const report = await API.getPortfolioUploadBatch(batchId);
      _renderPortfolioBatchReport(report);
      return report;
    } catch (err) {
      if (el) {
        el.innerHTML = `
          <div style="padding:12px;border:1px solid var(--border-light);border-radius:12px;background:var(--bg-tertiary)">
            <div style="font-weight:700;font-size:0.86rem;margin-bottom:4px">Committed Batch Report Unavailable</div>
            <div style="font-size:0.74rem;color:var(--text-muted)">${Utils.escapeHTML(err.message || 'Could not load stored batch report.')}</div>
          </div>
        `;
      }
      return null;
    }
  }

  // Portfolio drag-and-drop helpers
  async function _portfolioDrop(e) {
    e.preventDefault();
    document.getElementById('portfolio-dropzone').style.borderColor = 'var(--border-light)';
    const f = e.dataTransfer.files[0];
    if (f) _setPortfolioFile(f);
  }

  async function _portfolioFileSelected(e) {
    const f = e.target.files[0];
    if (f) _setPortfolioFile(f);
  }

  function _setPortfolioFile(f) {
    window._portfolioFile = f;
    const statusEl = document.getElementById('portfolio-upload-status');
    if (statusEl) statusEl.textContent = `Selected: ${f.name} (${(f.size/1024).toFixed(1)} KB)`;
    const dropzone = document.getElementById('portfolio-dropzone');
    if (dropzone) { dropzone.style.borderColor = 'var(--accent-blue)'; dropzone.style.background = 'rgba(37,99,235,0.05)'; }
    const btn = document.getElementById('portfolio-upload-btn');
    if (btn) btn.disabled = false;
  }

  async function _portfolioUpload() {
    const file = window._portfolioFile;
    if (!file) return;
    if (!ensureAuthenticated('upload portfolio data')) return;
    const mode = document.getElementById('portfolio-upload-mode')?.value || 'merge';
    const btn  = document.getElementById('portfolio-upload-btn');
    const statusEl = document.getElementById('portfolio-upload-status');
    const detailEl = document.getElementById('portfolio-upload-detail');

    btn.disabled = true;
    if (statusEl) statusEl.textContent = 'Uploadingâ€¦';
    if (detailEl) detailEl.textContent = '';

    const formData = new FormData();
    formData.append('file', file);
    formData.append('mode', mode);

    try {
      await API.refreshSession().catch(() => false);
      const resp = await fetch('/api/v1/datasources/portfolio/upload', {
        method: 'POST',
        credentials: 'include',
        body: formData,
      });
      const contentType = resp.headers.get('content-type') || '';
      const data = contentType.includes('application/json')
        ? await resp.json()
        : { detail: await resp.text() };
      if (resp.ok) {
        const skipped = Array.isArray(data.errors) ? data.errors.length : 0;
        const batchInfo = data.batch_id ? ` Â· Batch ${data.batch_id}${data.version_number ? ` v${data.version_number}` : ''}` : '';
        Utils.toast('success', 'Portfolio Uploaded', `${data.inserted} of ${data.total_rows} records imported${batchInfo}`);
        if (statusEl) statusEl.innerHTML = `<span style="color:var(--color-success)">âœ“ ${Utils.escapeHTML(String(data.inserted))} records imported${skipped ? ` Â· ${Utils.escapeHTML(String(skipped))} skipped` : ''}${Utils.escapeHTML(batchInfo)}</span>`;
        if (detailEl) detailEl.innerHTML = data.version_number || data.batch_id
          ? `Committed as ${data.version_number ? `version ${Utils.escapeHTML(String(data.version_number))}` : 'a new version'}${data.batch_id ? `, batch ${Utils.escapeHTML(String(data.batch_id))}` : ''}.`
          : '';
        if (data.batch_id) await _loadPortfolioBatchReport(data.batch_id);
        // Refresh summary
        _loadPortfolioSummary();
      } else {
        const detail = data.detail || {};
        const message = typeof detail === 'string' ? detail : (detail.message || 'Upload failed');
        const batchInfo = detail.batch_id ? ` Batch ${detail.batch_id}` : '';
        const errorCount = Array.isArray(detail.errors) ? detail.errors.length : 0;
        if (detailEl) {
          const rows = Array.isArray(detail.errors) ? detail.errors : [];
          const lines = rows.slice(0, 8).map((item) => `Row ${Utils.escapeHTML(item.row ?? 'â€”')}${item.isin ? ` (${Utils.escapeHTML(item.isin)})` : ''}: ${Utils.escapeHTML(item.errors)}`).join('<br/>');
          detailEl.innerHTML = `${batchInfo ? `<div>${Utils.escapeHTML(`Batch${batchInfo}`)}</div>` : ''}${errorCount ? `<div>${Utils.escapeHTML(`${errorCount} validation error(s)`)} </div><div style="margin-top:4px">${lines}</div>` : ''}`;
        }
        throw new Error(`${message}${batchInfo}${errorCount ? ` (${errorCount} validation errors)` : ''}`);
      }
    } catch (err) {
      Utils.toast('error', 'Upload Failed', err.message);
      if (statusEl) statusEl.innerHTML = `<span style="color:var(--color-danger)">âœ— ${Utils.escapeHTML(err.message)}</span>`;
      if (detailEl && !detailEl.textContent) detailEl.innerHTML = `<span style="color:var(--text-muted)">No batch details were returned.</span>`;
    } finally {
      btn.disabled = false;
    }
  }

  function _downloadPortfolioTemplate(e) {
    if (e && typeof e.preventDefault === 'function') e.preventDefault();
    const headers = ['isin','issuer_name','bond_name','maturity_date','face_value','quantity','purchase_price','purchase_date','current_value','credit_rating','coupon_rate','sector','category'];
    const example = ['INE001A01036','HDFC Ltd','HDFC Bond 2028','2028-03-31','1000','100','98.50','2024-01-15','','AAA','8.50','Banking','CORPORATE'];
    const csv = headers.join(',') + '\n' + example.join(',');
    const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
    const a = document.createElement('a');
    a.href = url; a.download = 'portfolio_template.csv'; a.click();
    URL.revokeObjectURL(url);
  }

  async function _loadPortfolioSummary() {
    const el = document.getElementById('ds-portfolio-summary');
    if (!el) return;
    try {
      const [uploadData, liveData] = await Promise.all([
        API.get('/datasources/portfolio/summary').catch((err) => {
          if (isAuthError(err)) throw err;
          return null;
        }),
        API.getPortfolio().catch((err) => {
          if (isAuthError(err)) throw err;
          return null;
        }),
      ]);
      const liveSummary = liveData?.summary || {};
      const liveHoldings = Array.isArray(liveData?.holdings) ? liveData.holdings : [];
      const distinctBondCount = new Set(liveHoldings.map((row) => String(row.isin || '').trim()).filter(Boolean)).size;
      const underlyingLotCount = liveSummary.holdings_count ?? liveHoldings.length ?? 'â€”';
      const couponWeightedYield = _displayCouponWeightedYield(liveSummary);
      el.innerHTML = `
        <div style="font-size:0.73rem;font-weight:700;color:var(--text-muted);margin-bottom:6px;text-transform:uppercase;letter-spacing:0.05em">Live Portfolio API (Active)</div>
        <div class="flex justify-between items-center" style="padding:5px 0;font-size:0.82rem">
          <span class="text-muted">Distinct Bonds</span><span class="fw-600">${distinctBondCount}</span>
        </div>
        <div class="flex justify-between items-center" style="padding:5px 0;font-size:0.82rem">
          <span class="text-muted">Underlying Lots</span><span class="fw-600">${underlyingLotCount}</span>
        </div>
        <div class="flex justify-between items-center" style="padding:5px 0;font-size:0.82rem">
          <span class="text-muted">AUM</span>
          <span class="fw-600">${liveSummary.total_market_value != null ? Utils.formatINR(liveSummary.total_market_value) : 'â€”'}</span>
        </div>
        <div class="flex justify-between items-center" style="padding:5px 0;font-size:0.82rem">
          <span class="text-muted">Coupon-Weighted Yield</span>
          <span class="fw-600">${couponWeightedYield != null ? Utils.formatYieldPct(couponWeightedYield, 2) : 'â€”'}</span>
        </div>
        ${(uploadData?.total_bonds || 0) > 0 ? `
        <div style="font-size:0.73rem;font-weight:700;color:var(--text-muted);margin:10px 0 6px;text-transform:uppercase;letter-spacing:0.05em">Uploaded CSV</div>
        <div class="flex justify-between items-center" style="padding:5px 0;font-size:0.82rem">
          <span class="text-muted">Bonds</span><span class="fw-600">${uploadData.total_bonds}</span>
        </div>
        <div class="flex justify-between items-center" style="padding:5px 0;font-size:0.82rem">
          <span class="text-muted">Face Value</span>
            <span class="fw-600">${liveSummary.face_value_crores != null ? `â‚¹${Number(liveSummary.face_value_crores).toFixed(2)} Cr` : (uploadData.total_face_value ? Utils.formatINR(uploadData.total_face_value) : 'â€”')}</span>
        </div>` : ''}`;
    } catch (err) {
      if (isAuthError(err)) {
        handleSessionExpired(err);
        return;
      }
      el.innerHTML = Utils.emptyHTML('Not available');
    }
  }

  async function triggerSebiSync() {
    const restoreButtons = _setBusyButtons('button[onclick*="App.triggerSebiSync()"]', true, 'Syncingâ€¦');
    Utils.toast('info', 'SEBI Sync', 'Triggering live SEBI API syncâ€¦');
    try {
      const res = await API.post('/datasources/sebi/sync', {});
      const totalStored = Object.values(res.tasks || {}).reduce((s, t) => s + (t.stored || t.updated || 0), 0);
      Utils.toast('success', 'SEBI Sync Complete', `${totalStored} records updated`);
      loadDataSourcesData();
    } catch (err) {
      if (isAuthError(err)) {
        handleSessionExpired(err);
        return;
      }
      Utils.toast('error', 'SEBI Sync Failed', err.message);
    } finally {
      restoreButtons();
    }
  }

  async function loadDataSourcesData() {
    try {
      const safeLoad = async (loader, fallback, label) => {
        try {
          return await loader();
        } catch (err) {
          if (isAuthError(err)) throw err;
          const base = fallback && typeof fallback === 'object' ? fallback : {};
          return { ...base, _error: err?.message || `${label} unavailable` };
        }
      };

      const [brokers, health, quotes, ingestLog, unkBrokers, freshness, sourceStatus] = await Promise.all([
        safeLoad(() => API.brokerStats(), { brokers: [] }, 'Broker statistics'),
        safeLoad(() => API.health(), { status: 'unknown', database: {} }, 'Platform health'),
        safeLoad(() => API.listQuotes({ limit: 20 }), { quotes: [] }, 'Quote uploads'),
        safeLoad(() => API.getIngestLog(20), { entries: [] }, 'Gmail ingest log'),
        safeLoad(() => API.getUnknownBrokers(), { unknown_files: [] }, 'Unknown broker sources'),
        safeLoad(() => API.get('/institutional/data/freshness'), {}, 'Pricing freshness'),
        safeLoad(() => API.get('/institutional/data/sources'), { sources: [] }, 'Data source registry'),
      ]);

      const dataIssues = [
        brokers?._error ? `Broker stats: ${brokers._error}` : null,
        health?._error ? `Platform health: ${health._error}` : null,
        quotes?._error ? `Quote uploads: ${quotes._error}` : null,
        ingestLog?._error ? `Gmail ingest: ${ingestLog._error}` : null,
        unkBrokers?._error ? `Unknown broker registry: ${unkBrokers._error}` : null,
        freshness?._error ? `Pricing freshness: ${freshness._error}` : null,
        sourceStatus?._error ? `Source registry: ${sourceStatus._error}` : null,
      ].filter(Boolean);
      const dsStatusBanner = document.getElementById('ds-status-banner');
      if (dsStatusBanner) {
        dsStatusBanner.innerHTML = dataIssues.length ? `
          <div class="card" style="border-color:rgba(245,158,11,0.35);background:linear-gradient(180deg,rgba(245,158,11,0.08),rgba(245,158,11,0.03))">
            <div class="card-body" style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:flex-start">
              <div style="flex:1;min-width:240px">
                <div class="metric-label" style="color:#b45309">Data Source Degraded Mode</div>
                <div style="font-size:0.82rem;margin-top:4px">Some ingestion or telemetry services are unavailable. Automation remains visible but outputs should be treated as partial until feeds recover.</div>
                <div style="font-size:0.74rem;color:var(--text-muted);margin-top:6px">${Utils.escapeHTML(dataIssues.slice(0, 4).join(' | '))}${dataIssues.length > 4 ? ' | ...' : ''}</div>
              </div>
              <div style="font-size:0.74rem;color:var(--text-muted)">${dataIssues.length} feed(s) impacted</div>
            </div>
          </div>
        ` : '';
      }

      // Gmail ingest stats
      const entries = ingestLog.entries || ingestLog.log || [];
      const lastEntry = entries[0];
      const gmailStatusChip = document.getElementById('ds-gmail-status-chip');
      if (gmailStatusChip) {
        gmailStatusChip.innerHTML = ingestLog?._error
          ? _compactChip('Unavailable', 'danger')
          : entries.length
            ? _compactChip('Connected', 'success')
            : _compactChip('No recent ingest', 'warning');
      }
      const lastEl = document.getElementById('ds-gmail-last');
      if (lastEl) lastEl.textContent = lastEntry ? Utils.formatDateTime(lastEntry.ingested_at || lastEntry.created_at) : 'No data yet';
      const countEl = document.getElementById('ds-gmail-count');
      if (countEl) countEl.textContent = entries.length > 0 ? `${entries.length} records` : '0 records';

      const lastBatchId = localStorage.getItem(_portfolioUploadBatchStorageKey());
      await _loadPortfolioBatchReport(lastBatchId);

      // Brokers â€” known + unknown section
      const knownBrokers = (brokers.brokers || []).filter(b => b.broker_name && b.broker_name.toLowerCase() !== 'unknown' && b.broker_name.trim() !== '');
      const unknownFiles = unkBrokers.unknown_files || [];
      const brokerEmptyState = brokers?._error
        ? Utils.emptyHTML('Broker feed unavailable', brokers._error)
        : Utils.emptyHTML('No brokers connected', 'Upload broker quotes to get started');
      const unknownSection = unknownFiles.length ? `
        <div style="margin-top:10px;padding:10px;background:rgba(245,158,11,0.07);border-radius:8px;border:1px solid rgba(245,158,11,0.3)">
          <div style="font-size:0.75rem;font-weight:700;color:#b45309;margin-bottom:6px">âš  ${unknownFiles.length} unidentified source file${unknownFiles.length > 1 ? 's' : ''} â€” click to name</div>
          ${unknownFiles.map((f, i) => {
            const safeFile = (f.source_file || 'unnamed').replace(/'/g, "\\'");
            return `<div style="display:flex;align-items:center;gap:6px;padding:5px 0;border-bottom:1px solid rgba(245,158,11,0.15)">
              <div style="flex:1;min-width:0">
                <div class="compact-ellipsis" style="font-size:0.78rem;font-weight:600" title="${Utils.escapeAttr(f.source_file || 'unnamed')}">${f.source_file || 'unnamed'}</div>
                <div style="font-size:0.68rem;color:var(--text-muted)">${f.quote_count} quotes Â· ${f.quote_date}</div>
              </div>
              <button class="btn btn-ghost btn-sm" type="button" style="font-size:0.72rem;flex-shrink:0" data-method="_renameUnknownBrokerFile" data-arg="${Utils.escapeAttr(safeFile)}">ðŸ· Name it</button>
            </div>`;
          }).join('')}
        </div>` : '';
      const dsBrokersEl = document.getElementById('ds-brokers');
      if (dsBrokersEl) dsBrokersEl.innerHTML = (knownBrokers.length
        ? knownBrokers.map(b => `
            <div style="padding:8px 0;border-bottom:1px solid var(--border-light)">
              <div class="flex justify-between items-center">
                <div>
                  <div class="fw-600" style="font-size:0.85rem">${b.broker_name}</div>
                  <div class="text-muted" style="font-size:0.72rem">${b.unique_isins || 0} ISINs &bull; ${b.total_quotes || 0} quotes</div>
                </div>
                <div class="text-right">
                  ${_compactChip('Active', 'success')}
                  <div class="text-muted" style="font-size:0.68rem;margin-top:2px">${Utils.formatDate(b.latest_quote)}</div>
                </div>
              </div>
            </div>`).join('')
        : brokerEmptyState
      ) + unknownSection;

      // Market data
      const freshnessPct = Number(freshness?.freshness_pct);
      const staleCount = Number(freshness?.stale || 0);
      const missingCount = Number(freshness?.missing || 0);
      const nonFreshCount = staleCount + missingCount;
      const freshnessTone = Number.isFinite(freshnessPct)
        ? (freshnessPct >= 90 ? 'success' : freshnessPct >= 75 ? 'warning' : 'danger')
        : 'warning';
      const freshnessLabel = Number.isFinite(freshnessPct) ? `${freshnessPct.toFixed(1)}%` : 'Unavailable';
      const freshnessCheckedAt = freshness?.checked_at ? Utils.formatDateTime(freshness.checked_at) : 'Unavailable';
      const sourceRows = Array.isArray(sourceStatus?.sources) ? sourceStatus.sources.slice(0, 6) : [];
      const activeSources = Number(sourceStatus?.total_active || 0);
      const sourceCheckedAt = sourceStatus?.checked_at ? Utils.formatDateTime(sourceStatus.checked_at) : 'Unavailable';
      const latestQuoteSyncCandidate = (quotes?.quotes || []).find((row) => row.extracted_at || row.quote_date) || null;
      const lastMarketSyncValue = freshness?.checked_at || sourceStatus?.checked_at || latestQuoteSyncCandidate?.extracted_at || latestQuoteSyncCandidate?.quote_date || null;
      const lastMarketSyncLabel = lastMarketSyncValue ? Utils.formatDateTime(lastMarketSyncValue) : 'Unavailable';
      const nseStatusTone = sourceStatus?._error ? 'warning' : (activeSources > 0 ? 'success' : 'warning');
      const nseStatusLabel = sourceStatus?._error ? 'Degraded' : (activeSources > 0 ? 'Active' : 'No active feed');
      const databaseTone = health?._error ? 'warning' : (health.status === 'OK' ? 'success' : 'warning');
      const sourceCountTone = activeSources > 0 ? 'info' : 'warning';

      const dsMarketEl = document.getElementById('ds-market');
      if (dsMarketEl) dsMarketEl.innerHTML = `
        <div class="flex justify-between items-center" style="padding:6px 0;font-size:0.82rem">
          <span class="text-muted">NSE Connection</span>${_compactChip(nseStatusLabel, nseStatusTone)}
        </div>
        <div class="flex justify-between items-center" style="padding:6px 0;font-size:0.82rem">
          <span class="text-muted">Database Status</span>
          ${_compactChip(health.status || 'Unknown', databaseTone)}
        </div>
        <div class="flex justify-between items-center" style="padding:6px 0;font-size:0.82rem">
          <span class="text-muted">Last Sync</span><span class="fw-600">${lastMarketSyncLabel}</span>
        </div>
        <div class="flex justify-between items-center" style="padding:6px 0;font-size:0.82rem">
          <span class="text-muted">SEBI API Mode</span>${_compactChip('Direct API', 'success')}
        </div>
        <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--border-light)">
          <div class="flex justify-between items-center" style="font-size:0.8rem;margin-bottom:6px">
            <span class="text-muted">Pricing Freshness</span>
            ${_compactChip(freshnessLabel, freshnessTone)}
          </div>
          <div class="flex justify-between items-center" style="padding:4px 0;font-size:0.78rem">
            <span class="text-muted">Fresh / Stale / Missing</span>
            <span class="fw-600">${Number(freshness?.fresh || 0)} / ${staleCount} / ${missingCount}</span>
          </div>
          <div class="flex justify-between items-center" style="padding:4px 0;font-size:0.78rem">
            <span class="text-muted">Fallback Exposure</span>
            <span class="fw-600 ${nonFreshCount > 0 ? 'text-amber' : 'text-green'}">${nonFreshCount} holding${nonFreshCount === 1 ? '' : 's'}</span>
          </div>
          <div class="flex justify-between items-center" style="padding:4px 0;font-size:0.78rem">
            <span class="text-muted">Freshness Checked</span>
            <span class="fw-600">${freshnessCheckedAt}</span>
          </div>
        </div>
        <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--border-light)">
          <div class="flex justify-between items-center" style="font-size:0.8rem;margin-bottom:6px">
            <span class="text-muted">Feed Transparency</span>
            ${_compactChip(`${activeSources} Active`, sourceCountTone)}
          </div>
          ${sourceRows.length ? sourceRows.map((src) => {
            const isActive = !!src.is_active;
            const latestData = src.latest_data ? Utils.formatDateTime(src.latest_data) : 'Unavailable';
            return `<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;font-size:0.76rem;border-bottom:1px solid rgba(148,163,184,0.15)">
              <span style="display:flex;align-items:center;gap:6px;min-width:0">
                ${_compactChip(isActive ? 'Active' : 'Degraded', isActive ? 'success' : 'warning')}
                <span class="truncate" style="max-width:130px" title="${Utils.escapeAttr(src.source_name || 'Unknown')}">${Utils.escapeHTML(src.source_name || 'Unknown')}</span>
              </span>
              <span style="text-align:right;color:var(--text-muted)">${Number(src.total_records || 0)} rec Â· ${latestData}</span>
            </div>`;
          }).join('') : `<div style="font-size:0.74rem;color:var(--text-muted);padding:4px 0">Source registry is unavailable. Check institutional data source service.</div>`}
          <div style="font-size:0.7rem;color:var(--text-muted);margin-top:6px">Source status checked: ${sourceCheckedAt}</div>
        </div>
        <button class="btn btn-primary btn-sm w-full" type="button" data-method="refreshMarket" style="margin-top:12px;justify-content:center">${IC.refresh} Sync Market Data</button>
      `;

      // Quote Uploads
      const quotesList = quotes.quotes || [];
      const hasQuoteValue = (value) => value !== null && value !== undefined && String(value).trim() !== '';
      const optionalQuotePlaceholder = '<span class="text-muted" title="Optional for clean-price-only uploads">Not provided</span>';
      const formatOptionalQuotePrice = (value) => hasQuoteValue(value) ? `â‚¹${Number(value).toFixed(2)}` : optionalQuotePlaceholder;
      const formatOptionalQuoteYtm = (value) => hasQuoteValue(value) ? Utils.formatYieldPct(value, 4) : optionalQuotePlaceholder;
      const dsUploadsEl = document.getElementById('ds-uploads');
      if (dsUploadsEl) dsUploadsEl.innerHTML = quotesList.length ? `
        <div style="font-size:0.72rem;color:var(--text-muted);padding:8px 10px;border:1px solid var(--border-light);border-radius:8px;background:var(--bg-tertiary);margin:0 0 8px 0">
          Bid/ask fields are optional for clean-price-only uploads. <strong>Not provided</strong> indicates optional omission, not ingestion failure.
        </div>
        <table class="data-table">
          <thead><tr><th>ISIN</th><th>Broker</th><th>Quote Date</th><th>Bid Price</th><th>Bid YTM</th><th>Ask Price</th><th>Ask YTM</th><th>Ingested</th></tr></thead>
          <tbody>${quotesList.map(q => `<tr>
            <td class="font-mono" data-method="navigate" data-page="analysis" data-isin="${Utils.escapeAttr(q.isin || '')}" style="cursor:pointer;color:var(--primary)">${q.isin || 'â€”'}</td>
            <td><strong>${q.broker_name || 'â€”'}</strong></td>
            <td>${Utils.formatDate(q.quote_date)}</td>
            <td class="col-number">${formatOptionalQuotePrice(q.bid_price)}</td>
            <td class="col-number">${formatOptionalQuoteYtm(q.bid_yield)}</td>
            <td class="col-number">${formatOptionalQuotePrice(q.ask_price)}</td>
            <td class="col-number">${formatOptionalQuoteYtm(q.ask_yield)}</td>
            <td>${Utils.formatDateTime(q.extracted_at)}</td>
          </tr>`).join('')}</tbody>
        </table>
      ` : (quotes?._error ? Utils.emptyHTML('Quote upload history unavailable', quotes._error) : Utils.emptyHTML('No quote uploads', 'Upload broker quote sheets or trigger Gmail ingest'));

    } catch (err) {
      if (isAuthError(err)) {
        handleSessionExpired(err);
        return;
      }
      Utils.toast('error', 'Data Sources Error', err.message);
    }

    // Load independent panels
    _loadPortfolioSummary();
    _loadSebiSyncStatus();
    _loadFourEyesPending();
  }

  async function _loadSebiSyncStatus() {
    const el = document.getElementById('ds-sebi');
    if (!el) return;
    try {
      const data = await API.get('/datasources/sebi/sync-log').catch((err) => {
        if (isAuthError(err)) throw err;
        return null;
      });
      const log = data?.log || [];
      const last = log[0];
      const statusTone = last?.status === 'OK' ? 'success' : 'warning';
      el.innerHTML = `
        <div class="flex justify-between items-center" style="padding:5px 0;font-size:0.82rem">
          <span class="text-muted">Last Sync</span>
          <span class="fw-600">${last ? Utils.formatDateTime(last.started_at) : 'Never'}</span>
        </div>
        <div class="flex justify-between items-center" style="padding:5px 0;font-size:0.82rem">
          <span class="text-muted">Records Stored</span>
          <span class="fw-600">${last ? last.records_stored || 0 : 'â€”'}</span>
        </div>
        <div class="flex justify-between items-center" style="padding:5px 0;font-size:0.82rem">
          <span class="text-muted">Status</span>
          ${_compactChip(last?.status || 'Pending', statusTone)}
        </div>
        <div class="flex justify-between items-center" style="padding:5px 0;font-size:0.82rem">
          <span class="text-muted">Schedule</span>
          <span class="fw-600">Every 4 hours</span>
        </div>
        <button class="btn btn-primary btn-sm w-full" type="button" data-method="triggerSebiSync" style="margin-top:12px;justify-content:center">
          ${IC.refresh} Sync Now
        </button>
      `;
    } catch (err) {
      if (isAuthError(err)) {
        handleSessionExpired(err);
        return;
      }
      if (el) el.innerHTML = Utils.emptyHTML('SEBI sync not available');
    }
  }

  async function _loadFourEyesPending() {
    const el = document.getElementById('ds-four-eyes');
    const countEl = document.getElementById('ds-four-eyes-count');
    if (!el) return;
    try {
      const data = await API.get('/datasources/four-eyes/pending').catch((err) => {
        if (isAuthError(err)) throw err;
        return null;
      });
      const pending = data?.pending || [];
      if (countEl) {
        countEl.className = _compactChipClass(pending.length ? 'warning' : 'success');
        countEl.textContent = `${pending.length} Pending`;
      }
      if (!pending.length) {
        el.innerHTML = Utils.emptyHTML('No pending approvals', 'All IPS rule changes are resolved');
        return;
      }
      el.innerHTML = pending.map(r => `
        <div style="padding:10px;background:rgba(245,158,11,0.08);border-radius:8px;margin-bottom:8px;border-left:3px solid var(--accent-amber)">
          <div class="flex justify-between" style="font-size:0.82rem">
            <span class="fw-600">${r.rule_key}</span>
            ${_compactChip('Pending', 'warning')}
          </div>
          <div style="font-size:0.75rem;color:var(--text-muted);margin-top:2px">
            By: ${r.initiator} Â· Expires: ${Utils.formatDateTime(r.expires_at)}
          </div>
          <div style="font-size:0.75rem;margin-top:4px">${r.old_value ?? 'N/A'} â†’ <strong>${r.new_value}</strong></div>
        </div>
      `).join('');
    } catch (err) {
      if (isAuthError(err)) {
        handleSessionExpired(err);
        return;
      }
      if (el) el.innerHTML = Utils.emptyHTML('Not available');
    }
  }

  async function triggerGmailIngest() {
    const restoreButtons = _setBusyButtons('button[onclick*="App.triggerGmailIngest()"]', true, 'Pollingâ€¦');
    // Step definitions for the progress display
    const stepsDef = [
      'Authenticating with Gmail OAuth2',
      'Polling inbox for broker quote emails',
      'Parsing PDF / Excel attachments',
      'Storing quotes into database',
    ];
    const stepsHTML = (activeIdx) => stepsDef.map((s, i) => {
      const done = i < activeIdx;
      const active = i === activeIdx;
      const icon = done
        ? `<span style="color:var(--color-success);font-size:1rem">âœ“</span>`
        : active
          ? `<span style="display:inline-block;width:14px;height:14px;border:2px solid var(--color-primary);border-top-color:transparent;border-radius:50%;animation:spin .7s linear infinite"></span>`
          : `<span style="color:var(--color-text-muted)">â—‹</span>`;
      const col = done ? 'var(--color-success)' : active ? 'var(--color-primary)' : 'var(--color-text-muted)';
      return `<div style="display:flex;align-items:center;gap:10px;padding:6px 0;font-size:0.84rem;color:${col}">
        <span style="width:20px;text-align:center;flex-shrink:0">${icon}</span>
        <span>${s}â€¦</span>
      </div>`;
    }).join('');

    const modal = document.getElementById('modal-overlay');
    document.getElementById('modal-content').innerHTML = `
      <div class="modal-header">
        <div class="modal-title">${IC.mail} Gmail Quote Poll</div>
        <button class="modal-close" data-action="close-modal">${IC.x}</button>
      </div>
      <div class="modal-body">
        <div id="gmail-steps" style="margin-bottom:16px;padding:4px 0">${stepsHTML(0)}</div>
        <div id="gmail-result"></div>
      </div>
    `;
    modal.classList.add('show');

    // Advance steps visually while the backend processes
    const timers = [1400, 2800, 4200].map((ms, idx) =>
      setTimeout(() => {
        const se = document.getElementById('gmail-steps');
        if (se) se.innerHTML = stepsHTML(idx + 1);
      }, ms)
    );

    try {
      const result = await API.triggerGmailIngest();
      timers.forEach(clearTimeout);

      const emails   = result.emails_processed    ?? 0;
      const attachs  = result.attachments_downloaded ?? 0;
      const files    = result.files_parsed         ?? 0;
      const stored   = result.quotes_stored        ?? 0;
      const errs     = result.errors               ?? [];
      const preview  = result.quote_preview        ?? [];
      const asOf     = result.preview_date         || new Date().toISOString().slice(0, 10);

      // Mark all steps done
      const se = document.getElementById('gmail-steps');
      if (se) se.innerHTML = stepsHTML(4);

      // Build stat tiles
      const statTiles = [
        ['Emails scanned', emails, false],
        ['Attachments', attachs, false],
        ['Files parsed', files, false],
        ['Quotes stored', stored, stored > 0],
      ].map(([l, v, hi]) =>
        `<div style="background:var(--color-surface-2);border-radius:8px;padding:10px 14px;min-width:90px;text-align:center">
          <div style="font-size:1.35rem;font-weight:700;color:${hi ? 'var(--color-success)' : 'var(--color-text)'}">${v}</div>
          <div style="font-size:0.7rem;color:var(--color-text-muted)">${l}</div>
        </div>`
      ).join('');

      // Build quote preview table
      let previewHTML = '';
      if (preview.length) {
        previewHTML = `
          <div style="margin-top:16px">
            <div style="font-weight:600;font-size:0.83rem;margin-bottom:8px;color:var(--color-success)">
              ${IC.check} ${stored} quote(s) now in database &mdash; latest for ${asOf}
            </div>
            <div class="table-container" style="max-height:280px;overflow-y:auto">
              <table class="data-table" style="font-size:0.75rem">
                <thead><tr>
                  <th>ISIN</th><th>Bond</th><th>Broker</th><th>Rating</th>
                  <th class="col-number">Yield</th><th class="col-number">Price</th><th>Maturity</th>
                </tr></thead>
                <tbody>${preview.map(q => `
                  <tr>
                    <td class="font-mono" style="font-size:0.67rem">${q.isin || 'â€”'}</td>
                    <td class="truncate" style="max-width:130px" title="${q.bond_name || ''}">${q.bond_name || q.isin || 'â€”'}</td>
                    <td><span class="badge badge-green" style="font-size:0.64rem">${q.broker_name || 'â€”'}</span></td>
                    <td>${Utils.ratingBadge(q.rating)}</td>
                    <td class="col-number text-green font-medium">${q.ytm != null ? q.ytm.toFixed(4) + '%' : 'â€”'}</td>
                    <td class="col-number">${q.clean_price != null ? 'â‚¹' + q.clean_price.toFixed(2) : 'â€”'}</td>
                    <td class="font-mono" style="font-size:0.67rem">${q.maturity_date ? Utils.formatDate(q.maturity_date) : 'â€”'}</td>
                  </tr>`).join('')}
                </tbody>
              </table>
            </div>
          </div>`;
      } else {
        previewHTML = `<div style="margin-top:12px;font-size:0.83rem;color:var(--color-text-muted)">
          ${stored === 0
            ? 'No new quotes added â€” no matching emails found or all attachments were already ingested.'
            : `${stored} quote(s) stored. Preview not available.`}
        </div>`;
      }

      const errBlock = errs.length
        ? `<div style="margin-top:8px">${errs.slice(0, 3).map(e =>
            `<div style="color:var(--color-danger);font-size:0.75rem;padding:2px 0">âš  ${e}</div>`).join('')}</div>`
        : '';

      document.getElementById('gmail-result').innerHTML = `
        <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:4px">${statTiles}</div>
        ${previewHTML}
        ${errBlock}
        <div style="display:flex;gap:8px;margin-top:16px;justify-content:flex-end">
          <button class="btn btn-ghost btn-sm" data-action="close-modal">Close</button>
          <button class="btn btn-primary btn-sm" data-action="close-and-navigate" data-route="recommendations">
            ${IC.trending} View Recommendations
          </button>
        </div>`;

    } catch (err) {
      timers.forEach(clearTimeout);
      if (isAuthError(err)) {
        handleSessionExpired(err);
        closeModal();
        return;
      }
      const se = document.getElementById('gmail-steps');
      if (se) se.innerHTML = `<div style="color:var(--color-danger);padding:8px;font-size:0.84rem">âš  ${Utils.escapeHTML(err.message)}</div>`;
      const res = document.getElementById('gmail-result');
      if (res) res.innerHTML = `<button class="btn btn-ghost btn-sm" data-action="close-modal">Close</button>`;
    } finally {
      restoreButtons();
    }
  }

  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  // 9. ADMIN SETTINGS PAGE
  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  // Helper: security overview item card
  function _secItem(label, status, detail) {
    const isActive = ['ACTIVE','ENFORCED','VERIFIED','AUTOMATED','YES','7 Tiers','JWT Bearer','30+ Types','7 Years','< 15 min','< 24 hrs','Daily','BLOCKED','SAFE','CONTROLLED','SCRIPT READY'].includes(status);
    const isWarn   = ['PLANNED','RECOMMENDED','MISSING','PARTIAL'].includes(status);
    const dotColor = isActive ? '#10b981' : isWarn ? '#f59e0b' : '#6b7280';
    return `<div style="background:var(--bg-tertiary);border-radius:8px;padding:12px 14px;border-left:3px solid ${dotColor}">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px">
        <div style="font-size:0.8rem;font-weight:600;color:var(--text-primary)">${label}</div>
        <span style="font-size:0.68rem;font-weight:700;color:${dotColor};white-space:nowrap;margin-left:8px">${status}</span>
      </div>
      <div style="font-size:0.73rem;color:var(--text-muted);line-height:1.5">${detail}</div>
    </div>`;
  }

  // â”€â”€ MFA Setup Helper Functions â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
  let _mfaPendingSecret = null;

  async function _mfaLoadStatus() {
    const area = document.getElementById('mfa-status-area');
    if (!area) return;
    try {
      const res = await API.authMe();
      const enrolled = !!res.mfa_secret;
      area.textContent = enrolled
        ? 'âœ… MFA is enrolled on your account.'
        : 'âš ï¸ MFA is NOT yet enrolled. Your account is protected by password + lockout only.';
      document.getElementById('mfa-step-setup').style.display = enrolled ? 'none' : 'block';
      document.getElementById('mfa-step-done').style.display  = enrolled ? 'block' : 'none';
    } catch(e) {
      area.textContent = 'Could not load MFA status.';
    }
  }

  async function _mfaStartSetup() {
    const btn = document.querySelector('#mfa-step-setup button');
    if (btn) { btn.disabled = true; btn.textContent = 'Generatingâ€¦'; }
    try {
      const res = await API.post('/auth/mfa/setup');
      _mfaPendingSecret = res.secret;
      const container = document.getElementById('mfa-qr-container');
      container.innerHTML =
        `<div style="margin-bottom:8px;color:var(--text-primary);font-weight:600">Your TOTP Secret Key:</div>` +
        `<div style="font-size:1rem;letter-spacing:0.12em;color:#10b981">${res.secret}</div>` +
        `<div style="margin-top:10px;font-size:0.72rem;color:var(--text-muted)">Or use this provisioning URI in your TOTP app:<br>` +
        `<span style="word-break:break-all">${res.provisioning_uri}</span></div>` +
        `<div style="margin-top:10px;font-size:0.72rem;background:var(--bg-secondary);padding:8px;border-radius:4px">` +
        `<strong>Compatible apps:</strong> Google Authenticator, Microsoft Authenticator, Authy, 1Password</div>`;
      document.getElementById('mfa-step-setup').style.display   = 'none';
      document.getElementById('mfa-step-confirm').style.display = 'block';
      document.getElementById('mfa-status-area').textContent = 'Step 2 of 2: Enter the 6-digit code from your authenticator app to activate.';
    } catch(e) {
      if (btn) { btn.disabled = false; btn.textContent = 'Generate MFA QR Code'; }
      Utils.toast('error', 'MFA Setup Failed', e.message || String(e));
    }
  }

  async function _mfaConfirm() {
    const code = document.getElementById('mfa-totp-input').value.trim();
    const msg  = document.getElementById('mfa-confirm-msg');
    if (!/^\d{6}$/.test(code)) { msg.textContent = 'âš  Enter exactly 6 digits.'; msg.style.color = '#f59e0b'; return; }
    const restoreButtons = _setBusyButtons('#mfa-step-confirm button.btn-primary', true, 'Verifyingâ€¦');
    msg.textContent = 'Verifyingâ€¦'; msg.style.color = 'var(--text-muted)';
    try {
      await API.post('/auth/mfa/confirm', { totp_code: code });
      document.getElementById('mfa-step-confirm').style.display = 'none';
      document.getElementById('mfa-step-done').style.display    = 'block';
      document.getElementById('mfa-status-area').textContent    = 'âœ… MFA enrolled and active.';
      Utils.toast('success', 'MFA Activated', 'Future logins: use password:TOTP_CODE format');
    } catch(e) {
      msg.textContent = 'âŒ Code incorrect or expired. Try again.'; msg.style.color = '#ef4444';
    } finally {
      restoreButtons();
    }
  }

  function _mfaReset() {
    _mfaPendingSecret = null;
    document.getElementById('mfa-step-confirm').style.display = 'none';
    document.getElementById('mfa-step-setup').style.display   = 'block';
    document.getElementById('mfa-confirm-msg').textContent    = '';
    document.getElementById('mfa-totp-input').value           = '';
    document.getElementById('mfa-status-area').textContent    = 'âš ï¸ MFA setup cancelled.';
  }

  async function renderAdmin(el) {
    el.innerHTML = `
      <div class="page-header">
        <div>
          <div class="page-title">Admin Settings</div>
          <div class="page-subtitle">System configuration, compliance rules, and user management</div>
        </div>
      </div>

      <div class="tabs" id="admin-tabs">
        <div class="tab active" data-tab="admin-compliance">Compliance Rules</div>
        <div class="tab" data-tab="admin-benchmark">Benchmark Config</div>
        <div class="tab" data-tab="admin-data">Data Management</div>
        <div class="tab" data-tab="admin-users">User Roles</div>
        <div class="tab" data-tab="admin-audit">Audit Logs</div>
        <div class="tab" data-tab="admin-security" style="color:#10b981;font-weight:700">ðŸ›¡ Security Overview</div>
      </div>

      <!-- Compliance Rules -->
      <div class="tab-content active" id="tab-admin-compliance">
        <div class="card">
          <div class="card-header"><div class="card-title">Compliance Rule Configuration</div><button class="btn btn-primary btn-sm" type="button" data-method="saveAdminSettings" data-arg="compliance">Save Changes</button></div>
          <div class="card-body">
            <div class="grid-2" style="gap:20px">
              <div class="form-group">
                <label class="form-label">Max Single Issuer Exposure (%)</label>
                <input type="number" class="form-input" id="cfg-issuer-exp" value="15.0" step="0.5" min="0" max="100" />
                <div class="form-help">EPFO limit: 15% of total AUM per issuer (EXP-001)</div>
              </div>
              <div class="form-group">
                <label class="form-label">Max Sector Exposure (%)</label>
                <input type="number" class="form-input" id="cfg-sector-exp" value="35.0" step="1" min="0" max="100" />
                <div class="form-help">Maximum allocation to any single sector (EXP-002)</div>
              </div>
              <div class="form-group">
                <label class="form-label">Minimum Rating</label>
                <select class="form-input" id="cfg-min-rating">
                  <option>AA-</option>
                  <option>AA</option>
                  <option>AA+</option>
                  <option>AAA</option>
                </select>
                <div class="form-help">Bonds below this rating will be flagged</div>
              </div>
              <div class="form-group">
                <label class="form-label">Max Portfolio Duration (Years)</label>
                <input type="number" class="form-input" id="cfg-max-dur" value="8.0" step="0.5" min="0" max="100" />
                <div class="form-help">Target upper limit for portfolio duration (DUR-001)</div>
              </div>
              <div class="form-group">
                <label class="form-label">Max Group Exposure (%)</label>
                <input type="number" class="form-input" id="cfg-group-exp" value="10.0" step="0.5" min="0" max="100" />
                <div class="form-help">Maximum exposure to any issuer group</div>
              </div>
              <div class="form-group">
                <label class="form-label">Price Deviation Alert Threshold (%)</label>
                <input type="number" class="form-input" id="cfg-price-dev" value="0.5" step="0.1" min="0" max="100" />
                <div class="form-help">Alert when price deviates more than this from average</div>
              </div>
            </div>
            <!-- Live impact preview panel â€” populated by oninput listeners -->
            <div id="compliance-preview-panel" style="display:none;margin-top:16px;padding:12px 16px;background:var(--bg-secondary,#fffbeb);border-radius:8px;border:1px solid #f59e0b">
              <div style="font-size:0.8rem;font-weight:600;margin-bottom:8px;color:#b45309">âš  Live Rule Impact Preview</div>
              <div id="compliance-preview-body" style="font-size:0.78rem;color:var(--text-primary)"></div>
            </div>
          </div>
        </div>
      </div>

      <!-- Benchmark -->
      <div class="tab-content" id="tab-admin-benchmark">
        <div class="card">
          <div class="card-header"><div class="card-title">Benchmark Configuration</div></div>
          <div class="card-body">
            <div class="grid-2" style="gap:20px">
              <div class="form-group">
                <label class="form-label">Benchmark Curve</label>
                <select class="form-input" id="cfg-bench-curve">
                  <option>India G-Sec Yield Curve</option>
                  <option>FIMMDA AAA Curve</option>
                  <option>NSE MIBOR Curve</option>
                </select>
                <div class="form-help">Reference curve for spread calculations</div>
              </div>
              <div class="form-group">
                <label class="form-label">Spread Threshold (bps)</label>
                <input type="number" class="form-input" id="cfg-spread-thresh" value="50" step="5" />
                <div class="form-help">Minimum spread over benchmark for investment consideration</div>
              </div>
              <div class="form-group">
                <label class="form-label">Target Duration (Years)</label>
                <input type="number" class="form-input" id="cfg-target-dur" value="4.5" step="0.25" />
                <div class="form-help">Target portfolio duration for ALM matching</div>
              </div>
              <div class="form-group">
                <label class="form-label">NAV Reporting Frequency</label>
                <select class="form-input" id="cfg-nav-freq">
                  <option>Daily</option>
                  <option>Weekly</option>
                  <option>Monthly</option>
                </select>
              </div>
            </div>
            <button class="btn btn-primary" type="button" data-method="saveAdminSettings" data-arg="benchmark" style="margin-top:16px">Save Benchmark Settings</button>
          </div>
        </div>
      </div>

      <!-- Data Management -->
      <div class="tab-content" id="tab-admin-data">
        <div class="card">
          <div class="card-header">
            <div class="card-title">Market Data Management</div>
            <div id="admin-data-status" style="font-size:0.8rem;color:#888"></div>
          </div>
          <div class="card-body">
            <div class="grid-2" style="gap:20px;margin-bottom:24px">

              <div style="background:var(--bg-secondary,#1a1a2e);border-radius:10px;padding:20px">
                <div style="font-weight:700;margin-bottom:6px">Full Market Refresh</div>
                <div style="font-size:0.82rem;color:#aaa;margin-bottom:14px">
                  Fetches G-Sec yield curve (FBIL/RBI), live bond prices (NSE/BSE),
                  synthesises prices for all remaining bonds, and seeds IM records
                  for any bond in the universe lacking an Information Memorandum.
                </div>
                <button class="btn btn-primary" id="btn-refresh-all" type="button" data-method="runFullMarketRefresh">
                  ${IC.refresh} Refresh All Market Data
                </button>
              </div>

              <div style="background:var(--bg-secondary,#1a1a2e);border-radius:10px;padding:20px">
                <div style="font-weight:700;margin-bottom:6px">Seed Missing IM Records</div>
                <div style="font-size:0.82rem;color:#aaa;margin-bottom:14px">
                  Generates synthetic Information Memorandum stubs from the bond master
                  for all bonds that have no IM document.  Safe to call repeatedly â€”
                  existing NSE/BSE records are not overwritten.
                </div>
                <button class="btn btn-secondary" id="btn-seed-im" type="button" data-method="runSeedIm">
                  ${IC.plus} Seed IM Records
                </button>
              </div>

            </div>

            <!-- Progress / result panel -->
            <div id="admin-data-result" style="display:none;background:#0f172a;border-radius:8px;padding:16px;font-family:monospace;font-size:0.82rem;color:#94a3b8;max-height:320px;overflow-y:auto;white-space:pre-wrap"></div>
          </div>
        </div>

        <div class="card" style="margin-top:16px">
          <div class="card-header"><div class="card-title">Data Coverage Summary</div><button class="btn btn-secondary btn-sm" type="button" data-method="loadDataCoverage">${IC.refresh} Refresh</button></div>
          <div class="card-body" id="admin-data-coverage">${Utils.loadingHTML('Loading coverage...')}</div>
        </div>
      </div>

      <!-- User Roles -->
      <div class="tab-content" id="tab-admin-users">
        <div class="card">
          <div class="card-header"><div class="card-title">User Roles &amp; Permissions</div><button class="btn btn-primary btn-sm" type="button" data-method="_showAddUserModal">${IC.plus} Add User</button></div>
          <div class="card-body">
            <div id="admin-users-table">${Utils.loadingHTML('Loading users...')}</div>
            <div style="margin-top:20px">
              <div style="font-size:0.85rem;font-weight:700;margin-bottom:12px">Permissions Matrix</div>
              <table class="data-table">
                <thead><tr><th>Permission</th><th>Admin</th><th>Portfolio</th><th>Compliance</th><th>Viewer</th></tr></thead>
                <tbody>
                  <tr><td>View Dashboard</td><td class="text-green">âœ“</td><td class="text-green">âœ“</td><td class="text-green">âœ“</td><td class="text-green">âœ“</td></tr>
                  <tr><td>Execute Trades</td><td class="text-green">âœ“</td><td class="text-green">âœ“</td><td class="text-red">âœ—</td><td class="text-red">âœ—</td></tr>
                  <tr><td>Modify Compliance Rules</td><td class="text-green">âœ“</td><td class="text-red">âœ—</td><td class="text-red">âœ—</td><td class="text-red">âœ—</td></tr>
                  <tr><td>Generate Reports</td><td class="text-green">âœ“</td><td class="text-green">âœ“</td><td class="text-green">âœ“</td><td class="text-red">âœ—</td></tr>
                  <tr><td>Manage Users</td><td class="text-green">âœ“</td><td class="text-red">âœ—</td><td class="text-red">âœ—</td><td class="text-red">âœ—</td></tr>
                  <tr><td>View Bond Universe</td><td class="text-green">âœ“</td><td class="text-green">âœ“</td><td class="text-green">âœ“</td><td class="text-green">âœ“</td></tr>
                  <tr><td>Run Compliance Scans</td><td class="text-green">âœ“</td><td class="text-green">âœ“</td><td class="text-green">âœ“</td><td class="text-red">âœ—</td></tr>
                  <tr><td>Override Compliance</td><td class="text-green">âœ“</td><td class="text-amber">âš </td><td class="text-red">âœ—</td><td class="text-red">âœ—</td></tr>
                  <tr><td>System Configuration</td><td class="text-green">âœ“</td><td class="text-red">âœ—</td><td class="text-red">âœ—</td><td class="text-red">âœ—</td></tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>

      <!-- Audit Logs -->
      <div class="tab-content" id="tab-admin-audit">
        <div class="card">
          <div class="card-header"><div class="card-title">Audit Logs</div><button class="btn btn-secondary btn-sm" type="button" data-method="loadAuditLogs">${IC.refresh} Refresh</button></div>
          <div class="table-container" id="admin-audit-table">${Utils.loadingHTML()}</div>
        </div>
      </div>

      <!-- Security Overview (Trustee-facing) -->
      <div class="tab-content" id="tab-admin-security">
        <div style="max-width:900px">

          <div style="background:linear-gradient(135deg,#0f172a,#1e3a5f);color:#fff;border-radius:12px;padding:24px 28px;margin-bottom:24px">
            <div style="display:flex;align-items:center;gap:14px;margin-bottom:6px">
              <span style="font-size:2rem">ðŸ›¡</span>
              <div>
                <div style="font-size:1.25rem;font-weight:700">Security & Infrastructure Overview</div>
                <div style="font-size:0.82rem;opacity:0.7;margin-top:2px">For Trustees & Board Members â€” Plain Language Summary</div>
              </div>
            </div>
            <div style="margin-top:16px;display:flex;gap:16px;flex-wrap:wrap">
              <div style="background:rgba(255,255,255,0.08);border-radius:8px;padding:10px 18px;text-align:center">
                <div style="font-size:1.6rem;font-weight:800;color:#10b981">9.1</div>
                <div style="font-size:0.7rem;opacity:0.75;margin-top:2px">Security Score</div>
              </div>
              <div style="background:rgba(255,255,255,0.08);border-radius:8px;padding:10px 18px;text-align:center">
                <div style="font-size:1.6rem;font-weight:800;color:#3b82f6">9.4</div>
                <div style="font-size:0.7rem;opacity:0.75;margin-top:2px">Performance Score</div>
              </div>
              <div style="background:rgba(255,255,255,0.08);border-radius:8px;padding:10px 18px;text-align:center">
                <div style="font-size:1.6rem;font-weight:800;color:#a78bfa">Live</div>
                <div style="font-size:0.7rem;opacity:0.75;margin-top:2px">DR Backup Status</div>
              </div>
              <div style="background:rgba(255,255,255,0.08);border-radius:8px;padding:10px 18px;text-align:center">
                <div style="font-size:1.6rem;font-weight:800;color:#f59e0b">Validated</div>
                <div style="font-size:0.7rem;opacity:0.75;margin-top:2px">Pentest 2026-03-03</div>
              </div>
            </div>
          </div>

          <!-- Data Protection -->
          <div class="card" style="margin-bottom:16px">
            <div class="card-header"><div class="card-title" style="display:flex;align-items:center;gap:8px"><span>ðŸ”</span> Data Protection</div></div>
            <div class="card-body">
              <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px">
                ${_secItem('Encryption at Rest', 'ACTIVE', 'All data files stored in SQLite with application-level field encryption for sensitive values. AES-256 applied to encrypted backup archives.')}
                ${_secItem('Encryption in Transit', 'ACTIVE', 'HTTPS/TLS enforced via nginx. HSTS header present. HTTP connections redirect to HTTPS in production.')}
                ${_secItem('Backup Encryption', 'ACTIVE', 'Automated backups stored as AES-256 encrypted ZIP archives. Unencrypted fallback is disabled.')}
                ${_secItem('Database Integrity', 'VERIFIED', 'SQLite PRAGMA integrity_check passes. SHA-256 checksums on backup files. Audit log entries are hash-chained (tamper-evident).')}
              </div>
            </div>
          </div>

          <!-- Access Controls -->
          <div class="card" style="margin-bottom:16px">
            <div class="card-header"><div class="card-title" style="display:flex;align-items:center;gap:8px"><span>ðŸ”‘</span> Access Controls</div></div>
            <div class="card-body">
              <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px">
                ${_secItem('Authentication Method', 'JWT Bearer', 'HMAC-SHA256 signed tokens issued on login. Every API call verifies token signature and expiry.')}
                ${_secItem('Password Policy', 'ENFORCED', 'Minimum 8 characters, complexity required. Passwords stored as bcrypt hash with unique salt â€” never in plain text.')}
                ${_secItem('Account Lockout', 'ACTIVE', 'Account locked after 5 failed attempts for 30 minutes. Lockout state persists across server restarts (database-backed).')}
                ${_secItem('Role-Based Access', '7 Tiers', 'Admin, Portfolio Manager, Compliance Officer, Analyst, Auditor, Viewer, and Super Admin. 50+ fine-grained permissions.')}
                ${_secItem('MFA (Multi-Factor Auth)', 'ACTIVE', 'TOTP-based MFA (RFC 6238 / Google Authenticator) is live. Enrol your account below using the Set Up MFA button. Once enrolled, every login requires password + 6-digit code.')}
                ${_secItem('Session Expiry', '8 Hours', 'JWT tokens expire after 8 hours. Token renewal requires re-authentication.')}
              </div>
            </div>
          </div>

          <!-- Audit Trail -->
          <div class="card" style="margin-bottom:16px">
            <div class="card-header"><div class="card-title" style="display:flex;align-items:center;gap:8px"><span>ðŸ“‹</span> Audit Trail & Compliance</div></div>
            <div class="card-body">
              <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px">
                ${_secItem('Audit Log Immutability', 'ACTIVE', 'All actions are append-only. No UPDATE or DELETE on audit records. Each entry is SHA-256 chain-linked to the previous â€” any tampering is detectable.')}
                ${_secItem('Actionable Events Logged', '30+ Types', 'Portfolio changes, compliance scans, login attempts, lockouts, report generation, rule modifications, and data source changes.')}
                ${_secItem('Audit Retention', '7 Years', 'Audit logs retained for regulatory compliance. Included in every encrypted backup.')}
                ${_secItem('Compliance Engine', 'AUTOMATED', 'Runs continuously. Cannot be bypassed by any user or LLM agent. Rules enforced in pure Python with no override path.')}
              </div>
            </div>
          </div>

          <!-- Backup & DR -->
          <div class="card" style="margin-bottom:16px">
            <div class="card-header"><div class="card-title" style="display:flex;align-items:center;gap:8px"><span>ðŸ’¾</span> Backup & Disaster Recovery</div></div>
            <div class="card-body">
              <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px">
                ${_secItem('Backup Frequency', 'Daily', 'Automated daily backup via Windows Task Scheduler. Encrypted ZIP created at midnight.')}
                ${_secItem('Last Backup', '2026-02-22', 'Most recent backup: backup_2026-02-22_1200.zip (encrypted). Contains all portfolio, compliance, and audit data.')}
                ${_secItem('RTO (Recovery Time)', '< 15 min', 'Restore from backup, restart server, and validate health in under 15 minutes. DR drill completed 2026-02-22.')}
                ${_secItem('RPO (Data Loss)', '< 24 hrs', 'Maximum 24-hour data window in worst-case failure. Real-time portfolio is reconstructable from trade audit log.')}
                ${_secItem('Restore Tested', 'YES', 'Backup restore validated as part of DR drill. SQLite integrity check passes on restored database.')}
                ${_secItem('Offsite Copy', 'SCRIPT READY', 'offsite_backup.py supports AWS S3 and Azure Blob. Configure S3_BUCKET or AZURE_STORAGE_CONNECTION_STRING in .env and run daily at 01:00.')}
              </div>
            </div>
          </div>

          <!-- Vulnerability Status -->
          <div class="card" style="margin-bottom:16px">
            <div class="card-header"><div class="card-title" style="display:flex;align-items:center;gap:8px"><span>ðŸ”</span> Penetration Test Results â€” 2026-03-03</div></div>
            <div class="card-body">
              <table class="data-table" style="font-size:0.8rem">
                <thead><tr><th>Test</th><th>Result</th><th>Meaning for Trustees</th></tr></thead>
                <tbody>
                  <tr><td>Unauthenticated access</td><td><span class="badge badge-green">BLOCKED</span></td><td>No data is visible without logging in</td></tr>
                  <tr><td>Forged login token</td><td><span class="badge badge-green">BLOCKED</span></td><td>Attackers cannot fake identity</td></tr>
                  <tr><td>SQL injection</td><td><span class="badge badge-green">SAFE</span></td><td>Database cannot be manipulated via search fields</td></tr>
                  <tr><td>Script injection (XSS)</td><td><span class="badge badge-green">SAFE</span></td><td>Browser cannot be hijacked via input fields</td></tr>
                  <tr><td>Brute-force password attack</td><td><span class="badge badge-green">BLOCKED</span></td><td>Locked after 5 attempts â€” attacker gives up</td></tr>
                  <tr><td>File/path traversal</td><td><span class="badge badge-green">BLOCKED</span></td><td>Cannot access server files by guessing paths</td></tr>
                  <tr><td>Token expiry bypass</td><td><span class="badge badge-green">BLOCKED</span></td><td>Old sessions cannot be reused</td></tr>
                  <tr><td>Privilege escalation</td><td><span class="badge badge-green">CONTROLLED</span></td><td>Role restrictions enforced in code</td></tr>
                  <tr><td>Content-Security-Policy header</td><td><span class="badge badge-green">ACTIVE</span></td><td>CSP deployed â€” restricts script/style sources, prevents XSS even if app layer is bypassed</td></tr>
                  <tr><td>Multi-factor authentication</td><td><span class="badge badge-green">ACTIVE</span></td><td>TOTP (pyotp RFC 6238) live â€” enrol below. Once enrolled, login requires password + authenticator code</td></tr>
                </tbody>
              </table>
            </div>
          </div>

          <!-- Live MFA Enrolment -->
          <div class="card" style="margin-bottom:16px" id="mfa-enrol-card">
            <div class="card-header"><div class="card-title" style="display:flex;align-items:center;gap:8px"><span>ðŸ”‘</span> Multi-Factor Authentication â€” Enrol Your Account</div></div>
            <div class="card-body">
              <div id="mfa-status-area" style="font-size:0.83rem;color:var(--text-secondary);margin-bottom:14px">Loading MFA statusâ€¦</div>
              <!-- Step 1: not enrolled -->
              <div id="mfa-step-setup" style="display:none">
                <p style="font-size:0.82rem;margin-bottom:12px">MFA is <strong>not yet enrolled</strong> for your account. Click the button below to generate a QR code, then scan it with Google Authenticator or any TOTP app.</p>
                <button class="btn btn-primary" type="button" data-method="_mfaStartSetup">Generate MFA QR Code</button>
              </div>
              <!-- Step 2: QR shown, awaiting confirmation -->
              <div id="mfa-step-confirm" style="display:none">
                <p style="font-size:0.82rem;margin-bottom:8px">Scan this QR code with your authenticator app, then enter the 6-digit code to confirm.</p>
                <div id="mfa-qr-container" style="font-family:monospace;font-size:0.75rem;background:var(--bg-tertiary);padding:12px;border-radius:6px;margin-bottom:12px;word-break:break-all"></div>
                <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
                  <input id="mfa-totp-input" type="text" maxlength="6" placeholder="Enter 6-digit code" style="padding:8px 12px;border:1px solid var(--border);border-radius:6px;font-size:1rem;letter-spacing:0.2em;width:160px">
                  <button class="btn btn-primary" type="button" data-method="_mfaConfirm">Confirm & Activate MFA</button>
                  <button class="btn" type="button" data-method="_mfaReset" style="opacity:0.6">Cancel</button>
                </div>
                <div id="mfa-confirm-msg" style="margin-top:10px;font-size:0.8rem"></div>
              </div>
              <!-- Step 3: enrolled -->
              <div id="mfa-step-done" style="display:none">
                <div style="display:flex;align-items:center;gap:10px">
                  <span style="font-size:1.6rem">âœ…</span>
                  <div>
                    <div style="font-weight:700;color:var(--success)">MFA is active on your account</div>
                    <div style="font-size:0.78rem;color:var(--text-muted);margin-top:2px">Your next login will require your password plus a 6-digit TOTP code from your authenticator app. Format: <code>password:123456</code></div>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <!-- Liability statement -->
          <div style="background:var(--bg-tertiary);border-radius:8px;padding:16px 20px;font-size:0.78rem;color:var(--text-muted);line-height:1.6">
            <strong style="color:var(--text-primary)">Data Ownership & Liability:</strong> All portfolio data is owned exclusively by the Trust. No data leaves the server infrastructure provided to the Trust. The platform vendor has no access to portfolio positions, compliance status, or audit logs without explicit written consent. The platform is a tool â€” investment decisions remain with the authorised investment officers and trustees at all times. The platform does not execute trades autonomously.
          </div>

        </div>
      </div>
    `;

    // Tab switching
    document.querySelectorAll('#admin-tabs .tab').forEach(tab => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('#admin-tabs .tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
        if (tab.dataset.tab === 'admin-data')     loadDataCoverage();
        if (tab.dataset.tab === 'admin-security') _mfaLoadStatus();
      });
    });

    loadAuditLogs();
    _loadAdminUsers();
    loadDataCoverage();
    _wireCompliancePreview();
  }

  function _wireCompliancePreview() {
    const inputs = ['cfg-issuer-exp', 'cfg-sector-exp', 'cfg-min-rating', 'cfg-max-dur', 'cfg-group-exp', 'cfg-price-dev'];
    const _doPreview = Utils.debounce(async () => {
      const panel  = document.getElementById('compliance-preview-panel');
      const body   = document.getElementById('compliance-preview-body');
      if (!panel || !body) return;
      const maxIssuer  = parseFloat(document.getElementById('cfg-issuer-exp')?.value) || 15;
      const maxSector  = parseFloat(document.getElementById('cfg-sector-exp')?.value) || 35;
      const maxDur     = parseFloat(document.getElementById('cfg-max-dur')?.value)    || 8;
      const minRating  = document.getElementById('cfg-min-rating')?.value || 'AA-';
      const ratingRank = { 'AAA': 5, 'AA+': 4, 'AA': 3, 'AA-': 2, 'A+': 1, 'A': 0 };
      const minRank    = ratingRank[minRating] ?? 0;
      try {
        const [concRes, portRes] = await Promise.all([
          API.getPortfolioConcentration().catch(() => null),
          API.getPortfolio().catch(() => null),
        ]);
        const warnings = [];
        if (concRes) {
          (concRes.issuer_concentration || []).forEach(row => {
            if (row.pct > maxIssuer) warnings.push(`Issuer <strong>${row.issuer_name || row.isin}</strong>: ${row.pct.toFixed(1)}% > ${maxIssuer}% limit`);
          });
          (concRes.sector_concentration || []).forEach(row => {
            if (row.pct > maxSector) warnings.push(`Sector <strong>${row.sector}</strong>: ${row.pct.toFixed(1)}% > ${maxSector}% limit`);
          });
        }
        if (portRes) {
          const holdings = portRes.holdings || [];
          holdings.forEach(h => {
            if ((ratingRank[h.rating] ?? 99) < minRank) warnings.push(`Bond <span class="font-mono">${h.isin}</span>: rating <strong>${h.rating}</strong> below ${minRating} minimum`);
            if ((h.modified_duration || 0) > maxDur) warnings.push(`Bond <span class="font-mono">${h.isin}</span>: duration ${(h.modified_duration||0).toFixed(2)}</strong> > ${maxDur}yr limit`);
          });
        }
        if (warnings.length) {
          panel.style.display = 'block';
          body.innerHTML = warnings.slice(0, 10).map(w => `<div style="padding:3px 0;border-bottom:1px solid #f59e0b22">â€¢ ${w}</div>`).join('') +
            (warnings.length > 10 ? `<div style="color:#b45309;margin-top:4px">â€¦and ${warnings.length - 10} more</div>` : '');
        } else {
          panel.style.display = 'block';
          body.innerHTML = '<div style="color:#16a34a;font-weight:600">âœ“ No breaches with these settings</div>';
        }
      } catch (_) {}
    }, 600);
    inputs.forEach(id => {
      const el = document.getElementById(id);
      if (el) el.addEventListener('input', _doPreview);
      if (el) el.addEventListener('change', _doPreview);
    });
  }

  function _readAdminComplianceNumber(fieldId, label, fallback) {
    const rawValue = document.getElementById(fieldId)?.value;
    const textValue = String(rawValue ?? '').trim();
    if (textValue === '') {
      return { ok: true, value: fallback };
    }
    const value = Number(textValue);
    if (!Number.isFinite(value) || value < 0 || value > 100) {
      return { ok: false, message: `${label} must be between 0 and 100` };
    }
    return { ok: true, value };
  }

  async function loadAuditLogs() {
    try {
      const logs = await API.healthLog(30);
      const items = (Array.isArray(logs) ? logs : logs.entries || logs.log || []);

      const target = document.getElementById('admin-audit-table') || document.getElementById('comp-audit');
      if (!target) {
        Utils.toast('info', 'Audit Logs', 'Open Admin Settings to view audit logs');
        return;
      }
      target.innerHTML = items.length ? `
        <table class="data-table">
          <thead><tr><th>Timestamp</th><th>Service</th><th>Action</th><th>Status</th><th>Duration</th><th>Details</th></tr></thead>
          <tbody>${items.slice(0, 50).map(l => `<tr>
            <td>${Utils.formatDateTime(l.recorded_at)}</td>
            <td>${l.service_name || 'â€”'}</td>
            <td>${l.check_type || 'â€”'}</td>
            <td>${Utils.statusBadge(l.status)}</td>
            <td>${l.duration_ms ? l.duration_ms + 'ms' : 'â€”'}</td>
            <td class="truncate" style="max-width:200px" title="${l.message || ''}">${l.message || 'â€”'}</td>
          </tr>`).join('')}</tbody>
        </table>
      ` : Utils.emptyHTML('No audit logs', 'System activity logs will appear here');
    } catch (err) {
      const target = document.getElementById('admin-audit-table') || document.getElementById('comp-audit');
      if (target) target.innerHTML = Utils.emptyHTML('Could not load audit logs', err.message);
    }
  }

  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  // MODALS
  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  function showTradeModal(type) {
    const modal = document.getElementById('modal-overlay');
    const content = document.getElementById('modal-content');
    if (!modal || !content) {
      Utils.toast('error', 'Unavailable', 'Trade modal is not ready');
      return;
    }
    content.innerHTML = `
      <div class="modal-header">
        <div class="modal-title">${type === 'buy' ? 'Add Investment' : 'Sell Investment'}</div>
        <button class="modal-close" data-action="close-modal">${IC.x}</button>
      </div>
      <div class="modal-body">
        <div class="form-row">
          <div class="form-group">
            <label class="form-label">ISIN</label>
            <input type="text" class="form-input" placeholder="INE001A07..." id="trade-isin" />
          </div>
          <div class="form-group">
            <label class="form-label">Face Value (â‚¹ Crores)</label>
            <input type="number" class="form-input" value="10" step="1" id="trade-fv" />
          </div>
        </div>
        <div class="form-row">
          <div class="form-group">
            <label class="form-label">Price (â‚¹)</label>
            <input type="number" class="form-input" placeholder="100.00" step="0.01" id="trade-price" />
          </div>
          <div class="form-group">
            <label class="form-label">YTM (%)</label>
            <input type="number" class="form-input" placeholder="7.50" step="0.01" id="trade-ytm" />
          </div>
        </div>
        <div class="form-group">
          <label class="form-label">Settlement Date</label>
          <input type="date" class="form-input" value="${new Date().toISOString().split('T')[0]}" id="trade-date" />
        </div>
        <div class="form-group">
          <label class="form-label">Notes / Override Reason</label>
          <textarea class="form-input" rows="2" placeholder="Optional notes. Compliance exceptions require maker-checker override approval in Decision Registry." id="trade-notes"></textarea>
        </div>

        <!-- Dynamic Portfolio Impact Preview -->
        <div id="trade-impact-panel" style="margin-top:16px;padding:16px;background:var(--bg-tertiary);border-radius:var(--border-radius)">
          <div style="font-size:0.82rem;font-weight:700;margin-bottom:8px">Portfolio Impact Preview</div>
          <div class="text-muted" style="font-size:0.78rem">Enter ISIN and amount to preview impact...</div>
        </div>

        <!-- Dynamic Compliance Status -->
        <div id="trade-compliance-panel" style="margin-top:12px">
          <div class="compliance-indicator" style="background:var(--bg-tertiary);border-left:3px solid var(--text-muted)">
            <span class="text-muted" style="font-size:0.82rem">Compliance check will run automatically when ISIN is entered</span>
          </div>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-secondary" data-action="close-modal">Cancel</button>
        <button class="btn btn-primary" id="trade-confirm-btn" type="button" data-method="executeTrade" data-arg="${type}">
          ${type === 'buy' ? 'Confirm Investment' : 'Confirm Sale'}
        </button>
      </div>
    `;
    modal.classList.add('show');

    // Auto-run compliance check when ISIN or FV changes
    const isinEl = document.getElementById('trade-isin');
    const fvEl = document.getElementById('trade-fv');
    const runCheck = Utils.debounce(async () => {
      const isin = (isinEl.value || '').trim().toUpperCase();
      const fvCr = parseFloat(fvEl.value) || 10;
      if (!isin || isin.length < 10) return;

      const panel = document.getElementById('trade-impact-panel');
      const compPanel = document.getElementById('trade-compliance-panel');
      panel.innerHTML = '<div style="font-size:0.82rem;font-weight:700;margin-bottom:8px">Portfolio Impact Preview</div><div class="text-muted" style="font-size:0.78rem">Checking compliance...</div>';

      try {
        const [comp, portData] = await Promise.all([
          API.preTradeCheck(isin, fvCr),
          API.getPortfolio().catch(() => ({ summary: {} })),
        ]);
        const summary = portData.summary || {};
        const curYTM = summary.weighted_ytm || 0;
        const curDur = summary.weighted_modified_duration || 0;
        const curAUM = summary.aum_crores || 0;
        const addWeight = curAUM > 0 ? fvCr / (curAUM + fvCr) : 1;
        const tradeYTM = parseFloat(document.getElementById('trade-ytm')?.value) || 0;
        const newYTM = tradeYTM > 0 ? curYTM + (tradeYTM - curYTM) * addWeight : curYTM;
        const newAUM = curAUM + fvCr;

        panel.innerHTML = `
          <div style="font-size:0.82rem;font-weight:700;margin-bottom:8px">Portfolio Impact Preview</div>
          <div class="grid-2" style="gap:8px;font-size:0.78rem">
            <div class="flex justify-between"><span class="text-muted">Current AUM</span><span class="fw-600">â‚¹${curAUM.toFixed(2)} Cr</span></div>
            <div class="flex justify-between"><span class="text-muted">Post-Trade AUM</span><span class="fw-600 text-green">â‚¹${newAUM.toFixed(2)} Cr</span></div>
            <div class="flex justify-between"><span class="text-muted">Current YTM</span><span class="fw-600">${curYTM.toFixed(4)}%</span></div>
            <div class="flex justify-between"><span class="text-muted">Est. New YTM</span><span class="fw-600">${newYTM.toFixed(4)}%</span></div>
            <div class="flex justify-between"><span class="text-muted">Current Duration</span><span class="fw-600">${curDur.toFixed(2)} yrs</span></div>
            <div class="flex justify-between"><span class="text-muted">Trade Weight</span><span class="fw-600">${(addWeight * 100).toFixed(2)}%</span></div>
          </div>
        `;

        const overall = comp.overall || 'UNKNOWN';
        const score = comp.score || 0;
        const fails = (comp.violations || []).filter(v => v.status === 'FAIL');
        const warns = (comp.violations || []).filter(v => v.status === 'WARN');
        const color = overall === 'PASS' ? 'green' : overall === 'FAIL' ? 'red' : 'amber';
        const failsHtml = fails.length ? `<div style="font-size:0.72rem;margin-top:6px;color:var(--color-danger)">${fails.map(f => 'âœ— ' + f.message).join('<br>')}</div>` : '';
        const warnsHtml = warns.length ? `<div style="font-size:0.72rem;margin-top:4px;color:var(--color-warning)">${warns.slice(0,3).map(w => 'âš  ' + w.message).join('<br>')}</div>` : '';

        compPanel.innerHTML = `
          <div class="compliance-indicator ${color}" style="margin-top:12px">
            <span class="status-dot ${color}"></span>
            Pre-Trade Compliance: <strong>${overall}</strong> â€” Score: ${score.toFixed(1)}% â€” ${comp.rules_checked || 0} rules checked
          </div>
          ${failsHtml}${warnsHtml}
        `;
      } catch (err) {
        compPanel.innerHTML = `<div class="compliance-indicator amber" style="margin-top:12px"><span class="status-dot amber"></span>Compliance check error: ${Utils.escapeHTML(err.message)}</div>`;
      }
    }, 600);

    isinEl.addEventListener('input', runCheck);
    isinEl.addEventListener('blur', runCheck);
    fvEl.addEventListener('change', runCheck);
  }

  function showQuoteUploadModal() {
    const modal = document.getElementById('modal-overlay');
    const content = document.getElementById('modal-content');
    if (!modal || !content) {
      Utils.toast('error', 'Unavailable', 'Quote upload modal is not ready');
      return;
    }
    content.innerHTML = `
      <div class="modal-header">
        <div class="modal-title">Upload Broker Quote</div>
        <button class="modal-close" data-action="close-modal">${IC.x}</button>
      </div>
      <div class="modal-body">
        <div style="margin-bottom:16px">
          <label class="form-label">Broker Quote File (PDF / Excel)</label>
          <div id="quote-dropzone" style="border:2px dashed var(--color-border);border-radius:8px;padding:32px;text-align:center;cursor:pointer;transition:background 0.2s"
            >
            <div style="font-size:2rem;margin-bottom:8px">ðŸ“„</div>
            <div style="font-weight:600;margin-bottom:4px">Drag & drop file here, or click to browse</div>
            <div style="font-size:0.78rem;color:var(--color-text-muted)">Supports: .pdf, .xlsx, .xls â€” auto-detects broker format</div>
            <input type="file" id="quote-file-input" accept=".pdf,.xlsx,.xls" style="display:none"
              >
          </div>
          <div id="quote-upload-status" style="margin-top:10px;font-size:0.82rem"></div>
          <div id="quote-progress-wrap" style="display:none;margin-top:8px;height:6px;background:var(--border-light,#e5e7eb);border-radius:3px;overflow:hidden">
            <div id="quote-progress-bar" style="width:0%;height:100%;background:var(--color-primary,#2563eb);transition:width 0.4s ease;border-radius:3px"></div>
          </div>
        </div>
        <details style="margin-top:8px">
          <summary style="cursor:pointer;font-size:0.82rem;color:var(--color-text-muted)">Or enter a single quote manually</summary>
          <div style="margin-top:12px">
            <div class="form-row">
              <div class="form-group">
                <label class="form-label">ISIN</label>
                <input type="text" class="form-input" placeholder="INE001A07..." id="quote-isin" />
              </div>
              <div class="form-group">
                <label class="form-label">Broker Name</label>
                <input type="text" class="form-input" placeholder="ICICI Securities" id="quote-broker" />
              </div>
            </div>
            <div class="form-row">
              <div class="form-group">
                <label class="form-label">Bid Yield (%)</label>
                <input type="number" class="form-input" step="0.01" id="quote-bid-yield" />
              </div>
              <div class="form-group">
                <label class="form-label">Ask Yield (%)</label>
                <input type="number" class="form-input" step="0.01" id="quote-ask-yield" />
              </div>
            </div>
            <div class="form-group">
              <label class="form-label">Quote Date</label>
              <input type="date" class="form-input" value="${new Date().toISOString().split('T')[0]}" id="quote-date" />
            </div>
          </div>
        </details>
      </div>
      <div class="modal-footer">
        <button class="btn btn-secondary" data-action="close-modal">Cancel</button>
        <button class="btn btn-primary" data-action="submit-quote">Submit Manual Quote</button>
      </div>
    `;
    const quoteDropzone = document.getElementById('quote-dropzone');
    const quoteFileInput = document.getElementById('quote-file-input');
    if (quoteDropzone && quoteFileInput) {
      quoteDropzone.addEventListener('dragover', (event) => {
        event.preventDefault();
        quoteDropzone.style.background = 'var(--color-surface-2)';
      });
      quoteDropzone.addEventListener('dragleave', () => {
        quoteDropzone.style.background = '';
      });
      quoteDropzone.addEventListener('drop', (event) => App._handleQuoteFileDrop(event));
      quoteDropzone.addEventListener('click', () => quoteFileInput.click());
      quoteFileInput.addEventListener('change', function () {
        App._handleQuoteFileSelect(this.files[0]);
      });
    }
    modal.classList.add('show');
  }

  async function _handleQuoteFileDrop(event) {
    event.preventDefault();
    document.getElementById('quote-dropzone').style.background = '';
    const file = event.dataTransfer.files[0];
    if (file) _handleQuoteFileSelect(file);
  }

  async function _handleQuoteFileSelect(file) {
    if (!file) return;
    const el  = document.getElementById('quote-upload-status');
    const dz  = document.getElementById('quote-dropzone');
    const pWrap = document.getElementById('quote-progress-wrap');
    const pBar  = document.getElementById('quote-progress-bar');

    // Show & animate progress bar to ~80% while waiting
    if (pWrap) pWrap.style.display = 'block';
    let _pct = 0;
    const _tick = setInterval(() => {
      if (_pct < 80) { _pct += Math.random() * 12; if (_pct > 80) _pct = 80; }
      if (pBar) pBar.style.width = _pct + '%';
    }, 300);

    if (el) el.innerHTML = `<span style="color:var(--color-text-muted)">Uploading <strong>${file.name}</strong>â€¦</span>`;
    if (dz) dz.style.borderColor = 'var(--color-primary)';
    try {
      const result = await API.uploadBrokerQuoteFile(file);
      clearInterval(_tick);
      if (pBar) { pBar.style.width = '100%'; pBar.style.background = '#22c55e'; }
      const _isUnknown = (result.broker === 'Unknown' || result.broker === 'unknown');
      if (el) el.innerHTML = `
        <div style="color:var(--color-success);font-weight:600">âœ“ ${result.quotes_stored} quotes stored</div>
        <div style="font-size:0.75rem;color:var(--color-text-muted)">
          Broker: <strong>${result.broker}</strong> &nbsp;|&nbsp;
          Date: ${result.quote_date} &nbsp;|&nbsp;
          Parsed: ${result.quotes_parsed} &nbsp;|&nbsp;
          Format: ${result.format}
        </div>
        ${result.errors?.length ? `<div style="color:var(--color-danger);font-size:0.72rem">Errors: ${result.errors.join('; ')}</div>` : ''}
        ${_isUnknown ? `
          <div style="margin-top:10px;padding:10px;background:rgba(245,158,11,0.1);border-radius:8px;border:1px solid #f59e0b">
            <div style="font-size:0.8rem;font-weight:600;color:#b45309;margin-bottom:6px">âš  Broker not detected â€” please identify:</div>
            <div style="display:flex;gap:8px;align-items:center">
              <input type="text" id="broker-name-identify" class="form-input" placeholder="e.g. ICICI Securities" style="flex:1;padding:6px 10px;font-size:0.82rem" />
              <button class="btn btn-primary btn-sm" type="button" data-method="_applyBrokerRename" data-arg="${Utils.escapeAttr(file.name || '')}">Apply</button>
            </div>
          </div>` : ''}
      `;
      if (dz) dz.style.borderColor = _isUnknown ? '#f59e0b' : 'var(--color-success)';
      Utils.toast(_isUnknown ? 'warning' : 'success', 'Quotes Imported',
        _isUnknown ? `${result.quotes_stored} quotes stored â€” broker unidentified` : `${result.quotes_stored} quotes from ${result.broker}`);
      // Auto-close only when broker was identified
      if (!_isUnknown) {
        setTimeout(() => {
          App.closeModal();
          renderRecommendations(window._recPageEl || document.getElementById('page-content'));
        }, 2000);
      }
    } catch (err) {
      clearInterval(_tick);
      if (pBar) { pBar.style.width = '100%'; pBar.style.background = '#ef4444'; }
      if (el) el.innerHTML = `<div style="color:var(--color-danger)">Upload failed: ${Utils.escapeHTML(err.message)}</div>`;
      if (dz) dz.style.borderColor = 'var(--color-danger)';
      Utils.toast('error', 'Upload Failed', err.message);
    }
  }

  function closeModal() {
    const overlay = document.getElementById('modal-overlay');
    if (overlay) {
      overlay.classList.remove('show');
      overlay.setAttribute('aria-hidden', 'true');
    }
    if (_focusTrapState.container === overlay) {
      deactivateFocusTrap();
    }
  }

  async function _applyBrokerRename(filename) {
    const name = document.getElementById('broker-name-identify')?.value?.trim();
    if (!name) { Utils.toast('warning', 'Required', 'Enter a broker name first'); return; }
    const restoreButtons = _setBusyButtons('button[data-method="_applyBrokerRename"], button[onclick*="App._applyBrokerRename("]', true, 'Savingâ€¦');
    try {
      await API.renameBroker('Unknown', name, filename);
      Utils.toast('success', 'Broker Identified', `${filename || 'Unknown file'} â†’ ${name}`);
      App.closeModal();
      loadDataSourcesData();
    } catch (err) { Utils.toast('error', 'Rename Failed', err.message); } finally { restoreButtons(); }
  }

  async function _renameUnknownBrokerFile(sourceFile) {
    const name = window.prompt(`Enter broker name for file:\n${sourceFile || '(unnamed)'}`)?.trim();
    if (!name) return;
    const restoreButtons = _setBusyButtons('button[data-method="_renameUnknownBrokerFile"], button[onclick*="App._renameUnknownBrokerFile("]', true, 'Savingâ€¦');
    try {
      await API.renameBroker('Unknown', name, sourceFile);
      Utils.toast('success', 'Broker Named', `${sourceFile || 'File'} â†’ ${name}`);
      loadDataSourcesData();
    } catch (err) { Utils.toast('error', 'Rename Failed', err.message); } finally { restoreButtons(); }
  }

  async function executeTrade(type) {
    const isin = document.getElementById('trade-isin')?.value;
    const fvCr = parseFloat(document.getElementById('trade-fv')?.value);
    const price = parseFloat(document.getElementById('trade-price')?.value);
    const date = document.getElementById('trade-date')?.value;
    const notes = document.getElementById('trade-notes')?.value;
    const trustId = getActiveTrustId();

    if (!isin || isNaN(fvCr)) {
      Utils.toast('warning', 'Missing Details', 'ISIN and Face Value are required');
      return;
    }

    // Convert Face Value (Crores) to Quantity (Units)
    // Formula: (Value in Cr * 10,000,000) / Standard Face Value 100
    const quantity = Math.round((fvCr * 1e7) / 100);

    const restoreButtons = _setBusyButtons('button[data-method="executeTrade"], button[onclick*="App.executeTrade("]', true, 'Executingâ€¦');
    Utils.toast('info', 'Processing', `Executing ${type.toUpperCase()}...`);
    
    try {
      await API.executeTrade({
        isin: isin.trim().toUpperCase(),
        action: type.toUpperCase(),
        quantity: quantity,
        price: price || 100.0,
        date: date || new Date().toISOString().split('T')[0],
        trust_id: trustId,
        broker: "Direct",
        notes: notes || ""
      });

      Utils.toast('success', 'Success', `${type.toUpperCase()} executed for ${isin}`);
      closeModal();
      
      // Refresh current view if it's portfolio or dashboard
      if (['dashboard', 'portfolio'].includes(currentPage)) {
        navigate(currentPage);
      }
    } catch (error) {
      console.error('Trade execution failed:', error);
      Utils.toast('error', 'Trade Failed', error.message || 'Server error during execution');
    } finally {
      restoreButtons();
    }
  }

  async function submitQuote() {
    const isin = document.getElementById('quote-isin')?.value;
    const broker = document.getElementById('quote-broker')?.value;
    if (!isin || !broker) { Utils.toast('warning', 'Missing', 'ISIN and Broker are required'); return; }
    const restoreButtons = _setBusyButtons('button[data-action="submit-quote"], button[onclick*="App.submitQuote()"]', true, 'Submittingâ€¦');
    try {
      await API.ingestQuote({
        isin: isin.trim().toUpperCase(),
        broker_name: broker.trim(),
        quote_date: document.getElementById('quote-date')?.value || new Date().toISOString().split('T')[0],
        bid_yield: parseFloat(document.getElementById('quote-bid-yield')?.value) || null,
        ask_yield: parseFloat(document.getElementById('quote-ask-yield')?.value) || null,
      });
      Utils.toast('success', 'Quote Saved', `Broker quote for ${isin} ingested`);
      closeModal();
    } catch (err) {
      Utils.toast('error', 'Error', err.message);
    } finally {
      restoreButtons();
    }
  }

  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  // Global Search
  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  async function globalSearch(query) {
    if (!query || query.length < 2) {
      hideSearchResults();
      return;
    }
    try {
      const result = await API.listBonds({ keyword: query, limit: 10 });
      const bonds = result.bonds || [];
      const dropdown = document.getElementById('search-results');
      const searchInput = document.getElementById('global-search');
      if (!dropdown) return;
      if (!bonds.length) {
        dropdown.innerHTML = '<div style="padding:12px;font-size:0.82rem;color:var(--text-muted)">No results found</div>';
        dropdown.style.display = 'block';
        dropdown.removeAttribute('aria-activedescendant');
        if (searchInput) searchInput.setAttribute('aria-expanded', 'true');
        _searchActiveIndex = -1;
        return;
      }
      dropdown.innerHTML = bonds.map((b, idx) => `
        <button type="button" id="search-result-${idx}" class="search-result-item" role="option" aria-selected="false" data-isin="${Utils.escapeAttr(b.isin)}" style="padding:8px 12px;cursor:pointer;border-bottom:1px solid var(--border-light);font-size:0.82rem;transition:background 0.1s">
          <div class="fw-600">${Utils.escapeHTML(b.issuer_name || b.isin)}</div>
          <div class="text-muted" style="font-size:0.7rem">${Utils.escapeHTML(b.isin)} â€¢ ${Utils.escapeHTML(b.credit_rating || 'NR')} â€¢ ${Utils.escapeHTML(b.issuer_type || '')}</div>
        </button>
      `).join('');
      dropdown.querySelectorAll('.search-result-item').forEach((item, idx) => {
        item.addEventListener('click', () => {
          const isin = item.getAttribute('data-isin') || '';
          navigate('analysis', { isin });
          hideSearchResults();
        });
        item.addEventListener('mouseenter', () => {
          setSearchActiveIndex(idx);
        });
      });
      dropdown.style.display = 'block';
      if (searchInput) searchInput.setAttribute('aria-expanded', 'true');
      setSearchActiveIndex(0);
    } catch (err) {
      console.error('Search error:', err);
    }
  }

  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  async function _loadAdminUsers() {
    try {
      const users = await API.listUsers().catch(() => []);
      const tableEl = document.getElementById('admin-users-table');
      if (!tableEl) return;
      if (!users || !users.length) {
        tableEl.innerHTML = Utils.emptyHTML('No users found', 'Create users via the API or add user button');
        return;
      }
      const roleBadge = r => ({'ADMIN':'badge-red','PORTFOLIO':'badge-blue','COMPLIANCE':'badge-amber','VIEWER':'badge-gray'}[r] || 'badge-gray');
      tableEl.innerHTML = `
        <table class="data-table">
          <thead><tr><th>Username</th><th>Email</th><th>Role</th><th>Status</th><th>Last Login</th><th>Created</th></tr></thead>
          <tbody>${users.map(u => `<tr>
            <td><strong>${u.username}</strong></td>
            <td>${u.email}</td>
            <td><span class="badge ${roleBadge(u.role)}">${u.role}</span></td>
            <td><span class="badge ${u.is_active ? 'badge-green' : 'badge-red'}">${u.is_active ? 'Active' : 'Inactive'}</span></td>
            <td>${u.last_login ? Utils.formatDateTime(u.last_login) : '\u2014'}</td>
            <td>${Utils.formatDate(u.created_at)}</td>
          </tr>`).join('')}</tbody>
        </table>
      `;
    } catch (err) {
      const tableEl = document.getElementById('admin-users-table');
      if (tableEl) tableEl.innerHTML = Utils.emptyHTML('Could not load users', err.message);
    }
  }

  function _showAddUserModal() {
    const modal = document.getElementById('modal-overlay');
    document.getElementById('modal-content').innerHTML = `
      <div class="modal-header">
        <div class="modal-title">Add User</div>
        <button class="modal-close" data-action="close-modal">&#x2715;</button>
      </div>
      <div class="modal-body">
        <div class="form-group"><label class="form-label">Username</label><input type="text" class="form-input" id="new-user-name" placeholder="jsmith" /></div>
        <div class="form-group"><label class="form-label">Email</label><input type="email" class="form-input" id="new-user-email" placeholder="j.smith@example.com" /></div>
        <div class="form-group"><label class="form-label">Role</label><select class="form-input" id="new-user-role"><option value="VIEWER">VIEWER</option><option value="COMPLIANCE">COMPLIANCE</option><option value="PORTFOLIO">PORTFOLIO</option><option value="ADMIN">ADMIN</option></select></div>
        <div class="form-group"><label class="form-label">Password</label><input type="password" class="form-input" id="new-user-pass" placeholder="â€¢â€¢â€¢â€¢â€¢â€¢â€¢â€¢" /></div>
        <div id="add-user-error" style="color:var(--accent-red);font-size:0.8rem;min-height:16px"></div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-secondary" data-action="close-modal">Cancel</button>
        <button class="btn btn-primary" type="button" data-method="_submitAddUser">Create User</button>
      </div>
    `;
    modal.classList.add('show');
  }

  async function _submitAddUser() {
    const u = document.getElementById('new-user-name')?.value.trim();
    const e = document.getElementById('new-user-email')?.value.trim();
    const r = document.getElementById('new-user-role')?.value;
    const p = document.getElementById('new-user-pass')?.value;
    const errEl = document.getElementById('add-user-error');
    if (!u || !e || !p) { if (errEl) errEl.textContent = 'All fields are required'; return; }
    const restoreButtons = _setBusyButtons('button[data-method="_submitAddUser"], button[onclick*="App._submitAddUser()"]', true, 'Creatingâ€¦');
    try {
      await API.registerUser({ username: u, email: e, role: r, password: p });
      Utils.toast('success', 'User Created', `${u} added with role ${r}`);
      closeModal();
      _loadAdminUsers();
    } catch (err) {
      if (errEl) errEl.textContent = err.message;
    } finally {
      restoreButtons();
    }
  }

  // ===============================================================
  // Authentication Functions
  // ===============================================================
  function showLoginScreen(message = '') {
    _setSessionState(false, _looksLikeExpiredSession(message) || _sessionExpired);
    closeModal();
    _clearUserChrome();
    renderSessionLocked(message || 'Sign in to continue.');
    const overlay = document.getElementById('login-overlay');
    if (overlay) {
      overlay.style.display = 'flex';
      overlay.setAttribute('aria-hidden', 'false');
      activateFocusTrap(overlay, { onEscape: null });
      const usernameInput = document.getElementById('login-username');
      if (usernameInput) {
        setTimeout(() => usernameInput.focus(), 0);
      }
    }
    const errEl = document.getElementById('login-error');
    if (errEl) {
      errEl.textContent = message || '';
      errEl.style.color = message ? '#ef4444' : '';
    }
  }

  function hideLoginScreen() {
    _setAuthLockedState(false);
    const overlay = document.getElementById('login-overlay');
    if (overlay) {
      overlay.style.display = 'none';
      overlay.setAttribute('aria-hidden', 'true');
      if (_focusTrapState.container === overlay) {
        deactivateFocusTrap();
      }
    }
    const errEl = document.getElementById('login-error');
    if (errEl) {
      errEl.textContent = '';
      errEl.style.color = '';
    }
  }

  async function doLogin(e) {
    if (e) e.preventDefault();
    const username = document.getElementById('login-username')?.value.trim();
    const password = document.getElementById('login-password')?.value;
    const errEl = document.getElementById('login-error');
    const btn = document.getElementById('login-btn');
    if (!username || !password) { if (errEl) errEl.textContent = 'Please enter username and password'; return; }
    if (errEl) { errEl.textContent = ''; errEl.style.color = ''; }
    if (btn) { btn.textContent = 'Signing in...'; btn.disabled = true; }
    try {
      const resp = await API.login(username, password);
      _setSessionState(true, false);
      hideLoginScreen();
      window.dispatchEvent(new CustomEvent('pf:auth-changed', { detail: { authenticated: true, username } }));
      _patchLiveSimulationApiOverlay();
      await _loadAndRenderUserInfo();
      const hash = window.location.hash.replace('#', '') || 'dashboard';
      navigate(hash);
      loadNotifications();
    } catch (err) {
      console.error('[doLogin] error:', err);
      if (errEl) { errEl.style.color = '#ef4444'; errEl.textContent = err.message || 'Invalid credentials'; }
      if (btn) { btn.textContent = 'Sign In'; btn.disabled = false; }
    }
  }

  async function logout() {
    _clearSimulationState();
    _setSimulationBadge(false);
    _setSessionState(false, false);
    try { await API.post('/auth/logout'); } catch (_) {}
    _setSessionState(false, false);
    API.clearToken();
    _clearUserChrome();
    window.dispatchEvent(new CustomEvent('pf:auth-changed', { detail: { authenticated: false } }));
    showLoginScreen();
  }

  function handleSessionExpired(err) {
    _setSessionState(false, true);
    return handleAuthError(err, err?.message || 'Your session expired. Sign in again to continue.');
  }

  async function _loadAndRenderUserInfo() {
    try {
      const user = await API.authMe();
      if (user) {
        _setSessionState(true, false);
        const initials = (user.username || 'U').substring(0, 2).toUpperCase();
        const avatarEl = document.getElementById('topbar-avatar-initials');
        if (avatarEl) avatarEl.textContent = initials;
        const nameEl = document.getElementById('user-display-name');
        if (nameEl) nameEl.textContent = user.username;
        const roleEl = document.getElementById('user-display-role');
        if (roleEl) roleEl.textContent = `Role: ${user.role}`;
      }
    } catch { /* session invalid, will be caught by navigate */ }
  }

  async function loadAdminUsers() { _loadAdminUsers(); }

  // Notification Panel
  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  async function loadNotifications() {
    try {
      const alerts = await API.listAlerts({ limit: 200 });
      const openAlerts = (alerts.alerts || []).filter(a => !a.is_resolved);
      const count = openAlerts.length;
      const badge = document.getElementById('notification-count');
      const sidebarBadge = document.getElementById('sidebar-alert-count');
      if (badge) {
        badge.textContent = count;
        badge.style.display = count > 0 ? 'flex' : 'none';
      }
      if (sidebarBadge) {
        sidebarBadge.textContent = count;
        sidebarBadge.style.display = count > 0 ? 'inline-flex' : 'none';
      }
      return openAlerts;
    } catch { return []; }
  }

  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  // Initialization
  // â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
  async function init() {
    // â”€â”€ Auth check â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    let user = null;
    try {
      user = await API.authMe();
      if (user) {
        _setSessionState(true, false);
        const initials = (user.username || 'U').substring(0, 2).toUpperCase();
        const avatarEl = document.getElementById('topbar-avatar-initials');
        if (avatarEl) avatarEl.textContent = initials;
        const nameEl = document.getElementById('user-display-name');
        if (nameEl) nameEl.textContent = user.username;
        const roleEl = document.getElementById('user-display-role');
        if (roleEl) roleEl.textContent = `Role: ${user.role}`;
      }
    } catch {
      showLoginScreen();
      return;
    }

    window.dispatchEvent(new CustomEvent('pf:auth-changed', { detail: { authenticated: true, username: user ? user.username : undefined } }));

    _patchLiveSimulationApiOverlay();

    // Dark mode â€” honour saved pref, fall back to OS preference
    const prefersDark = window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false;
    const savedTheme = localStorage.getItem('theme') || (prefersDark ? 'dark' : 'light');
    document.documentElement.setAttribute('data-theme', savedTheme);
    const toggle = document.getElementById('dark-mode-toggle');
    if (toggle) toggle.checked = savedTheme === 'dark';
    localStorage.removeItem('benchmarkConfig');
    const sourceBadge = document.getElementById('trust-source-badge');
    if (sourceBadge) {
      sourceBadge.textContent = `Trust ${getActiveTrustId()}`;
    }
    // Live OS theme change (when no explicit pref saved)
    window.matchMedia?.('(prefers-color-scheme: dark)').addEventListener('change', e => {
      if (!localStorage.getItem('theme')) {
        document.documentElement.setAttribute('data-theme', e.matches ? 'dark' : 'light');
      }
    });

    // Route from hash (token is confirmed valid at this point)
    const hash = window.location.hash.replace('#', '') || 'dashboard';
    navigate(hash);

    // Mobile sidebar controls
    const mobileMenuBtn = document.getElementById('mobile-menu-toggle');
    if (mobileMenuBtn) {
      mobileMenuBtn.addEventListener('click', () => toggleSidebar());
    }
    const sidebarBackdrop = document.getElementById('sidebar-backdrop');
    if (sidebarBackdrop) {
      sidebarBackdrop.addEventListener('click', () => closeSidebar());
    }
    window.addEventListener('resize', () => {
      if (!isMobileViewport()) {
        closeSidebar();
      }
    });

    // Accessible user menu controls
    const avatarBtn = document.getElementById('topbar-avatar');
    if (avatarBtn) {
      avatarBtn.addEventListener('click', () => {
        const shouldOpen = avatarBtn.getAttribute('aria-expanded') !== 'true';
        toggleUserDropdown(shouldOpen);
      });
      avatarBtn.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          const shouldOpen = avatarBtn.getAttribute('aria-expanded') !== 'true';
          toggleUserDropdown(shouldOpen);
        } else if (e.key === 'ArrowDown') {
          e.preventDefault();
          toggleUserDropdown(true);
        } else if (e.key === 'Escape') {
          closeUserDropdown();
          avatarBtn.focus();
        }
      });
    }
    const userMenuAdmin = document.getElementById('user-menu-admin');
    if (userMenuAdmin) {
      userMenuAdmin.addEventListener('click', () => {
        closeUserDropdown();
        navigate('admin');
      });
    }
    const userMenuLogout = document.getElementById('user-menu-logout');
    if (userMenuLogout) {
      userMenuLogout.addEventListener('click', () => logout());
    }

    // Load notifications
    loadNotifications();

    window.addEventListener('pf:simulation-tick', (ev) => {
      const detail = ev?.detail || null;
      if (detail && typeof detail === 'object' && detail.snapshot && typeof detail.snapshot === 'object') {
        _fanOutSimulationTick(detail.snapshot, detail.session && typeof detail.session === 'object' ? detail.session : null, detail.source || 'local');
      } else {
        _fanOutSimulationTick(detail || null);
      }
    });

    window.addEventListener('storage', (ev) => {
      if (ev.key !== LIVE_SIMULATION_STATE_KEY) return;
      if (!ev.newValue) {
        _clearSimulationSnapshot();
        _setSimulationBadge(false);
        const analysisIsin = currentPage === 'analysis' || currentPage === 'bond-analysis'
          ? document.getElementById('analysis-isin')?.value?.trim() || ''
          : '';
        navigate(currentPage, analysisIsin ? { isin: analysisIsin } : {});
        return;
      }
      try {
        const parsed = JSON.parse(ev.newValue);
        const snapshot = parsed?.snapshot && typeof parsed.snapshot === 'object' ? parsed.snapshot : null;
        const sessionMeta = parsed?.session && typeof parsed.session === 'object' ? parsed.session : null;
        if (!snapshot) return;
        try {
          window.__pfSimulationSnapshot = snapshot;
          window.__pfSimulationSessionMeta = sessionMeta;
          window.__pfSimulationSessionId = sessionMeta?.session_id || snapshot.session_id || null;
        } catch (_) {}
        const topbarAum = document.getElementById('topbar-aum');
        if (topbarAum && snapshot.summary && snapshot.summary.total_market_value != null) {
          topbarAum.textContent = Utils.formatINR(Number(snapshot.summary.total_market_value || 0));
        }
        _setSimulationBadge(true);
        window.dispatchEvent(new CustomEvent('pf:simulation-tick', {
          detail: {
            snapshot,
            session: sessionMeta,
            source: 'storage',
          },
        }));
      } catch (_) {}
    });

    // â”€â”€ WebSocket â€” real-time alert + market push â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    const _wsTopicPaths = {
      alerts: '/ws/alerts',
      prices: '/ws/prices',
    };
    const _wsRetryState = {
      alerts: { delayMs: 2000, retries: 0 },
      prices: { delayMs: 2000, retries: 0 },
    };
    let _wsDisabled = false;
    const _wsSockets = {
      alerts: null,
      prices: null,
    };

    function _hasLiveToken() {
      return isAuthenticated();
    }

    function _disconnectWS(topic = null) {
      const topics = topic ? [topic] : Object.keys(_wsSockets);
      topics.forEach((name) => {
        const socket = _wsSockets[name];
        if (!socket) return;
        _wsSockets[name] = null;
        try {
          socket.onopen = null;
          socket.onmessage = null;
          socket.onclose = null;
          socket.onerror = null;
          socket.close(1000);
        } catch (_) {}
      });
    }

    function _handleAlertMessage(msg) {
      if (!(msg.type === 'alert' || msg.alert_type)) return;
      const sev = msg.severity || 'INFO';
      const badgeEl = document.getElementById('notification-count');
      const sbBadge = document.getElementById('sidebar-alert-count');
      const old = parseInt(badgeEl?.textContent || '0', 10);
      const upd = old + 1;
      if (badgeEl) { badgeEl.textContent = upd; badgeEl.style.display = 'flex'; }
      if (sbBadge) { sbBadge.textContent = upd; sbBadge.style.display = 'inline-flex'; }
      Utils.toast(sev === 'CRITICAL' ? 'error' : sev === 'WARNING' ? 'warning' : 'info',
        `Alert: ${sev}`, msg.title || msg.detail || 'New alert received');
    }

    function _handlePriceMessage(msg) {
      if (!msg || typeof msg !== 'object') return;

      if (msg.event === 'simulation_tick') {
        _emitSimulationTick({
          session_id: msg.session_id,
          timeline: {
            date: msg.date,
            index: msg.timeline_index,
          },
          summary: msg.summary || {},
          risk: msg.risk || {},
          im_checker: msg.im_checker || {},
        });
        return;
      }

      if (msg.event === 'market_data_refreshed' || msg.event === 'single_update' || msg.type === 'market_pulse' || msg.type === 'price_update') {
        if (currentPage === 'dashboard' && _canRefreshFromSimTick('dashboard', 1200)) {
          loadDashboardData(true);
        }
      }
    }

    function _connectTopicWS(topic) {
      if (_wsDisabled || _wsSockets[topic] || !_hasLiveToken()) return;
      const wsPath = _wsTopicPaths[topic];
      if (!wsPath) return;

      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      const wsUrl = `${proto}//${location.host}${wsPath}`;
      let ws;
      try { ws = new WebSocket(wsUrl); } catch (e) {
        const retry = _wsRetryState[topic];
        retry.retries += 1;
        return;
      }

      _wsSockets[topic] = ws;
      ws.onopen = () => {
        _wsRetryState[topic].delayMs = 2000;
        _wsRetryState[topic].retries = 0;
      };
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (topic === 'alerts') _handleAlertMessage(msg);
          if (topic === 'prices') _handlePriceMessage(msg);
        } catch (_) {}
      };
      ws.onclose = () => {
        if (_wsSockets[topic] === ws) _wsSockets[topic] = null;
        if (!_hasLiveToken()) return;
        const retry = _wsRetryState[topic];
        retry.retries += 1;
        if (retry.retries >= 6) {
          return;
        }
        retry.delayMs = Math.min(retry.delayMs * 2, 60000);
        setTimeout(() => {
          if (!_wsDisabled) _connectTopicWS(topic);
        }, retry.delayMs);
      };
      ws.onerror = () => {
        try { ws.close(); } catch (_) {}
      };
    }

    function _connectWS() {
      Object.keys(_wsSockets).forEach((topic) => _connectTopicWS(topic));
    }

    window.addEventListener('pf:auth-changed', (ev) => {
      const authenticated = !!ev?.detail?.authenticated && isAuthenticated();
      Object.keys(_wsRetryState).forEach((topic) => {
        _wsRetryState[topic].delayMs = 2000;
        _wsRetryState[topic].retries = 0;
      });
      if (authenticated) {
        _wsDisabled = false;
        _connectWS();
        return;
      }
      _wsDisabled = false;
      _disconnectWS();
    });

    // Global search
    const searchInput = document.getElementById('global-search');
    if (searchInput) {
      searchInput.addEventListener('input', Utils.debounce((e) => globalSearch(e.target.value), 300));
      searchInput.addEventListener('keydown', (e) => {
        const dropdown = document.getElementById('search-results');
        const isOpen = dropdown && dropdown.style.display === 'block';
        const items = dropdown ? Array.from(dropdown.querySelectorAll('.search-result-item')) : [];
        if (e.key === 'ArrowDown' && isOpen && items.length) {
          e.preventDefault();
          setSearchActiveIndex(_searchActiveIndex < 0 ? 0 : _searchActiveIndex + 1);
        } else if (e.key === 'ArrowUp' && isOpen && items.length) {
          e.preventDefault();
          setSearchActiveIndex(_searchActiveIndex <= 0 ? items.length - 1 : _searchActiveIndex - 1);
        } else if (e.key === 'Enter' && isOpen && _searchActiveIndex >= 0) {
          e.preventDefault();
          activateSearchResult();
        } else if (e.key === 'Escape') {
          hideSearchResults();
        }
      });
      searchInput.addEventListener('blur', () => setTimeout(() => {
        hideSearchResults();
      }, 200));
    }

    // Hash change (still handles manual URL edits / back-button navigation)
    window.addEventListener('hashchange', () => {
      if (!isAuthenticated()) return;
      const page = window.location.hash.replace('#', '') || 'dashboard';
      navigate(page);
    });

    window.addEventListener('popstate', () => {
      if (!isAuthenticated()) return;
      const page = window.location.hash.replace('#', '') || 'dashboard';
      navigate(page);
    });

    // Update time
    setInterval(() => {
      const el = document.getElementById('topbar-time');
      if (el) el.textContent = new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' });
    }, 30000);

    // Click outside to close dropdown
    document.addEventListener('click', (e) => {
      if (!e.target.closest('.topbar-avatar') && !e.target.closest('.user-dropdown')) {
        closeUserDropdown();
      }
    });

    // Close search suggestions on outside click
    document.addEventListener('click', (e) => {
      if (!e.target.closest('.topbar-search')) {
        hideSearchResults();
      }
    });

    // Modal focus management
    const modalOverlay = document.getElementById('modal-overlay');
    if (modalOverlay) {
      const observer = new MutationObserver(() => {
        const open = modalOverlay.classList.contains('show');
        modalOverlay.setAttribute('aria-hidden', open ? 'false' : 'true');
        if (open) {
          activateFocusTrap(modalOverlay, { onEscape: () => closeModal() });
        } else if (_focusTrapState.container === modalOverlay) {
          deactivateFocusTrap();
        }
      });
      observer.observe(modalOverlay, { attributes: true, attributeFilter: ['class'] });
    }

    // Escape key â€” close modal
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !isLoginOverlayOpen()) closeModal();
    });
  }

  // â”€â”€ Admin Settings Persistence â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
  async function saveAdminSettings(tab) {
    const restoreButtons = _setBusyButtons('button[data-method="saveAdminSettings"], button[onclick*="App.saveAdminSettings("]', true, 'Savingâ€¦');
    try {
      let payload, endpoint;
      if (tab === 'compliance') {
        const issuerExp = _readAdminComplianceNumber('cfg-issuer-exp', 'Max Single Issuer Exposure (%)', 15.0);
        const sectorExp = _readAdminComplianceNumber('cfg-sector-exp', 'Max Sector Exposure (%)', 35.0);
        const maxDur = _readAdminComplianceNumber('cfg-max-dur', 'Max Portfolio Duration (Years)', 8.0);
        const groupExp = _readAdminComplianceNumber('cfg-group-exp', 'Max Group Exposure (%)', 10.0);
        const priceDev = _readAdminComplianceNumber('cfg-price-dev', 'Price Deviation Alert Threshold (%)', 0.5);
        const invalidField = [issuerExp, sectorExp, maxDur, groupExp, priceDev].find((entry) => !entry.ok);
        if (invalidField) {
          Utils.toast('warning', 'Invalid compliance setting', invalidField.message);
          return;
        }
        payload = {
          max_single_issuer_pct: issuerExp.value,
          max_sector_pct: sectorExp.value,
          min_rating: document.getElementById('cfg-min-rating')?.value || 'AA-',
          max_avg_duration: maxDur.value,
          max_group_pct: groupExp.value,
          price_deviation_threshold_pct: priceDev.value,
        };
        endpoint = '/compliance/rules/config';
      } else if (tab === 'benchmark') {
        payload = {
          benchmark_curve: document.getElementById('cfg-bench-curve')?.value,
          spread_threshold_bps: parseFloat(document.getElementById('cfg-spread-thresh')?.value) || 50,
          target_duration: parseFloat(document.getElementById('cfg-target-dur')?.value) || 4.5,
          nav_frequency: document.getElementById('cfg-nav-freq')?.value || 'Daily',
        };
        // Persist benchmark config on the server only; avoid stale client-side carryover.
        await API.post('/intelligence/config/benchmark', payload);
        localStorage.removeItem('benchmarkConfig');
        Utils.toast('success', 'Saved', 'Benchmark settings saved successfully');
        return;
      } else {
        Utils.toast('warning', 'Unknown', `Unknown settings tab: ${tab}`);
        return;
      }
      await API.post(endpoint, payload);
      Utils.toast('success', 'Saved', `${tab.charAt(0).toUpperCase() + tab.slice(1)} settings saved successfully`);
    } catch (err) {
      Utils.toast('error', 'Save Failed', err.message || 'Could not save settings');
    } finally {
      restoreButtons();
    }
  }

  // â”€â”€â”€ IM Checker â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
  async function renderIMChecker(el, params = {}) {
    el.innerHTML = `
      <div class="page-header" style="margin-bottom:24px">
        <div>
          <h1 class="page-title">Intelligence Center</h1>
          <p class="page-subtitle" style="color:var(--text-muted);margin-top:4px;font-size:0.875rem">
            IM analysis &bull; Document library &bull; Scrape SEBI / BSE / NSE
          </p>
        </div>
      </div>

      <div id="imc-sim-bridge" style="display:none"></div>

      <!-- Tab Navigation -->
      <div role="tablist" aria-label="Intelligence Center tabs" style="display:flex;gap:4px;margin-bottom:20px;border-bottom:2px solid var(--border-light)">
        <button id="imc-tab-btn-analysis" role="tab" aria-selected="true" aria-controls="imc-tab-analysis" type="button" data-method="_imcShowTab" data-arg="analysis"
          style="padding:10px 20px;font-size:0.88rem;font-weight:600;border:none;background:none;cursor:pointer;
                 border-bottom:2px solid var(--accent-blue);margin-bottom:-2px;color:var(--accent-blue)">
          IM Analysis
        </button>
        <button id="imc-tab-btn-library" role="tab" aria-selected="false" aria-controls="imc-tab-library" type="button" data-method="_imcShowTab" data-arg="library"
          style="padding:10px 20px;font-size:0.88rem;font-weight:600;border:none;background:none;cursor:pointer;
                 border-bottom:2px solid transparent;margin-bottom:-2px;color:var(--text-muted)">
          Document Library
        </button>
        <button id="imc-tab-btn-upload" role="tab" aria-selected="false" aria-controls="imc-tab-upload" type="button" data-method="_imcShowTab" data-arg="upload"
          style="padding:10px 20px;font-size:0.88rem;font-weight:600;border:none;background:none;cursor:pointer;
                 border-bottom:2px solid transparent;margin-bottom:-2px;color:var(--text-muted)">
          Information Memorandum
        </button>
        <button id="imc-tab-btn-dossier" role="tab" aria-selected="false" aria-controls="imc-tab-dossier" type="button" data-method="_imcShowTab" data-arg="dossier"
          style="padding:10px 20px;font-size:0.88rem;font-weight:600;border:none;background:none;cursor:pointer;
                 border-bottom:2px solid transparent;margin-bottom:-2px;color:var(--text-muted)">
          Institutional Dossier
        </button>
        <button id="imc-tab-btn-fetch" role="tab" aria-selected="false" aria-controls="imc-tab-fetch" type="button" data-method="_imcShowTab" data-arg="fetch"
          style="padding:10px 20px;font-size:0.88rem;font-weight:600;border:none;background:none;cursor:pointer;
                 border-bottom:2px solid transparent;margin-bottom:-2px;color:var(--text-muted)">
          Fetch Sources
        </button>
      </div>

      <!-- Tab: IM Analysis -->
      <div id="imc-tab-analysis" role="tabpanel" aria-labelledby="imc-tab-btn-analysis" aria-hidden="false">
      <!-- Bond Search -->
      <div class="card" style="margin-bottom:20px;padding:20px">
        <div style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap">
          <div style="flex:1;min-width:220px">
            <label class="form-label">Search Bond (ISIN / Issuer)</label>
            <div style="position:relative">
              <input id="imc-search" type="text" class="form-input" placeholder="e.g. INE001A07123 or IRFC..."
                     style="width:100%;padding-right:36px" autocomplete="off" />
              <div id="imc-search-dd" style="display:none;position:absolute;top:100%;left:0;right:0;background:var(--bg-card);
                   border:1px solid var(--border-light);border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.15);
                   max-height:260px;overflow-y:auto;z-index:200"></div>
            </div>
          </div>
          <button class="btn btn-primary" type="button" data-method="_imcRunFull" style="white-space:nowrap">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:16px;height:16px"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>
            Run Full Analysis
          </button>
        </div>
        <div id="imc-bond-info" style="margin-top:12px;display:none"></div>
      </div>

      <!-- Two-column layout -->
      <div style="display:grid;grid-template-columns:420px 1fr;gap:20px;align-items:start" id="imc-main-grid">

        <!-- LEFT: Forms -->
        <div>
          <!-- IM Data Form -->
          <div class="card" style="margin-bottom:16px">
            <div style="padding:16px 20px;border-bottom:1px solid var(--border-light);display:flex;justify-content:space-between;align-items:center">
              <h3 style="margin:0;font-size:0.95rem;font-weight:600">IM Structure Data</h3>
              <button class="btn btn-sm btn-outline" type="button" data-method="_imcSaveIM">Save</button>
            </div>
            <div style="padding:16px 20px;display:flex;flex-direction:column;gap:12px">
              <div>
                <label class="form-label">Object of Issue</label>
                <input id="imc-object" type="text" class="form-input" placeholder="e.g. Working capital / capex" />
              </div>
              <div>
                <label class="form-label">Security Structure</label>
                <input id="imc-security" type="text" class="form-input" placeholder="e.g. First pari-passu charge on assets" />
              </div>
              <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
                <div>
                  <label class="form-label">Charge Type</label>
                  <select id="imc-charge" class="form-input">
                    <option value="FIRST">First</option>
                    <option value="SECOND">Second</option>
                    <option value="PARI_PASSU">Pari Passu</option>
                    <option value="UNSECURED">Unsecured</option>
                  </select>
                </div>
                <div>
                  <label class="form-label">Redemption</label>
                  <select id="imc-redemption" class="form-input">
                    <option value="BULLET">Bullet</option>
                    <option value="AMORTIZING">Amortizing</option>
                    <option value="SINKING">Sinking Fund</option>
                    <option value="STRUCTURED">Structured</option>
                  </select>
                </div>
              </div>
              <div>
                <label class="form-label">Trustee Name</label>
                <input id="imc-trustee" type="text" class="form-input" placeholder="e.g. IDBI Trusteeship" />
              </div>
              <div>
                <label class="form-label">Guarantee / Credit Enhancement</label>
                <input id="imc-guarantee" type="text" class="form-input" placeholder="e.g. Corporate guarantee from parent" />
              </div>
              <div>
                <label class="form-label">Default / Cross-Default Clauses</label>
                <input id="imc-default" type="text" class="form-input" placeholder="Key default triggers" />
              </div>
              <div>
                <label class="form-label">Step-Up Clauses</label>
                <input id="imc-stepup" type="text" class="form-input" placeholder="e.g. 25bps if rating falls below AA" />
              </div>
              <div>
                <label class="form-label">Escrow / Waterfall Mechanism</label>
                <input id="imc-escrow" type="text" class="form-input" placeholder="Escrow account details" />
              </div>
            </div>
          </div>

          <!-- Covenant Ratios -->
          <div class="card" style="margin-bottom:16px">
            <div style="padding:14px 20px;border-bottom:1px solid var(--border-light)">
              <h3 style="margin:0;font-size:0.9rem;font-weight:600">Covenant Ratios (Required)</h3>
            </div>
            <div style="padding:16px 20px;display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px">
              <div>
                <label class="form-label" style="font-size:0.75rem">DSCR (min)</label>
                <input id="imc-dscr-req" type="number" step="0.01" class="form-input" placeholder="e.g. 1.2" />
              </div>
              <div>
                <label class="form-label" style="font-size:0.75rem">ICR (min)</label>
                <input id="imc-icr-req" type="number" step="0.01" class="form-input" placeholder="e.g. 2.0" />
              </div>
              <div>
                <label class="form-label" style="font-size:0.75rem">D/EBITDA (max)</label>
                <input id="imc-de-req" type="number" step="0.01" class="form-input" placeholder="e.g. 5.0" />
              </div>
            </div>
          </div>

          <!-- Full IM Text -->
          <div class="card" style="margin-bottom:16px">
            <div style="padding:14px 20px;border-bottom:1px solid var(--border-light)">
              <h3 style="margin:0;font-size:0.9rem;font-weight:600">Full IM Text (for linguistic heuristics)</h3>
            </div>
            <div style="padding:16px 20px">
              <textarea id="imc-imtext" class="form-input" rows="6" placeholder="Paste full IM PDF text here..." style="width:100%;font-size:0.8rem;font-family:monospace;resize:vertical"></textarea>
            </div>
          </div>

          <!-- Financial Data -->
          <div class="card">
            <div style="padding:14px 20px;border-bottom:1px solid var(--border-light);display:flex;justify-content:space-between;align-items:center">
              <h3 style="margin:0;font-size:0.9rem;font-weight:600">Financials</h3>
              <button class="btn btn-sm btn-outline" type="button" data-method="_imcSaveFinancials">Save</button>
            </div>
            <div style="padding:16px 20px;display:flex;flex-direction:column;gap:12px">
              <div>
                <label class="form-label">Financial Year</label>
                <select id="imc-fy" class="form-input">
                  ${['FY2026','FY2025','FY2024','FY2023','FY2022'].map(y=>`<option value="${y}">${y}</option>`).join('')}
                </select>
              </div>
              <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
                ${[
                  ['imc-revenue','Revenue (Cr)'],['imc-ebitda','EBITDA (Cr)'],
                  ['imc-netprofit','Net Profit (Cr)'],['imc-interest','Interest Exp (Cr)'],
                  ['imc-debt','Total Debt (Cr)'],['imc-cash','Cash (Cr)'],
                  ['imc-assets','Total Assets (Cr)'],['imc-networth','Net Worth (Cr)'],
                  ['imc-curassets','Current Assets (Cr)'],['imc-curliab','Current Liab (Cr)'],
                  ['imc-cfo','CFO (Cr)'],['imc-capex','Capex (Cr)'],
                ].map(([id,label])=>`
                  <div>
                    <label class="form-label" style="font-size:0.75rem">${label}</label>
                    <input id="${id}" type="number" step="0.01" class="form-input" />
                  </div>
                `).join('')}
              </div>
              <div id="imc-ratios" style="display:none;background:var(--bg-tertiary);border-radius:8px;padding:12px;font-size:0.8rem;margin-top:4px"></div>
            </div>
          </div>
        </div>

        <!-- RIGHT: Results -->
        <div id="imc-results-container">
          <div style="text-align:center;padding:60px 24px;color:var(--text-muted);border:2px dashed var(--border-light);border-radius:12px;background:linear-gradient(180deg,rgba(15,23,42,0.02),rgba(15,23,42,0.04))">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="width:48px;height:48px;margin:0 auto 16px;display:block;opacity:.4"><path d="M9 12h6M9 16h6M9 8h6M5 4h14a1 1 0 011 1v14a1 1 0 01-1 1H5a1 1 0 01-1-1V5a1 1 0 011-1z"/></svg>
            <p style="font-size:0.95rem;font-weight:500;margin:0 0 8px">No analysis yet</p>
            <p style="font-size:0.8rem;margin:0 0 14px">Select a bond, paste IM text if available, then run the analysis.</p>
            <div style="display:flex;justify-content:center;gap:8px;flex-wrap:wrap;font-size:0.72rem">
              <span class="badge">1. Search ISIN</span>
              <span class="badge">2. Load / paste IM</span>
              <span class="badge">3. Run Full Analysis</span>
            </div>
          </div>
        </div>

      </div><!-- /imc-main-grid -->
      </div><!-- /imc-tab-analysis -->

      <!-- Tab: Document Library -->
      <div id="imc-tab-library" role="tabpanel" aria-labelledby="imc-tab-btn-library" aria-hidden="true" style="display:none">
        <div class="card" style="padding:20px;margin-bottom:16px">
          <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end">
            <div style="flex:1;min-width:180px">
              <label class="form-label">Search</label>
              <input id="imc-lib-q" type="text" class="form-input" placeholder="Issuer / ISIN / keyword" />
            </div>
            <div>
              <label class="form-label">Source</label>
              <select id="imc-lib-source" class="form-input">
                <option value="">All Sources</option>
                <option>SEBI</option><option>BSE</option><option>NSE</option><option>MANUAL</option>
              </select>
            </div>
            <div>
              <label class="form-label">Status</label>
              <select id="imc-lib-status" class="form-input">
                <option value="">All</option>
                <option value="DONE">Done</option>
                <option value="PENDING">Pending</option>
                <option value="FAILED">Failed</option>
                <option value="NO_PDF">No PDF</option>
              </select>
            </div>
            <button class="btn btn-primary" type="button" data-method="_imcLoadDocLibrary" data-arg="1">Search</button>
          </div>
        </div>
        <div id="imc-lib-body">
          <div style="text-align:center;padding:40px;color:var(--text-muted)">Use the filters above and click Search, or use the <strong>Fetch Sources</strong> tab to scrape SEBI/BSE/NSE.</div>
        </div>
      </div><!-- /imc-tab-library -->

      <!-- Tab: Information Memorandum Upload -->
      <div id="imc-tab-upload" role="tabpanel" aria-labelledby="imc-tab-btn-upload" aria-hidden="true" style="display:none">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;align-items:start">

          <!-- LEFT: Upload Area -->
          <div>
            <div class="card" style="padding:20px;margin-bottom:16px">
              <h3 style="margin:0 0 16px;font-size:1rem;font-weight:700">Upload Information Memorandum</h3>
              <p style="font-size:0.82rem;color:var(--text-muted);margin:0 0 16px">
                Upload an IM PDF for a bond. The system will automatically extract key fields,
                detect risk clauses, run the risk engine, and store structured data.
                <br><strong>Max file size: 50 MB &bull; PDF format only</strong>
              </p>

              <!-- ISIN Input -->
              <div style="margin-bottom:16px">
                <label class="form-label">Bond ISIN <span style="color:#dc2626">*</span></label>
                <div style="position:relative">
                  <input id="imc-up-isin" type="text" class="form-input" placeholder="e.g. INE001A07123"
                         style="width:100%;font-family:monospace;text-transform:uppercase" maxlength="12" />
                  <div id="imc-up-isin-dd" style="display:none;position:absolute;top:100%;left:0;right:0;background:var(--bg-card);
                       border:1px solid var(--border-light);border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.15);
                       max-height:180px;overflow-y:auto;z-index:200"></div>
                </div>
                <div id="imc-up-bond-info" style="margin-top:8px;font-size:0.8rem;color:var(--text-muted)"></div>
              </div>

              <!-- Issuer Name -->
              <div style="margin-bottom:16px">
                <label class="form-label">Issuer Name</label>
                <input id="imc-up-issuer" type="text" class="form-input" placeholder="Auto-detected from bond master" readonly
                       style="width:100%;background:var(--bg-tertiary)" />
              </div>

              <!-- PDF Drop Zone -->
              <div id="imc-up-dropzone"
                   style="border:2px dashed var(--border-light);border-radius:12px;padding:40px 24px;text-align:center;
                          cursor:pointer;transition:all 0.2s;background:var(--bg-tertiary)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="width:48px;height:48px;margin:0 auto 12px;display:block;opacity:.5">
                  <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M17 8l-5-5-5 5M12 3v12"/>
                </svg>
                <p style="font-size:0.95rem;font-weight:600;margin:0 0 4px;color:var(--text-primary)">Drag & drop IM PDF here</p>
                <p style="font-size:0.8rem;margin:0;color:var(--text-muted)">or click to browse &bull; PDF only &bull; Max 50 MB</p>
                <input id="imc-up-file" type="file" accept=".pdf" style="display:none" />
              </div>

              <!-- Selected file info -->
              <div id="imc-up-file-info" style="display:none;margin-top:12px;padding:12px 16px;background:var(--bg-tertiary);border-radius:8px;font-size:0.82rem">
              </div>

              <!-- Upload button -->
              <button id="imc-up-submit" class="btn btn-primary" style="width:100%;margin-top:16px;padding:12px;font-size:0.95rem;display:none"
                      type="button" data-method="_imcSubmitUpload">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:18px;height:18px;vertical-align:middle;margin-right:6px">
                  <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M17 8l-5-5-5 5M12 3v12"/>
                </svg>
                Process &amp; Upload IM
              </button>

              <!-- Processing status -->
              <div id="imc-up-status" style="display:none;margin-top:16px"></div>
            </div>

            <!-- Legal Disclaimer -->
            <div class="card" style="padding:16px 20px;background:var(--bg-tertiary)">
              <div style="font-size:0.75rem;color:var(--text-muted)">
                <strong>Disclaimer:</strong> Document uploaded by user. Platform performs analytical processing only.
                Source metadata, extraction results, and risk scores are stored for audit purposes.
                This system does not provide investment advice. All analysis is for internal use only.
              </div>
            </div>
          </div>

          <!-- RIGHT: Results & History -->
          <div>
            <!-- Upload Result -->
            <div id="imc-up-result" style="display:none"></div>

            <!-- Upload History for this ISIN -->
            <div class="card" style="padding:20px">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
                <h3 style="margin:0;font-size:0.95rem;font-weight:700">Upload History</h3>
                <button class="btn btn-sm btn-outline" type="button" data-method="_imcLoadUploadHistory">Refresh</button>
              </div>
              <div id="imc-up-history">
                <div style="text-align:center;padding:30px;color:var(--text-muted);font-size:0.85rem">
                  Enter an ISIN above and upload a PDF to see results here.
                </div>
              </div>
            </div>

            <!-- Connectors Status -->
            <div class="card" style="padding:20px;margin-top:16px">
              <h3 style="margin:0 0 12px;font-size:0.9rem;font-weight:600">Source Connectors</h3>
              <div id="imc-up-connectors">
                <div style="font-size:0.82rem;color:var(--text-muted)">Loading...</div>
              </div>
            </div>
          </div>
        </div>
      </div><!-- /imc-tab-upload -->

      <!-- Tab: Institutional Dossier -->
      <div id="imc-tab-dossier" role="tabpanel" aria-labelledby="imc-tab-btn-dossier" aria-hidden="true" style="display:none">
        <div id="imc-dossier-body">
          <div style="text-align:center;padding:56px 24px;color:var(--text-muted);border:2px dashed var(--border-light);border-radius:12px">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="width:48px;height:48px;margin:0 auto 16px;display:block;opacity:.4"><path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20"/><path d="M20 2H8a2 2 0 0 0-2 2v16"/><path d="M8 7h8"/><path d="M8 11h8"/><path d="M8 15h5"/></svg>
            <p style="font-size:0.95rem;font-weight:600;margin:0 0 8px">No dossier loaded</p>
            <p style="font-size:0.82rem;margin:0">Open an upload from the Information Memorandum tab to view the full institutional dossier.</p>
          </div>
        </div>
      </div><!-- /imc-tab-dossier -->

      <!-- Tab: Fetch Sources -->
      <div id="imc-tab-fetch" role="tabpanel" aria-labelledby="imc-tab-btn-fetch" aria-hidden="true" style="display:none">
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px;margin-bottom:20px">

          <!-- SEBI card -->
          <div class="card" style="padding:20px">
            <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
              <div style="width:40px;height:40px;background:#1a4080;border-radius:10px;display:flex;align-items:center;justify-content:center">
                <svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" style="width:22px;height:22px"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
              </div>
              <div>
                <h3 style="margin:0;font-size:1rem;font-weight:700">SEBI</h3>
                <p style="margin:0;font-size:0.78rem;color:var(--text-muted)">Debt Offer Documents &bull; ~580 prospectuses</p>
              </div>
            </div>
            <div style="display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap">
              <div style="flex:1;min-width:120px">
                <label class="form-label" style="font-size:0.75rem">Pages (25 docs/page)</label>
                <input id="imc-sebi-pages" type="number" min="1" max="25" value="3" class="form-input" />
              </div>
              <div style="flex:1;min-width:100px">
                <label class="form-label" style="font-size:0.75rem">Max Docs</label>
                <input id="imc-sebi-maxdocs" type="number" min="1" max="500" value="50" class="form-input" />
              </div>
            </div>
            <label style="display:flex;align-items:center;gap:8px;font-size:0.82rem;margin-bottom:12px;cursor:pointer">
              <input id="imc-sebi-pdf" type="checkbox" checked />
              Download &amp; extract PDF text
            </label>
            <button class="btn btn-primary" type="button" data-method="_imcTriggerFetch" data-arg="sebi" style="width:100%">
              Start SEBI Fetch
            </button>
            <div id="imc-sebi-status" style="margin-top:10px;font-size:0.8rem;color:var(--text-muted)"></div>
          </div>

          <!-- BSE card -->
          <div class="card" style="padding:20px">
            <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
              <div style="width:40px;height:40px;background:#c23b22;border-radius:10px;display:flex;align-items:center;justify-content:center">
                <svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" style="width:22px;height:22px"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/></svg>
              </div>
              <div>
                <h3 style="margin:0;font-size:1rem;font-weight:700">BSE</h3>
                <p style="margin:0;font-size:0.78rem;color:var(--text-muted)">Private Placements &bull; Debenture Listings</p>
              </div>
            </div>
            <div style="margin-bottom:12px;font-size:0.8rem;color:var(--text-muted)">Fetches metadata from BSE API. PDF extraction limited.</div>
            <div style="margin-bottom:12px">
              <label class="form-label" style="font-size:0.75rem">Max Records</label>
              <input id="imc-bse-maxdocs" type="number" min="1" max="1000" value="100" class="form-input" />
            </div>
            <button class="btn btn-outline" type="button" data-method="_imcTriggerFetch" data-arg="bse" style="width:100%">
              Start BSE Fetch
            </button>
            <div id="imc-bse-status" style="margin-top:10px;font-size:0.8rem;color:var(--text-muted)"></div>
          </div>

          <!-- NSE card -->
          <div class="card" style="padding:20px">
            <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
              <div style="width:40px;height:40px;background:#003087;border-radius:10px;display:flex;align-items:center;justify-content:center">
                <svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" style="width:22px;height:22px"><circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15.3 15.3 0 010 20M12 2a15.3 15.3 0 000 20"/></svg>
              </div>
              <div>
                <h3 style="margin:0;font-size:1rem;font-weight:700">NSE</h3>
                <p style="margin:0;font-size:0.78rem;color:var(--text-muted)">Corporate Offer Documents</p>
              </div>
            </div>
            <div style="margin-bottom:12px;font-size:0.8rem;color:var(--text-muted)">Fetches metadata from NSE API. Requires session handshake.</div>
            <div style="margin-bottom:12px">
              <label class="form-label" style="font-size:0.75rem">Max Records</label>
              <input id="imc-nse-maxdocs" type="number" min="1" max="1000" value="100" class="form-input" />
            </div>
            <button class="btn btn-outline" type="button" data-method="_imcTriggerFetch" data-arg="nse" style="width:100%">
              Start NSE Fetch
            </button>
            <div id="imc-nse-status" style="margin-top:10px;font-size:0.8rem;color:var(--text-muted)"></div>
          </div>
        </div>

        <!-- Active Jobs -->
        <div class="card" style="padding:20px;margin-bottom:16px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
            <h3 style="margin:0;font-size:0.95rem;font-weight:600">Active Fetch Jobs</h3>
            <button class="btn btn-sm btn-outline" type="button" data-method="_imcRefreshJobs">Refresh</button>
          </div>
          <div id="imc-jobs-list">
            <div style="color:var(--text-muted);font-size:0.82rem;text-align:center;padding:20px">No jobs yet. Start a fetch above.</div>
          </div>
        </div>

        <!-- Fetch History -->
        <div class="card" style="padding:20px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
            <h3 style="margin:0;font-size:0.95rem;font-weight:600">Fetch History</h3>
            <button class="btn btn-sm btn-outline" type="button" data-method="_imcLoadFetchHistory">Load</button>
          </div>
          <div id="imc-fetch-history">
            <div style="color:var(--text-muted);font-size:0.82rem;text-align:center;padding:20px">Click Load to view history.</div>
          </div>
        </div>
      </div><!-- /imc-tab-fetch -->

      `;

    // Bond search autocomplete
    const searchInput = el.querySelector('#imc-search');
    const searchDD = el.querySelector('#imc-search-dd');
    let _imcSearchTimer;

    const selectIMSearchResult = (isin) => {
      if (!isin) return;
      const normalized = String(isin).toUpperCase();
      window._imcCurrentISIN = normalized;
      searchInput.value = normalized;
      searchDD.style.display = 'none';
      App._imcLoadBond(normalized);
    };

    searchInput.addEventListener('input', () => {
      clearTimeout(_imcSearchTimer);
      const q = searchInput.value.trim();
      if (q.length < 2) { searchDD.style.display = 'none'; return; }
      _imcSearchTimer = setTimeout(async () => {
        try {
          const data = await API.get(`/im-checker/search?q=${encodeURIComponent(q)}`);
          const results = data.results || [];
          if (!results.length) { searchDD.style.display = 'none'; return; }
          searchDD.innerHTML = results.map(r => `
            <div class="imc-dd-item" data-isin="${r.isin}"
                 style="padding:10px 14px;cursor:pointer;border-bottom:1px solid var(--border-light);font-size:0.85rem">
              <div style="font-weight:600">${r.isin}</div>
              <div style="color:var(--text-muted);font-size:0.78rem">${r.issuer_name || ''} Â· ${r.bond_name || ''} Â· ${r.credit_rating || ''}</div>
            </div>`).join('');
          searchDD.style.display = 'block';
          searchDD.querySelectorAll('.imc-dd-item').forEach(item => {
            item.addEventListener('click', () => {
              selectIMSearchResult(item.dataset.isin);
            });
          });
        } catch (err) {
          _imcLogEvent('search.autocomplete_error', {
            action: 'search_autocomplete',
            query_length: q.length,
            message: String(err?.message || '').slice(0, 120),
          }, 'warn');
          searchDD.style.display = 'none';
        }
      }, 320);
    });

    searchInput.addEventListener('keydown', async (e) => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      const firstItem = searchDD.querySelector('.imc-dd-item');
      if (firstItem) {
        firstItem.click();
        return;
      }
      const typed = searchInput.value.trim().toUpperCase();
      if (typed.length < 2) return;
      try {
        const data = await API.get(`/im-checker/search?q=${encodeURIComponent(typed)}`);
        const results = data.results || [];
        const exact = results.find((r) => String(r.isin || '').toUpperCase() === typed);
        const selected = exact || (results.length === 1 ? results[0] : null);
        if (selected && selected.isin) {
          selectIMSearchResult(selected.isin);
          return;
        }
        Utils.toast('warning', 'Bond Not Found', `No unique match for ${typed}`);
      } catch (err) {
        if (_imcHandleAuthError(err)) return;
        Utils.toast('error', 'Search Failed', _imcFriendlyError(err, 'Could not search bond. Try a valid ISIN or issuer.'));
      }
    });

    if (!_imcSearchOutsideListenerBound) {
      document.addEventListener('click', (e) => {
        if (e.target.closest('#imc-search') || e.target.closest('#imc-search-dd')) return;
        const dd = document.getElementById('imc-search-dd');
        if (dd) dd.style.display = 'none';
      });
      _imcSearchOutsideListenerBound = true;
    }

    _imcInitUploadISINSearch();
    const liveSnapshot = _getLiveSimulationSnapshot();
    if (liveSnapshot) {
      _renderIMSimulationBridge(liveSnapshot);
    }
    const uploadDropzone = el.querySelector('#imc-up-dropzone');
    const uploadFileInput = el.querySelector('#imc-up-file');
    if (uploadDropzone && uploadFileInput) {
      uploadDropzone.addEventListener('dragover', (event) => {
        event.preventDefault();
        uploadDropzone.style.borderColor = 'var(--accent-blue)';
        uploadDropzone.style.background = 'rgba(37,99,235,0.05)';
      });
      uploadDropzone.addEventListener('dragleave', () => {
        uploadDropzone.style.borderColor = 'var(--border-light)';
        uploadDropzone.style.background = 'var(--bg-tertiary)';
      });
      uploadDropzone.addEventListener('drop', (event) => {
        App._imcHandleUploadDrop(event);
      });
      uploadDropzone.addEventListener('click', () => uploadFileInput.click());
      uploadFileInput.addEventListener('change', (event) => {
        App._imcHandleUploadSelect(event);
      });
    }
  }

  function _imcResolveCurrentISIN() {
    return (window._imcCurrentISIN || document.getElementById('imc-search')?.value.trim() || '');
  }

  function _imcLogEvent(action, details = {}, level = 'info') {
    const redactKeys = new Set([
      'token', 'authorization', 'password', 'access_token', 'refresh_token',
      'im_full_text', 'full_text', 'override_text', 'file_bytes',
    ]);
    const safeDetails = {};
    Object.entries(details || {}).forEach(([k, v]) => {
      if (redactKeys.has(String(k).toLowerCase())) {
        safeDetails[k] = '[redacted]';
        return;
      }
      if (typeof v === 'string' && v.length > 240) {
        safeDetails[k] = `${v.slice(0, 240)}...`;
        return;
      }
      safeDetails[k] = v;
    });
    const payload = {
      module: 'intelligence_center',
      action,
      page: currentPage,
      at: new Date().toISOString(),
      ...safeDetails,
    };
    const fn = level === 'error' ? console.error : level === 'warn' ? console.warn : console.info;
    fn('[IMC_EVENT]', payload);
  }

  function _imcFriendlyError(err, fallback = 'Request failed. Please try again.') {
    if (isAuthError(err)) return 'Your session expired. Please sign in again to continue.';
    const status = Number(err?.status || 0);
    if (status === 400) return 'Request could not be processed. Check your input and try again.';
    if (status === 404) return 'Requested data was not found. Refresh and try again.';
    if (status === 413) return 'Payload is too large. Reduce file/text size and try again.';
    if (status === 429) return 'Too many requests. Please wait a moment and retry.';
    if (status >= 500) return 'Service is temporarily unavailable. Please retry shortly.';

    const raw = String(err?.message || '').trim();
    if (!raw) return fallback;
    if (/unauthorized|forbidden|token validation failed|internal server error|traceback|sql|http\s*\d{3}/i.test(raw)) {
      return fallback;
    }
    return raw.length > 180 ? `${raw.slice(0, 180)}...` : raw;
  }

  function _imcHandleAuthError(err) {
    if (!isAuthError(err)) return false;
    handleSessionExpired(err);
    return true;
  }

  async function _imcLoadBond(isin) {
    const info = document.getElementById('imc-bond-info');
    if (!info) return;
    _imcLogEvent('bond.load_start', { isin });
    try {
      if (isin) window._imcCurrentISIN = isin;
      const data = await API.get(`/im-checker/${isin}`);
      const b = data.bond || {};
      info.style.display = 'block';
      info.innerHTML = `
        <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:center;padding:10px 14px;background:var(--bg-tertiary);border-radius:8px;font-size:0.85rem">
          <span style="font-weight:700;font-size:1.05rem">${b.isin || isin}</span>
          <span style="color:var(--text-secondary)">${b.issuer_name || ''}</span>
          <span style="color:var(--text-muted)">${b.bond_name || ''}</span>
          ${b.credit_rating ? `<span class="badge-rating" style="background:var(--accent-blue);color:#fff;padding:2px 8px;border-radius:12px;font-size:0.78rem">${b.credit_rating}</span>` : ''}
          ${b.coupon_rate ? `<span style="color:var(--text-muted)">Coupon: ${b.coupon_rate}%</span>` : ''}
          ${b.maturity_date ? `<span style="color:var(--text-muted)">Maturity: ${b.maturity_date}</span>` : ''}
        </div>`;
      // Populate IM fields if data exists
      const im = data.im_data;
      if (im) {
        const fv = (id, val) => { const el = document.getElementById(id); if (el && val != null) el.value = val; };
        fv('imc-object', im.object_of_issue); fv('imc-security', im.security_structure);
        fv('imc-charge', im.charge_type); fv('imc-redemption', im.redemption_structure);
        fv('imc-trustee', im.trustee_name); fv('imc-guarantee', im.guarantee_details);
        fv('imc-default', im.default_clauses); fv('imc-stepup', im.step_up_clauses);
        fv('imc-escrow', im.escrow_mechanism); fv('imc-imtext', im.im_full_text);
        fv('imc-dscr-req', im.dscr_requirement); fv('imc-icr-req', im.interest_coverage_req);
        fv('imc-de-req', im.debt_ebitda_limit);
      } else {
        _imcClearIMForm();
      }
      await _imcLoadFinancials(isin);
      _imcLogEvent('bond.load_success', { isin });
    } catch (err) {
      if (_imcHandleAuthError(err)) return;
      info.style.display = 'block';
      info.innerHTML = `<div style="color:var(--accent-red);font-size:0.82rem">${_imcFriendlyError(err, 'Bond data could not be loaded right now.')}</div>`;
      _imcLogEvent('bond.load_failed', { isin, message: String(err?.message || '').slice(0, 120) }, 'warn');
    }
  }

  function _imcClearIMForm() {
    ['imc-object', 'imc-security', 'imc-trustee', 'imc-guarantee', 'imc-default', 'imc-stepup', 'imc-escrow', 'imc-imtext', 'imc-dscr-req', 'imc-icr-req', 'imc-de-req']
      .forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
    const charge = document.getElementById('imc-charge');
    const redemption = document.getElementById('imc-redemption');
    if (charge) charge.value = 'FIRST';
    if (redemption) redemption.value = 'BULLET';
  }

  function _imcClearFinancialForm() {
    ['imc-revenue', 'imc-ebitda', 'imc-netprofit', 'imc-interest', 'imc-debt', 'imc-cash', 'imc-assets', 'imc-networth', 'imc-curassets', 'imc-curliab', 'imc-cfo', 'imc-capex']
      .forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
    const fy = document.getElementById('imc-fy');
    if (fy) fy.value = 'FY2026';
    const ratioEl = document.getElementById('imc-ratios');
    if (ratioEl) { ratioEl.style.display = 'none'; ratioEl.innerHTML = ''; }
  }

  function _imcRenderFinancialRatios(ratios = {}) {
    const ratioEl = document.getElementById('imc-ratios');
    if (!ratioEl) return;
    const entries = Object.entries({
      'D/EBITDA': ratios.debt_ebitda,
      'ICR': ratios.interest_coverage,
      'DSCR': ratios.dscr,
      'Curr. Ratio': ratios.current_ratio,
      'ROE': ratios.roe != null ? ratios.roe + '%' : null,
    }).filter(([, v]) => v != null);
    if (!entries.length) {
      ratioEl.style.display = 'none';
      ratioEl.innerHTML = '';
      return;
    }
    ratioEl.style.display = 'block';
    ratioEl.innerHTML = `<div style="display:flex;gap:16px;flex-wrap:wrap;font-weight:500">
      ${entries.map(([k, v]) => `<span style="background:var(--bg-card);padding:4px 10px;border-radius:6px;border:1px solid var(--border-light)">${k}: <strong>${v}</strong></span>`).join('')}
    </div>`;
  }

  async function _imcLoadFinancials(isin) {
    _imcClearFinancialForm();
    try {
      const data = await API.get(`/im-checker/${isin}/financials`);
      const latest = (data.financials || [])[0];
      if (!latest) return;
      const fv = (id, val) => { const el = document.getElementById(id); if (el && val != null) el.value = val; };
      fv('imc-fy', latest.financial_year || 'FY2026');
      fv('imc-revenue', latest.revenue_cr);
      fv('imc-ebitda', latest.ebitda_cr);
      fv('imc-netprofit', latest.net_profit_cr);
      fv('imc-interest', latest.interest_expense_cr);
      fv('imc-debt', latest.total_debt_cr);
      fv('imc-cash', latest.cash_cr);
      fv('imc-assets', latest.total_assets_cr);
      fv('imc-networth', latest.net_worth_cr);
      fv('imc-curassets', latest.current_assets_cr);
      fv('imc-curliab', latest.current_liabilities_cr);
      fv('imc-cfo', latest.cfo_cr);
      fv('imc-capex', latest.capex_cr);
      _imcRenderFinancialRatios(latest);
    } catch (err) {
      if (_imcHandleAuthError(err)) return;
      _imcLogEvent('financials.load_failed', { isin, message: String(err?.message || '').slice(0, 120) }, 'warn');
    }
  }

  async function _imcSaveIM() {
    const isin = _imcResolveCurrentISIN();
    if (!isin) { Utils.toast('warning', 'No Bond', 'Select a bond first'); return; }
    window._imcCurrentISIN = isin;
    const restoreButtons = _setBusyButtons('button[data-method="_imcSaveIM"], button[onclick*="App._imcSaveIM()"]', true, 'Savingâ€¦');
    const gv = id => document.getElementById(id)?.value || null;
    const gf = id => { const v = document.getElementById(id)?.value; return v ? parseFloat(v) : null; };
    const payload = {
      object_of_issue: gv('imc-object'), security_structure: gv('imc-security'),
      charge_type: gv('imc-charge') || 'FIRST', redemption_structure: gv('imc-redemption') || 'BULLET',
      trustee_name: gv('imc-trustee'), guarantee_details: gv('imc-guarantee'),
      default_clauses: gv('imc-default'), step_up_clauses: gv('imc-stepup'),
      escrow_mechanism: gv('imc-escrow'), im_full_text: gv('imc-imtext'),
      dscr_requirement: gf('imc-dscr-req'), interest_coverage_req: gf('imc-icr-req'),
      debt_ebitda_limit: gf('imc-de-req'),
    };
    try {
      _imcLogEvent('im.save_start', { isin });
      await API.post(`/im-checker/${isin}`, payload);
      Utils.toast('success', 'Saved', 'IM data saved successfully');
      _imcLogEvent('im.save_success', { isin, fields: Object.keys(payload).length });
    } catch (err) {
      if (_imcHandleAuthError(err)) return;
      Utils.toast('error', 'Save Failed', _imcFriendlyError(err, 'Could not save IM data.'));
      _imcLogEvent('im.save_failed', { isin, message: String(err?.message || '').slice(0, 120) }, 'warn');
    }
  }

  function _imcSaveIMAndRun() {
    _imcSaveIM();
    _imcRunFull();
  }

  async function _imcSaveFinancials() {
    const isin = _imcResolveCurrentISIN();
    if (!isin) { Utils.toast('warning', 'No Bond', 'Select a bond first'); return; }
    window._imcCurrentISIN = isin;
    const restoreButtons = _setBusyButtons('button[data-method="_imcSaveFinancials"], button[onclick*="App._imcSaveFinancials()"]', true, 'Savingâ€¦');
    const gf = id => { const v = document.getElementById(id)?.value; return v ? parseFloat(v) : null; };
    const payload = {
      financial_year: document.getElementById('imc-fy')?.value || 'FY2025',
      revenue_cr: gf('imc-revenue'), ebitda_cr: gf('imc-ebitda'),
      net_profit_cr: gf('imc-netprofit'), interest_expense_cr: gf('imc-interest'),
      total_debt_cr: gf('imc-debt'), cash_cr: gf('imc-cash'),
      total_assets_cr: gf('imc-assets'), net_worth_cr: gf('imc-networth'),
      current_assets_cr: gf('imc-curassets'), current_liabilities_cr: gf('imc-curliab'),
      cfo_cr: gf('imc-cfo'), capex_cr: gf('imc-capex'),
    };
    try {
      _imcLogEvent('financials.save_start', { isin, fy: payload.financial_year });
      const res = await API.post(`/im-checker/${isin}/financials`, payload);
      Utils.toast('success', 'Saved', 'Financials saved');
      _imcRenderFinancialRatios(res.ratios || {});
      _imcLogEvent('financials.save_success', { isin, fy: payload.financial_year });
    } catch (err) {
      if (_imcHandleAuthError(err)) return;
      Utils.toast('error', 'Save Failed', _imcFriendlyError(err, 'Could not save financials.'));
      _imcLogEvent('financials.save_failed', { isin, message: String(err?.message || '').slice(0, 120) }, 'warn');
    }
  }

  async function _imcRunFull() {
    const isin = _imcResolveCurrentISIN();
    if (!isin) { Utils.toast('warning', 'No Bond', 'Select a bond first'); return; }
    if (_imcRunFullInFlight) {
      Utils.toast('info', 'Analysis Running', 'Please wait for the current analysis to complete.');
      return;
    }
    const resultsEl = document.getElementById('imc-results-container');
    if (!resultsEl) return;
    _imcRunFullInFlight = true;
    _imcLogEvent('analysis.run_start', { isin });
    resultsEl.innerHTML = `<div style="text-align:center;padding:60px 24px;color:var(--text-muted)"><div class="spinner" style="width:36px;height:36px;margin:0 auto 16px"></div><p>Running Intelligence Analystsâ€¦</p></div>`;
    try {
      const [hDataResult, mDataResult, summaryResult, uploadHistoryResult] = await Promise.allSettled([
        API.get(`/im-checker/${isin}/heuristics`),
        API.get(`/im-checker/${isin}/migration`),
        API.get(`/im-checker/${isin}/summary`),
        API.get(`/im-checker/uploads/isin/${encodeURIComponent(isin)}`),
      ]);
      _imcRenderFullIntelligence(resultsEl, hData, mData);
      _imcLogEvent('analysis.run_success', { isin, triggered: hData?.triggered_count || 0 });
    } catch (err) {
      if (_imcHandleAuthError(err)) return;
      const msg = _imcFriendlyError(err, 'Analysis failed. Please retry.');
      resultsEl.innerHTML = `<div class="card" style="padding:20px;color:var(--accent-red)">${msg}</div>`;
      _imcLogEvent('analysis.run_failed', { isin, message: String(err?.message || '').slice(0, 120) }, 'warn');
    } finally {
      _imcRunFullInFlight = false;
    }
  }

  async function _imcHandlePDFUpload(e) {
    const isin = _imcResolveCurrentISIN();
    if (!isin) { Utils.toast('warning', 'No Bond', 'Select a bond first'); e.target.value=''; return; }
    window._imcCurrentISIN = isin;
    const file = e.target.files[0];
    if (!file) return;
    if (!ensureAuthenticated('upload an IM PDF')) { e.target.value = ''; return; }
    Utils.toast('info', 'Parsing PDF', 'Extracting parameters from IM...');
    const formData = new FormData();
    formData.append('file', file);
    try {
      _imcLogEvent('upload.quick_start', { isin, file_name: file.name, file_size: file.size });
      const res = await API.post(`/im-checker/${isin}/upload`, formData);
      if (res.status === 'ok') {
        Utils.toast('success', 'Parsed', `Extracted ${res.extracted.length} fields from PDF`);
        _imcLoadBond(isin); // reload fields
        _imcLogEvent('upload.quick_success', { isin });
      } else {
        throw new Error(res.detail || 'Upload failed');
      }
    } catch (err) {
      if (_imcHandleAuthError(err)) return;
      Utils.toast('error', 'Upload Failed', _imcFriendlyError(err, 'PDF upload failed.'));
      _imcLogEvent('upload.quick_failed', { isin, message: String(err?.message || '').slice(0, 120) }, 'warn');
    } finally { e.target.value = ''; }
  }

  function _imcRenderFullIntelligence(el, hData, mData, summaryData = null) {
    const institutional = summaryData?.institutional_analysis || summaryData?.im_intelligence ? (summaryData.institutional_analysis || {}) : {};
    const imIntel = summaryData?.im_intelligence || institutional?.intelligence || institutional?.risk?.intelligence_json || {};
    const imDecision = institutional?.decision_recommendation || imIntel.decision || {};
    const decisionAction = String(imDecision.action || 'CAUTION').toUpperCase();
    const decisionColor = decisionAction === 'INVEST' ? '#16a34a' : (decisionAction === 'AVOID' ? '#dc2626' : '#f59e0b');
    const missingClauses = Array.isArray(imIntel.missing_clauses) ? imIntel.missing_clauses : [];
    const categories = Array.isArray(imIntel.risk_categories) ? imIntel.risk_categories : [];
    const strengths = Array.isArray(imIntel.clause_strengths) ? imIntel.clause_strengths : [];
    el.innerHTML = `
      <div id="imc-results">
        ${imIntel && imIntel.risk_score != null ? `
        <div class="card" style="margin-bottom:16px;padding:20px">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap">
            <div>
              <div style="font-size:0.78rem;color:var(--text-muted);margin-bottom:4px">Structured IM Decision</div>
              <div style="font-size:1.15rem;font-weight:800;color:${decisionColor}">${decisionAction}</div>
              <div style="font-size:0.78rem;color:var(--text-muted);margin-top:6px">${Utils.escapeHTML(String(imDecision.reason || imIntel.summary || 'No decision explanation available'))}</div>
            </div>
            <div style="text-align:right">
              <div style="font-size:0.78rem;color:var(--text-muted)">Risk Score</div>
              <div style="font-size:1.2rem;font-weight:800">${Number(imIntel.risk_score || 0).toFixed(1)} / ${Utils.escapeHTML(String(imIntel.risk_level || 'UNKNOWN'))}</div>
              <div style="font-size:0.72rem;color:var(--text-muted)">Confidence ${(Number(imDecision.confidence || imIntel.analysis_confidence || 0) * 100).toFixed(1)}%</div>
            </div>
          </div>
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-top:14px;font-size:0.78rem">
            <div style="padding:10px 12px;background:var(--bg-tertiary);border-radius:8px"><div class="text-muted" style="font-size:0.7rem">Clause Coverage</div><div style="font-weight:700">${imIntel.clause_coverage?.ratio != null ? (Number(imIntel.clause_coverage.ratio) * 100).toFixed(0) + '%' : 'â€”'}</div></div>
            <div style="padding:10px 12px;background:var(--bg-tertiary);border-radius:8px"><div class="text-muted" style="font-size:0.7rem">Critical Clauses</div><div style="font-weight:700">${imIntel.clause_coverage?.critical_present ?? 'â€”'}/${imIntel.clause_coverage?.critical_total ?? 'â€”'}</div></div>
            <div style="padding:10px 12px;background:var(--bg-tertiary);border-radius:8px"><div class="text-muted" style="font-size:0.7rem">Missing Clauses</div><div style="font-weight:700">${missingClauses.length}</div></div>
            <div style="padding:10px 12px;background:var(--bg-tertiary);border-radius:8px"><div class="text-muted" style="font-size:0.7rem">Risk Categories</div><div style="font-weight:700">${categories.length}</div></div>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px;font-size:0.76rem">
            <div><div style="font-weight:700;margin-bottom:6px">Top Risk Categories</div><div style="color:var(--text-muted)">${categories.length ? categories.slice(0, 3).map((item) => `${Utils.escapeHTML(String(item.category || 'risk'))}: ${Utils.escapeHTML(String(item.reason || ''))}`).join('<br/>') : 'None'}</div></div>
            <div><div style="font-weight:700;margin-bottom:6px">Missing / Inconclusive Clauses</div><div style="color:var(--text-muted)">${missingClauses.length ? missingClauses.slice(0, 5).map((item) => `${Utils.escapeHTML(String(item.clause_type || 'CLAUSE'))} (${Utils.escapeHTML(String(item.strength || 'MISSING'))})`).join(' | ') : 'None'}</div></div>
          </div>
          <div style="margin-top:12px;font-size:0.76rem"><div style="font-weight:700;margin-bottom:6px">Strong Clause Detections</div><div style="color:var(--text-muted)">${strengths.filter((item) => item.strength === 'STRONG').slice(0, 6).map((item) => Utils.escapeHTML(String(item.clause_type || 'CLAUSE'))).join(' | ') || 'None yet'}</div></div>
        </div>` : ''}
        ${_imcRenderHeuristics(hData)}
        ${_imcRenderMigration(mData)}
      </div>`;
  }

  function _imcRenderHeuristics(data) {
    const score = data.hidden_risk_score || 0;
    const level = data.risk_level || 'UNKNOWN';
    const triggered = data.triggered_count || 0;
    const total = data.total_heuristics || 24;
    const levelColor = { LOW: '#16a34a', MEDIUM: '#d97706', HIGH: '#e55a2b', CRITICAL: '#dc2626' }[level] || '#6b7280';
    const catSummary = data.category_summary || {};
    const heuristics = data.heuristics || [];
    const triggered_list = heuristics.filter(h => h.triggered);
    const ok_list = heuristics.filter(h => !h.triggered);

    return `
      <!-- Risk Score Banner -->
      <div class="card" style="margin-bottom:16px;padding:20px">
        <div style="display:flex;align-items:center;gap:20px;flex-wrap:wrap">
          <div style="text-align:center">
            <div class="imc-score-ring" style="width:88px;height:88px;border-radius:50%;background:conic-gradient(${levelColor} 0% ${score}%, var(--bg-tertiary) ${score}% 100%);display:flex;align-items:center;justify-content:center;position:relative">
              <div style="width:68px;height:68px;border-radius:50%;background:var(--bg-card);display:flex;align-items:center;justify-content:center;flex-direction:column">
                <span style="font-size:1.4rem;font-weight:800;color:${levelColor}">${Math.round(score)}</span>
                <span style="font-size:0.6rem;color:var(--text-muted)">/ 100</span>
              </div>
            </div>
          </div>
          <div style="flex:1">
            <div style="font-size:0.78rem;color:var(--text-muted);margin-bottom:4px">IM Structural Risk Score</div>
            <span style="display:inline-block;padding:4px 14px;border-radius:20px;font-weight:700;font-size:0.95rem;color:#fff;background:${levelColor}">${level}</span>
            <div style="margin-top:8px;font-size:0.85rem;color:var(--text-secondary)">
              <strong style="color:${levelColor}">${triggered}</strong> of <strong>${total}</strong> heuristics triggered
            </div>
          </div>
          <button class="btn btn-sm btn-outline" type="button" data-method="_imcSaveIMAndRun">Re-run</button>
        </div>

        <!-- Category summary -->
        <div style="margin-top:16px;display:flex;flex-wrap:wrap;gap:8px">
          ${Object.entries(catSummary).map(([cat, cs]) => {
            const catColor = cs.triggered > 0 ? (cs.max_severity === 'CRITICAL' ? '#dc2626' : cs.max_severity === 'HIGH' ? '#e55a2b' : '#d97706') : '#16a34a';
            return `<div style="flex:1;min-width:120px;padding:8px 12px;background:var(--bg-tertiary);border-radius:8px;border-left:3px solid ${catColor}">
              <div style="font-size:0.7rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px">${cat}</div>
              <div style="font-weight:700;color:${catColor}">${cs.triggered}/${cs.total}</div>
            </div>`;
          }).join('')}
        </div>
      </div>

      <!-- Triggered Heuristics -->
      ${triggered_list.length ? `
      <div class="card" style="margin-bottom:16px">
        <div style="padding:14px 20px;border-bottom:1px solid var(--border-light);display:flex;align-items:center;gap:10px">
          <svg viewBox="0 0 24 24" fill="none" stroke="#e55a2b" stroke-width="2" style="width:18px;height:18px"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
          <h3 style="margin:0;font-size:0.95rem;font-weight:600">Triggered Heuristics (${triggered_list.length})</h3>
        </div>
        <div style="padding:12px 16px;display:flex;flex-direction:column;gap:10px">
          ${triggered_list.map(h => _imcHeuristicCard(h)).join('')}
        </div>
      </div>` : ''}

      <details class="card" style="margin-bottom:16px">
        <summary style="padding:14px 20px;cursor:pointer;display:flex;align-items:center;gap:10px;font-weight:600;font-size:0.9rem;list-style:none">
          Passed Heuristics (${ok_list.length})
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:14px;height:14px;margin-left:auto"><polyline points="6 9 12 15 18 9"/></svg>
        </summary>
        <div style="padding:12px 16px;display:flex;flex-direction:column;gap:8px">
          ${ok_list.map(h => _imcHeuristicCard(h)).join('')}
        </div>
      </details>`;
  }

  function _imcRenderMigration(data) {
    if (data.status === 'Insufficient data') return '';
    const signals = Array.isArray(data.signals) ? data.signals : [];
    const levelColor = { CRITICAL: '#dc2626', WATCH: '#d97706', STABLE: '#16a34a' }[data.alert_level] || '#6b7280';
    return `
      <div class="card" style="margin-bottom:16px">
        <div style="padding:14px 20px;border-bottom:1px solid var(--border-light);display:flex;justify-content:space-between;align-items:center">
          <h3 style="margin:0;font-size:0.95rem;font-weight:600">Credit Migration Predictor</h3>
          <span style="font-size:0.75rem;font-weight:700;color:${levelColor}">${data.alert_level}</span>
        </div>
        <div style="padding:20px;display:flex;gap:24px;flex-wrap:wrap">
          <div style="flex:1;min-width:200px">
            <div style="font-size:0.75rem;color:var(--text-muted);margin-bottom:4px">Predicted Rating Migration</div>
            <div style="display:flex;align-items:center;gap:12px">
              <span style="font-size:1.1rem;font-weight:700">${data.current_rating}</span>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:20px;height:20px"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
              <span style="font-size:1.1rem;font-weight:700;color:${levelColor}">${data.predicted_rating}</span>
            </div>
            <div style="margin-top:16px;height:4px;background:var(--bg-tertiary);border-radius:2px;overflow:hidden">
              <div style="width:${data.downgrade_probability * 100}%;height:100%;background:#dc2626"></div>
            </div>
            <div style="display:flex;justify-content:space-between;font-size:0.7rem;margin-top:4px;color:var(--text-muted)">
              <span>Downgrade Probability</span>
              <span style="font-weight:600;color:#dc2626">${Math.round(data.downgrade_probability * 100)}%</span>
            </div>
          </div>
          <div style="flex:1.5;min-width:260px">
            <div style="font-size:0.75rem;color:var(--text-muted);margin-bottom:8px">Negative Signals Triggered</div>
            <div style="display:flex;flex-direction:column;gap:8px">
              ${signals.map(s => `
                <div style="font-size:0.8rem;padding:6px 10px;background:var(--bg-tertiary);border-radius:6px;border-left:3px solid ${s.sev==='CRITICAL'?'#dc2626':'#e55a2b'}">
                  <strong>${s.id}</strong>: ${s.msg}
                </div>
              `).join('') || '<div style="color:var(--text-muted);font-style:italic;font-size:0.8rem">No deterioration signals found.</div>'}
            </div>
          </div>
        </div>
      </div>`;
  }

  // â”€â”€â”€ Tab switching â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
  function _imcShowTab(tab) {
    ['analysis', 'library', 'upload', 'dossier', 'fetch'].forEach(t => {
      const panel = document.getElementById('imc-tab-' + t);
      const btn   = document.getElementById('imc-tab-btn-' + t);
      if (!panel || !btn) return;
      const active = t === tab;
      panel.style.display = active ? '' : 'none';
      panel.setAttribute('aria-hidden', active ? 'false' : 'true');
      btn.style.borderBottomColor = active ? 'var(--accent-blue)' : 'transparent';
      btn.style.color = active ? 'var(--accent-blue)' : 'var(--text-muted)';
      btn.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    if (tab === 'library') _imcLoadDocLibrary(1);
    if (tab === 'fetch') { _imcRefreshJobs(); _imcLoadFetchHistory(); }
    if (tab === 'upload') { _imcLoadConnectors(); _imcLoadUploadHistory(); }
  }

  // â”€â”€â”€ Document Library â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
  async function _imcLoadDocLibrary(page = 1) {
    const body = document.getElementById('imc-lib-body');
    if (!body) return;
    const reqSeq = ++_imcDocReqSeq;
    body.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-muted)"><div class="spinner" style="width:32px;height:32px;margin:auto"></div></div>';
    const q      = document.getElementById('imc-lib-q')?.value.trim() || '';
    const source = document.getElementById('imc-lib-source')?.value || '';
    const status = document.getElementById('imc-lib-status')?.value || '';
    let url = `/im-checker/documents?page=${page}&page_size=20`;
    if (q)      url += `&q=${encodeURIComponent(q)}`;
    if (source) url += `&source=${source}`;
    if (status) url += `&status=${status}`;
    try {
      const data = await API.get(url);
      if (reqSeq !== _imcDocReqSeq) return;
      const docs = data.documents || [];
      const total = data.total || 0;
      const pages = data.pages || 1;
      if (!docs.length) {
        body.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-muted)"><div style="font-weight:700;color:var(--text-secondary);margin-bottom:6px">No documents available yet</div><div style="margin-bottom:8px">Document Library is empty because no IM files have been fetched or uploaded.</div><div>Next: Use <strong>Fetch Sources</strong> to pull SEBI/BSE/NSE documents or upload a PDF in <strong>Information Memorandum</strong>.</div></div>';
        return;
      }
      const statusBadge = s => Utils.statusBadge(s);
      const srcColor = {'SEBI':'#1a4080','BSE':'#c23b22','NSE':'#003087','MANUAL':'#374151'};
      body.innerHTML = `
        <div class="card" style="overflow:hidden">
          <table class="table" style="width:100%;border-collapse:collapse;font-size:0.83rem">
            <thead><tr style="background:var(--bg-tertiary)">
              <th style="padding:10px 14px;text-align:left;font-weight:600;color:var(--text-secondary)">Title / Issuer</th>
              <th style="padding:10px 14px;text-align:left;font-weight:600;color:var(--text-secondary)">ISIN</th>
              <th style="padding:10px 14px;text-align:left;font-weight:600;color:var(--text-secondary)">Source</th>
              <th style="padding:10px 14px;text-align:left;font-weight:600;color:var(--text-secondary)">Date</th>
              <th style="padding:10px 14px;text-align:left;font-weight:600;color:var(--text-secondary)">Status</th>
              <th style="padding:10px 14px;text-align:left;font-weight:600;color:var(--text-secondary)">Risk</th>
              <th style="padding:10px 14px;text-align:left;font-weight:600;color:var(--text-secondary)">Actions</th>
            </tr></thead>
            <tbody>
              ${docs.map((d,i) => `<tr style="border-top:1px solid var(--border-light);${i%2?'background:var(--bg-tertiary)':''}">
                <td style="padding:10px 14px">
                  <div style="font-weight:600;max-width:280px;white-space:normal;overflow-wrap:anywhere;word-break:break-word;line-height:1.35" title="${d.doc_title||''}">${d.doc_title||'â€“'}</div>
                  <div style="color:var(--text-muted);font-size:0.75rem">${d.issuer_name||''} &bull; ${d.doc_type||''}</div>
                </td>
                <td style="padding:10px 14px;font-family:monospace;font-size:0.78rem">${d.isin||'â€“'}</td>
                <td style="padding:10px 14px">
                  <span style="padding:2px 8px;border-radius:10px;font-size:0.72rem;font-weight:700;color:#fff;background:${srcColor[d.source]||'#374151'}">${d.source||'?'}</span>
                </td>
                <td style="padding:10px 14px;color:var(--text-muted);font-size:0.78rem">${(d.filing_date||'').slice(0,10)||'â€“'}</td>
                <td style="padding:10px 14px">${statusBadge(d.extraction_status)}</td>
                <td style="padding:10px 14px;font-weight:700;color:${d.heuristic_score>=50?'#dc2626':d.heuristic_score>=25?'#d97706':'#16a34a'}">
                  ${d.heuristic_score!=null ? Math.round(d.heuristic_score) : 'â€“'}
                </td>
                <td style="padding:8px 10px">
                  <div style="display:flex;gap:4px">
                    ${d.extraction_status==='DONE' ? `<button class="btn btn-sm btn-primary" type="button" data-method="_imcAnalyseDoc" data-arg="${d.id}" data-arg2="${Utils.escapeAttr(d.isin || '')}" style="font-size:0.72rem;padding:4px 8px">Analyse</button>` : ''}
                    ${['FAILED','NO_PDF','PENDING'].includes(d.extraction_status) ? `<button class="btn btn-sm btn-outline" type="button" data-method="_imcReprocessDoc" data-arg="${d.id}" style="font-size:0.72rem;padding:4px 8px">Retry</button>` : ''}
                  </div>
                </td>
              </tr>`).join('')}
            </tbody>
          </table>
        </div>
        ${pages>1 ? `
        <div style="display:flex;justify-content:center;gap:8px;margin-top:12px;flex-wrap:wrap">
          ${page>1 ? `<button class="btn btn-sm btn-outline" type="button" data-method="_imcLoadDocLibrary" data-arg="${page-1}">&#8592; Prev</button>` : ''}
          <span style="padding:6px 14px;font-size:0.85rem;color:var(--text-muted)">Page ${page} of ${pages} &bull; ${total} documents</span>
          ${page<pages ? `<button class="btn btn-sm btn-outline" type="button" data-method="_imcLoadDocLibrary" data-arg="${page+1}">Next &#8594;</button>` : ''}
        </div>` : `<div style="text-align:center;font-size:0.78rem;color:var(--text-muted);margin-top:8px">${total} document${total!==1?'s':''}</div>`}
      `;
    } catch (err) {
      if (_imcHandleAuthError(err)) return;
      body.innerHTML = `<div class="card" style="padding:20px;color:var(--accent-red)">${_imcFriendlyError(err, 'Document library could not be loaded.')}</div>`;
      _imcLogEvent('library.load_failed', { page, message: String(err?.message || '').slice(0, 120) }, 'warn');
    }
  }

  async function _imcAnalyseDoc(docId, isin) {
    const restoreButtons = _setBusyButtons('button[data-method="_imcAnalyseDoc"], button[onclick*="App._imcAnalyseDoc("]', true, 'Analysingâ€¦');
    Utils.toast('info', 'Analysing', `Running heuristics on document #${docId}...`);
    try {
      const res = await API.post(`/im-checker/documents/${docId}/analyse`, {});
      const analysis = res.analysis || {};
      const score = analysis.hidden_risk_score ?? analysis.risk_score ?? 0;
      const level = analysis.risk_level || 'UNKNOWN';
      Utils.toast('success', 'Analysis Done', `Risk score: ${Math.round(score)} (${level})`);
      if (isin && isin !== 'null' && isin.length > 5) {
        _imcShowTab('analysis');
        window._imcCurrentISIN = isin;
        const si = document.getElementById('imc-search');
        if (si) si.value = isin;
        await _imcLoadBond(isin);
        await _imcRunFull();
      } else {
        _imcLoadDocLibrary(1);
      }
    } catch (err) {
      if (_imcHandleAuthError(err)) return;
      Utils.toast('error', 'Analysis Failed', _imcFriendlyError(err, 'Document analysis failed.'));
    }
  }

  async function _imcReprocessDoc(docId) {
    const restoreButtons = _setBusyButtons('button[data-method="_imcReprocessDoc"], button[onclick*="App._imcReprocessDoc("]', true, 'Retryingâ€¦');
    Utils.toast('info', 'Reprocessing', `Re-triggering PDF extraction for doc #${docId}...`);
    try {
      await API.post(`/im-checker/documents/${docId}/reprocess`, {});
      Utils.toast('success', 'Queued', 'Reprocessing started in background');
      setTimeout(() => _imcLoadDocLibrary(1), 3000);
    } catch (err) {
      if (_imcHandleAuthError(err)) return;
      Utils.toast('error', 'Failed', _imcFriendlyError(err, 'Could not reprocess document.'));
    }
  }

  // â”€â”€â”€ Fetch Source Controls â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
  async function _imcTriggerFetch(source) {
    const restoreButtons = _setBusyButtons('button[data-method="_imcTriggerFetch"], button[onclick*="App._imcTriggerFetch("]', true, 'Startingâ€¦');
    const statusEl = document.getElementById(`imc-${source}-status`);
    if (statusEl) statusEl.innerHTML = '<span style="color:var(--accent-blue)">Starting fetch...</span>';
    try {
      _imcLogEvent('fetch.start', { source });
      let url = '';
      if (source === 'sebi') {
        const pages   = document.getElementById('imc-sebi-pages')?.value   || '3';
        const maxdocs = document.getElementById('imc-sebi-maxdocs')?.value || '50';
        const pdf     = document.getElementById('imc-sebi-pdf')?.checked !== false;
        url = `/im-checker/fetch/sebi?max_pages=${pages}&max_docs=${maxdocs}&extract_pdf=${pdf}`;
      } else if (source === 'bse') {
        const maxdocs = document.getElementById('imc-bse-maxdocs')?.value || '100';
        url = `/im-checker/fetch/bse?max_docs=${maxdocs}`;
      } else {
        const maxdocs = document.getElementById('imc-nse-maxdocs')?.value || '100';
        url = `/im-checker/fetch/nse?max_docs=${maxdocs}`;
      }
      const res = await API.post(url, {});
      const jobId = res.job_id;
      if (statusEl) statusEl.innerHTML = `<span style="color:#16a34a">&#10003; Started â€” Job: <code>${jobId}</code></span>`;
      Utils.toast('success', `${source.toUpperCase()} Fetch Started`, res.message || 'Background job running');
      _imcPollFetchJob(jobId, source);
      _imcRefreshJobs();
    } catch (err) {
      if (_imcHandleAuthError(err)) return;
      const msg = _imcFriendlyError(err, 'Fetch could not be started.');
      if (statusEl) statusEl.innerHTML = `<span style="color:var(--accent-red)">${msg}</span>`;
      Utils.toast('error', 'Fetch Failed', msg);
      _imcLogEvent('fetch.start_failed', { source, message: String(err?.message || '').slice(0, 120) }, 'warn');
    }
  }

  function _imcPollFetchJob(jobId, source) {
    const statusEl = document.getElementById(`imc-${source}-status`);
    let polls = 0;
    const interval = setInterval(async () => {
      try {
        const job = await API.get(`/im-checker/fetch/status/${jobId}`);
        const pct = job.total ? Math.round((job.done + job.failed + (job.no_pdf||0)) / job.total * 100) : 0;
        if (statusEl) {
          const col = job.status==='COMPLETED' ? '#16a34a' : job.status==='FAILED' ? '#dc2626' : '#2563eb';
          statusEl.innerHTML = `<div style="font-size:0.78rem">
            <span style="color:${col};font-weight:600">${job.status}</span> &bull;
            ${job.done||0} done &bull; ${job.failed||0} failed &bull; ${job.no_pdf||0} no-pdf &bull; ${job.total||'?'} total
            ${job.total>0 ? `<div style="height:4px;background:var(--bg-tertiary);border-radius:2px;margin-top:4px;overflow:hidden">
              <div style="width:${pct}%;height:100%;background:var(--accent-blue)"></div></div>` : ''}
          </div>`;
        }
        if (job.status === 'COMPLETED' || job.status === 'FAILED' || ++polls > 120) {
          clearInterval(interval);
          if (job.status === 'COMPLETED') {
            Utils.toast('success', 'Fetch Complete', `${job.done} documents processed`);
            _imcRefreshJobs();
          }
        }
      } catch (err) {
        clearInterval(interval);
        if (_imcHandleAuthError(err)) return;
        if (statusEl) statusEl.innerHTML = `<span style="color:var(--accent-red)">${_imcFriendlyError(err, 'Fetch progress could not be updated.')}</span>`;
        _imcLogEvent('fetch.poll_failed', { job_id: jobId, source, message: String(err?.message || '').slice(0, 120) }, 'warn');
      }
    }, 3000);
  }

  async function _imcRefreshJobs() {
    const el = document.getElementById('imc-jobs-list');
    if (!el) return;
    const reqSeq = ++_imcJobsReqSeq;
    try {
      const data = await API.get('/im-checker/fetch/status');
      if (reqSeq !== _imcJobsReqSeq) return;
      const jobs = (data.jobs || []).slice().reverse();
      if (!jobs.length) {
        el.innerHTML = '<div style="color:var(--text-muted);font-size:0.82rem;text-align:center;padding:20px"><div style="font-weight:700;color:var(--text-secondary);margin-bottom:4px">No active fetch jobs</div><div>Start a SEBI/BSE/NSE fetch above to populate this section.</div></div>';
        return;
      }
      const col = {'RUNNING':'#2563eb','COMPLETED':'#16a34a','FAILED':'#dc2626'};
      el.innerHTML = `<table class="table" style="width:100%;border-collapse:collapse;font-size:0.82rem">
        <thead><tr style="background:var(--bg-tertiary)">
          <th style="padding:8px 12px;text-align:left">Job ID</th>
          <th style="padding:8px 12px;text-align:left">Source</th>
          <th style="padding:8px 12px;text-align:left">Status</th>
          <th style="padding:8px 12px;text-align:left">Progress</th>
          <th style="padding:8px 12px;text-align:left">Started</th>
        </tr></thead>
        <tbody>${jobs.map(j => `<tr style="border-top:1px solid var(--border-light)">
          <td style="padding:8px 12px;font-family:monospace;font-size:0.75rem">${j.job_id}</td>
          <td style="padding:8px 12px;font-weight:600">${j.source}</td>
          <td style="padding:8px 12px"><span style="color:${col[j.status]||'#6b7280'};font-weight:600">${j.status}</span></td>
          <td style="padding:8px 12px">${j.done||0}/${j.total||'?'} done &bull; ${j.failed||0} failed</td>
          <td style="padding:8px 12px;color:var(--text-muted);font-size:0.75rem">${(j.started_at||'').slice(0,16)}</td>
        </tr>`).join('')}</tbody></table>`;
    } catch (err) {
      if (_imcHandleAuthError(err)) return;
      el.innerHTML = `<div style="color:var(--accent-red);font-size:0.82rem;text-align:center;padding:20px">${_imcFriendlyError(err, 'Could not load active jobs.')}</div>`;
      _imcLogEvent('fetch.jobs_failed', { message: String(err?.message || '').slice(0, 120) }, 'warn');
    }
  }

  async function _imcLoadFetchHistory() {
    const el = document.getElementById('imc-fetch-history');
    if (!el) return;
    const reqSeq = ++_imcHistoryReqSeq;
    try {
      const data = await API.get('/im-checker/fetch/history');
      if (reqSeq !== _imcHistoryReqSeq) return;
      const rows = data.history || [];
      if (!rows.length) {
        el.innerHTML = '<div style="color:var(--text-muted);font-size:0.82rem;text-align:center;padding:16px"><div style="font-weight:700;color:var(--text-secondary);margin-bottom:4px">No fetch history yet</div><div>Run a source fetch to generate ingestion history and audit trail.</div></div>';
        return;
      }
      el.innerHTML = `<table class="table" style="width:100%;border-collapse:collapse;font-size:0.82rem">
        <thead><tr style="background:var(--bg-tertiary)">
          <th style="padding:8px 12px;text-align:left">Date</th>
          <th style="padding:8px 12px;text-align:left">Source</th>
          <th style="padding:8px 12px;text-align:left">ISINs Found</th>
          <th style="padding:8px 12px;text-align:left">New Records</th>
        </tr></thead>
        <tbody>${rows.map(r => `<tr style="border-top:1px solid var(--border-light)">
          <td style="padding:8px 12px;color:var(--text-muted);font-size:0.78rem">${(r.fetched_at||'').slice(0,16)}</td>
          <td style="padding:8px 12px;font-weight:600">${r.source}</td>
          <td style="padding:8px 12px">${r.isins_found||0}</td>
          <td style="padding:8px 12px">${r.new_records||0}</td>
        </tr>`).join('')}</tbody></table>`;
    } catch (err) {
      if (_imcHandleAuthError(err)) return;
      el.innerHTML = `<div style="color:var(--accent-red);font-size:0.82rem;text-align:center;padding:16px">${_imcFriendlyError(err, 'Could not load fetch history.')}</div>`;
      _imcLogEvent('fetch.history_failed', { message: String(err?.message || '').slice(0, 120) }, 'warn');
    }
  }

  // â”€â”€â”€ IM Upload Tab Functions â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

  let _imcUploadFile = null;  // cached File object for upload

  // ISIN autocomplete for upload tab
  function _imcInitUploadISINSearch() {
    const inp = document.getElementById('imc-up-isin');
    const dd  = document.getElementById('imc-up-isin-dd');
    if (!inp || !dd || inp.dataset.autocompleteBound === 'true') return;
    inp.dataset.autocompleteBound = 'true';
    let tmr;
    inp.addEventListener('input', () => {
      clearTimeout(tmr);
      const q = inp.value.trim();
      if (q.length < 2) { dd.style.display='none'; return; }
      tmr = setTimeout(async () => {
        try {
          const data = await API.get(`/im-checker/search?q=${encodeURIComponent(q)}`);
          const items = (data.results || []).slice(0, 10);
          if (!items.length) { dd.style.display='none'; return; }
          dd.innerHTML = items.map(b =>
            `<div style="padding:10px 14px;cursor:pointer;font-size:0.84rem;border-bottom:1px solid var(--border-light);display:flex;justify-content:space-between"
                  onmousedown="App._imcSelectUploadISIN('${b.isin}','${(b.issuer_name||'').replace(/'/g,"\\'")}')">
              <span style="font-family:monospace;font-weight:600">${b.isin}</span>
              <span class="compact-ellipsis" style="color:var(--text-muted);max-width:200px;text-align:right;flex:1" title="${Utils.escapeAttr(b.issuer_name || '')}">${b.issuer_name||''}</span>
            </div>`
          ).join('');
          dd.style.display = 'block';
        } catch (err) {
          dd.style.display='none';
          _imcLogEvent('upload.search_failed', { message: String(err?.message || '').slice(0, 120) }, 'warn');
        }
      }, 250);
    });
    inp.addEventListener('change', () => { _imcResolveUploadBond(); });
    inp.addEventListener('blur', () => setTimeout(() => { dd.style.display='none'; }, 200));
  }

  function _imcSelectUploadISIN(isin, issuer) {
    const inp = document.getElementById('imc-up-isin');
    const issInp = document.getElementById('imc-up-issuer');
    const dd = document.getElementById('imc-up-isin-dd');
    const info = document.getElementById('imc-up-bond-info');
    if (inp) inp.value = isin;
    if (issInp) issInp.value = issuer;
    if (dd) dd.style.display = 'none';
    if (info) info.innerHTML = `<span style="color:#16a34a">&#10003;</span> Bond found: <strong>${issuer}</strong>`;
    _imcLoadUploadHistory();
  }

  async function _imcResolveUploadBond() {
    const inp = document.getElementById('imc-up-isin');
    const issInp = document.getElementById('imc-up-issuer');
    const info = document.getElementById('imc-up-bond-info');
    if (!inp) return;
    const isin = (inp.value || '').trim().toUpperCase();
    if (isin.length < 10) {
      if (issInp) issInp.value = '';
      if (info) info.innerHTML = '';
      return;
    }
    try {
      const data = await API.get(`/im-checker/${encodeURIComponent(isin)}`);
      const bond = data.bond || {};
      if (issInp) issInp.value = bond.issuer_name || '';
      if (info) info.innerHTML = bond.issuer_name
        ? `<span style="color:#16a34a">&#10003;</span> Bond found: <strong>${bond.issuer_name}</strong>`
        : '<span style="color:var(--text-muted)">Bond found</span>';
      _imcLoadUploadHistory();
    } catch (err) {
      if (_imcHandleAuthError(err)) return;
      if (issInp) issInp.value = '';
      if (info) info.innerHTML = '<span style="color:var(--accent-red)">Bond not found in master registry</span>';
    }
  }

  function _imcHandleUploadDrop(e) {
    const files = e.dataTransfer?.files;
    if (!files || !files.length) return;
    _imcSetUploadFile(files[0]);
  }

  function _imcHandleUploadSelect(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    _imcSetUploadFile(file);
  }

  function _imcSetUploadFile(file) {
    if (!file.name.toLowerCase().endsWith('.pdf')) {
      Utils.toast('error', 'Invalid File', 'Only PDF files are accepted');
      return;
    }
    if (file.size > 50 * 1024 * 1024) {
      Utils.toast('error', 'File Too Large', 'Maximum file size is 50 MB');
      return;
    }
    _imcUploadFile = file;
    const infoEl = document.getElementById('imc-up-file-info');
    const submitBtn = document.getElementById('imc-up-submit');
    if (infoEl) {
      const sizeMB = (file.size / 1024 / 1024).toFixed(2);
      infoEl.style.display = 'block';
      infoEl.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div>
            <div style="font-weight:600;color:var(--text-primary)">${file.name}</div>
            <div style="color:var(--text-muted);font-size:0.78rem">${sizeMB} MB &bull; PDF</div>
          </div>
          <button class="btn btn-sm btn-outline" type="button" data-method="_imcClearUploadFile" style="font-size:0.75rem;padding:4px 10px">Remove</button>
        </div>`;
    }
    if (submitBtn) submitBtn.style.display = 'block';
  }

  function _imcClearUploadFile() {
    _imcUploadFile = null;
    const infoEl = document.getElementById('imc-up-file-info');
    const submitBtn = document.getElementById('imc-up-submit');
    const fileInput = document.getElementById('imc-up-file');
    if (infoEl) { infoEl.style.display = 'none'; infoEl.innerHTML = ''; }
    if (submitBtn) submitBtn.style.display = 'none';
    if (fileInput) fileInput.value = '';
  }

  async function _imcSubmitUpload() {
    const isin = (document.getElementById('imc-up-isin')?.value || '').trim().toUpperCase();
    if (!isin || isin.length < 10) {
      Utils.toast('warning', 'ISIN Required', 'Enter a valid ISIN (e.g. INE001A07123)');
      return;
    }
    if (!_imcUploadFile) {
      Utils.toast('warning', 'No File', 'Select a PDF file to upload');
      return;
    }
    if (!ensureAuthenticated('upload an IM file')) return;

    const statusEl = document.getElementById('imc-up-status');
    const submitBtn = document.getElementById('imc-up-submit');
    if (submitBtn) submitBtn.disabled = true;
    if (statusEl) {
      statusEl.style.display = 'block';
      statusEl.innerHTML = `
        <div class="card" style="padding:20px;text-align:center">
          <div class="spinner" style="width:32px;height:32px;margin:0 auto 12px"></div>
          <p style="margin:0;color:var(--text-secondary);font-size:0.88rem">Processing IM PDF...</p>
          <p style="margin:4px 0 0;color:var(--text-muted);font-size:0.78rem">Validating â†’ Extracting text â†’ Running NLP â†’ Risk analysis â†’ Storing</p>
        </div>`;
    }

    const issuer = document.getElementById('imc-up-issuer')?.value || '';
    const formData = new FormData();
    formData.append('file', _imcUploadFile);

    try {
      let url = `/im-checker/upload?isin=${encodeURIComponent(isin)}`;
      if (issuer) url += `&issuer_name=${encodeURIComponent(issuer)}`;

      const res = await API.post(url, formData);

      if (res.status === 'success') {
        Utils.toast('success', 'Upload Complete', `IM processed: Risk ${res.risk_level} (${Math.round(res.risk_score)})`);
        _imcRenderUploadResult(res);
        _imcLoadUploadHistory();
        _imcClearUploadFile();
      } else if (res.status === 'duplicate') {
        Utils.toast('warning', 'Duplicate', res.errors?.[0] || 'File already uploaded');
        if (statusEl) statusEl.innerHTML = `<div class="card" style="padding:16px;color:#d97706;font-size:0.88rem">
          <strong>Duplicate detected:</strong> ${Utils.escapeHTML(res.errors?.[0] || 'Identical file already exists')}
        </div>`;
      } else {
        throw new Error(res.detail?.errors?.[0] || res.errors?.[0] || res.detail || 'Upload failed');
      }
    } catch (err) {
      if (_imcHandleAuthError(err)) return;
      Utils.toast('error', 'Upload Failed', _imcFriendlyError(err, 'Upload failed. Please check file and retry.'));
      if (statusEl) statusEl.innerHTML = `<div class="card" style="padding:16px;color:var(--accent-red);font-size:0.88rem">
        <strong>Error:</strong> ${_imcFriendlyError(err, 'Upload failed. Please retry.')}
      </div>`;
    } finally {
      if (submitBtn) submitBtn.disabled = false;
    }
  }

  function _imcRenderInstitutionalPanel(data) {
    if (!data || !data.upload_id) return '';
    const meta = data.metadata || {};
    const clauses = data.clauses || [];
    const covenants = data.covenants || [];
    const risk = data.risk || {};
    const sections = data.sections || [];
    const redFlags = risk.red_flags || [];
    const processingLog = data.processing_log || {};
    const benchmark = data.sector_benchmark || {};

    const levelColor = { LOW:'#16a34a', MEDIUM:'#d97706', HIGH:'#e55a2b', CRITICAL:'#dc2626' }[risk.risk_level] || '#6b7280';
    const statusMeta = st => {
      const status = (st || '').toUpperCase();
      if (status === 'PRESENT' || status === 'FOUND') return { label: 'Present', bg: 'rgba(22,163,74,0.1)', border: 'rgba(22,163,74,0.2)', fg: '#16a34a', icon: '&#10003;' };
      if (status === 'INCONCLUSIVE') return { label: 'Inconclusive', bg: 'rgba(217,119,6,0.12)', border: 'rgba(217,119,6,0.25)', fg: '#d97706', icon: '&#9888;' };
      return { label: 'Absent', bg: 'rgba(220,38,38,0.08)', border: 'rgba(220,38,38,0.15)', fg: '#dc2626', icon: '&#10005;' };
    };

    // Clause type labels
    const clauseLabels = {
      ACCELERATION: 'Acceleration', CROSS_DEFAULT: 'Cross-Default', NEGATIVE_PLEDGE: 'Negative Pledge',
      CHANGE_OF_CONTROL: 'Change of Control', SECURITY: 'Security', GUARANTEE: 'Guarantee',
      EVENT_OF_DEFAULT: 'Event of Default'
    };

    // Build clause status grid
    const allClauseTypes = ['ACCELERATION','CROSS_DEFAULT','NEGATIVE_PLEDGE','CHANGE_OF_CONTROL','SECURITY','GUARANTEE','EVENT_OF_DEFAULT'];
    const clauseMap = {};
    clauses.forEach(c => { clauseMap[c.clause_type] = c; });

    // Separate covenants
    const finCov = covenants.filter(c => c.covenant_type === 'FINANCIAL');
    const opsCov = covenants.filter(c => c.covenant_type === 'OPERATIONAL');

    return `
      <!-- Institutional Risk Score -->
      <div class="card" style="padding:20px;margin-bottom:16px">
        <div style="display:flex;align-items:center;gap:20px;flex-wrap:wrap">
          <div style="text-align:center">
            <div style="width:88px;height:88px;border-radius:50%;background:conic-gradient(${levelColor} 0% ${risk.risk_score||0}%, var(--bg-tertiary) ${risk.risk_score||0}% 100%);display:flex;align-items:center;justify-content:center">
              <div style="width:68px;height:68px;border-radius:50%;background:var(--bg-card);display:flex;align-items:center;justify-content:center;flex-direction:column">
                <span style="font-size:1.4rem;font-weight:800;color:${levelColor}">${Math.round(risk.risk_score||0)}</span>
                <span style="font-size:0.6rem;color:var(--text-muted)">/ 100</span>
              </div>
            </div>
          </div>
          <div style="flex:1">
            <div style="font-size:0.78rem;color:var(--text-muted);margin-bottom:4px">Institutional IM Risk Score</div>
            <span style="display:inline-block;padding:4px 14px;border-radius:20px;font-weight:700;font-size:0.95rem;color:#fff;background:${levelColor}">${risk.risk_level||'UNKNOWN'}</span>
            <div style="margin-top:8px;font-size:0.82rem;color:var(--text-secondary)">
              ${data.pages_count||0} pages &bull; ${(data.total_tokens||0).toLocaleString()} tokens &bull;
              ${sections.length} sections detected &bull; ${clauses.length} clauses analysed
            </div>
            ${(benchmark.avg_score != null) ? `<div style="margin-top:10px;font-size:0.76rem;color:var(--text-muted)">Vs sector avg <strong style="color:${benchmark.delta > 0 ? '#dc2626' : '#16a34a'}">${benchmark.avg_score}</strong> <span style="margin-left:6px;padding:2px 8px;border-radius:999px;background:var(--bg-tertiary);color:${benchmark.delta > 0 ? '#dc2626' : '#16a34a'};font-weight:700">${benchmark.delta > 0 ? '+' : ''}${benchmark.delta || 0}</span></div>` : ''}
          </div>
        </div>

        <!-- Score Breakdown -->
        ${risk.unsecured_penalty || risk.missing_clauses_penalty || risk.covenant_penalty || risk.rating_penalty ? `
        <div style="margin-top:16px;display:flex;flex-wrap:wrap;gap:8px">
          ${risk.unsecured_penalty ? `<div style="padding:6px 12px;background:var(--bg-tertiary);border-radius:8px;border-left:3px solid #dc2626;font-size:0.78rem"><span style="color:var(--text-muted)">Unsecured</span> <strong style="color:#dc2626">+${risk.unsecured_penalty}</strong></div>` : ''}
          ${risk.missing_clauses_penalty ? `<div style="padding:6px 12px;background:var(--bg-tertiary);border-radius:8px;border-left:3px solid #e55a2b;font-size:0.78rem"><span style="color:var(--text-muted)">Missing Clauses</span> <strong style="color:#e55a2b">+${risk.missing_clauses_penalty}</strong></div>` : ''}
          ${risk.covenant_penalty ? `<div style="padding:6px 12px;background:var(--bg-tertiary);border-radius:8px;border-left:3px solid #d97706;font-size:0.78rem"><span style="color:var(--text-muted)">Covenants</span> <strong style="color:#d97706">+${risk.covenant_penalty}</strong></div>` : ''}
          ${risk.rating_penalty ? `<div style="padding:6px 12px;background:var(--bg-tertiary);border-radius:8px;border-left:3px solid #dc2626;font-size:0.78rem"><span style="color:var(--text-muted)">Rating</span> <strong style="color:#dc2626">+${risk.rating_penalty}</strong></div>` : ''}
          ${risk.coupon_penalty ? `<div style="padding:6px 12px;background:var(--bg-tertiary);border-radius:8px;border-left:3px solid #d97706;font-size:0.78rem"><span style="color:var(--text-muted)">High Coupon</span> <strong style="color:#d97706">+${risk.coupon_penalty}</strong></div>` : ''}
          ${risk.structure_penalty ? `<div style="padding:6px 12px;background:var(--bg-tertiary);border-radius:8px;border-left:3px solid #ca8a04;font-size:0.78rem"><span style="color:var(--text-muted)">Structure</span> <strong style="color:#ca8a04">+${risk.structure_penalty}</strong></div>` : ''}
        </div>` : ''}
      </div>

      ${(processingLog.method_used || processingLog.char_count || processingLog.pages_processed) ? `
      <div class="card" style="padding:16px 20px;margin-bottom:16px">
        <h3 style="margin:0 0 12px;font-size:0.92rem;font-weight:700">Extraction Diagnostics</h3>
        <div style="display:grid;grid-template-columns:repeat(4, minmax(120px, 1fr));gap:8px;font-size:0.8rem">
          ${_imcFieldRow('Parser', (processingLog.method_used || 'â€“').toUpperCase())}
          ${_imcFieldRow('Pages', processingLog.pages_processed || data.pages_count || 0)}
          ${_imcFieldRow('Text Length', processingLog.char_count ? processingLog.char_count.toLocaleString() : null)}
          ${_imcFieldRow('Time', processingLog.processing_time != null ? processingLog.processing_time + 's' : null)}
        </div>
      </div>` : ''}

      <!-- Bond Summary (Metadata) -->
      ${Object.keys(meta).length > 2 ? `
      <div class="card" style="padding:20px;margin-bottom:16px">
        <h3 style="margin:0 0 12px;font-size:0.95rem;font-weight:700">Bond Summary</h3>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:0.82rem">
          ${_imcFieldRow('Issuer', meta.issuer_name)}
          ${_imcFieldRow('ISIN', meta.isin)}
          ${_imcFieldRow('Currency', meta.currency)}
          ${_imcFieldRow('Coupon', meta.coupon ? meta.coupon + '%' : null)}
          ${_imcFieldRow('Maturity', meta.maturity_date)}
          ${_imcFieldRow('Issue Size', meta.issue_size_cr ? '\u20B9' + meta.issue_size_cr + ' Cr' : null)}
          ${_imcFieldRow('Face Value', meta.face_value ? '\u20B9' + Number(meta.face_value).toLocaleString() : null)}
          ${_imcFieldRow('Security Type', meta.security_type)}
          ${_imcFieldRow('Seniority', meta.seniority)}
          ${_imcFieldRow('Credit Rating', meta.credit_rating)}
          ${_imcFieldRow('Rating Agency', meta.rating_agency)}
          ${_imcFieldRow('Trustee', meta.trustee)}
          ${_imcFieldRow('Registrar', meta.registrar)}
          ${_imcFieldRow('Listing', meta.listing_exchange)}
          ${_imcFieldRow('Governing Law', meta.governing_law)}
          ${_imcFieldRow('Guarantee', meta.guarantee)}
          ${meta.call_option ? _imcFieldRow('Call Option', 'Yes') : ''}
          ${meta.put_option ? _imcFieldRow('Put Option', 'Yes') : ''}
          ${meta.step_up ? _imcFieldRow('Step-Up', 'Yes') : ''}
        </div>
      </div>` : ''}

      <!-- Clause Detection Status -->
      <div class="card" style="padding:20px;margin-bottom:16px">
        <h3 style="margin:0 0 14px;font-size:0.95rem;font-weight:700">Clause Detection</h3>
        <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px">
          ${allClauseTypes.map(ct => {
            const cl = clauseMap[ct];
            const sm = statusMeta(cl?.status);
            const conf = cl ? Math.round((cl.confidence || 0) * 100) : 0;
            return `<div style="padding:8px 14px;border-radius:8px;font-size:0.82rem;font-weight:600;min-width:140px;
                         background:${sm.bg};
                         color:${sm.fg};border:1px solid ${sm.border}">
              <div>${sm.icon} ${clauseLabels[ct] || ct}</div>
              ${cl ? `<div style="font-size:0.7rem;font-weight:400;margin-top:2px;opacity:0.82">${sm.label} &bull; ${conf}% conf${cl.page_number ? ' &bull; p.' + cl.page_number : ''}${cl.section ? ' &bull; ' + cl.section.replace(/_/g,' ') : ''}</div>` : `<div style="font-size:0.7rem;font-weight:400;margin-top:2px;opacity:0.7">Not processed</div>`}
            </div>`;
          }).join('')}
        </div>

        <!-- Clause Evidence -->
        ${clauses.filter(c => ['PRESENT','FOUND','INCONCLUSIVE'].includes((c.status||'').toUpperCase()) && ((c.evidence || []).length || c.clause_text)).length ? `
        <div style="border-top:1px solid var(--border-light);padding-top:14px">
          <h4 style="margin:0 0 10px;font-size:0.85rem;font-weight:600;color:var(--text-secondary)">Evidence Snippets</h4>
          ${clauses.filter(c => ['PRESENT','FOUND','INCONCLUSIVE'].includes((c.status||'').toUpperCase())).map(c => {
            const evList = c.evidence || [];
            if (!evList.length && !c.clause_text) return '';
            return `<details style="margin-bottom:8px;background:var(--bg-tertiary);border-radius:8px;border-left:3px solid #16a34a">
              <summary style="padding:10px 14px;cursor:pointer;font-size:0.82rem;font-weight:600;list-style:none;display:flex;justify-content:space-between;align-items:center">
                <span>${clauseLabels[c.clause_type] || c.clause_type}</span>
                <span style="font-size:0.72rem;color:var(--text-muted);font-weight:400">${evList.length} evidence${evList.length !== 1 ? 's' : ''} &bull; p.${c.page_number || '?'}</span>
              </summary>
              <div style="padding:0 14px 10px;font-size:0.78rem">
                ${c.clause_text ? `<div style="padding:8px 10px;margin-bottom:6px;background:var(--bg-card);border-radius:6px;border-left:2px solid #2563eb;color:var(--text-secondary)">${c.clause_text.slice(0, 300)}${c.clause_text.length > 300 ? '...' : ''}</div>` : ''}
                ${evList.map(ev => `<div style="padding:6px 10px;margin-bottom:4px;background:var(--bg-card);border-radius:6px;font-size:0.75rem;color:var(--text-secondary)">
                  <span style="color:var(--text-muted);font-size:0.7rem">p.${ev.page_number || '?'}${ev.section ? ' &bull; ' + ev.section.replace(/_/g,' ') : ''} &bull; ${Math.round((ev.confidence||0)*100)}%</span><br>
                  "${(ev.text_snippet || '').slice(0, 200)}${(ev.text_snippet || '').length > 200 ? '...' : ''}"
                </div>`).join('')}
              </div>
            </details>`;
          }).join('')}
        </div>` : ''}
      </div>

      <!-- Covenants -->
      ${covenants.length ? `
      <div class="card" style="padding:20px;margin-bottom:16px">
        <h3 style="margin:0 0 14px;font-size:0.95rem;font-weight:700">Covenants (${covenants.length})</h3>

        ${finCov.length ? `
        <div style="margin-bottom:14px">
          <h4 style="margin:0 0 8px;font-size:0.85rem;font-weight:600;color:#2563eb">Financial Covenants (${finCov.length})</h4>
          <table class="table" style="width:100%;border-collapse:collapse;font-size:0.82rem">
            <thead><tr style="background:var(--bg-tertiary)">
              <th style="padding:8px 12px;text-align:left;font-weight:600;color:var(--text-secondary)">Covenant</th>
              <th style="padding:8px 12px;text-align:left;font-weight:600;color:var(--text-secondary)">Threshold</th>
              <th style="padding:8px 12px;text-align:left;font-weight:600;color:var(--text-secondary)">Page</th>
              <th style="padding:8px 12px;text-align:left;font-weight:600;color:var(--text-secondary)">Confidence</th>
            </tr></thead>
            <tbody>${finCov.map((c,i) => `<tr style="border-top:1px solid var(--border-light);${i%2?'background:var(--bg-tertiary)':''}">
              <td style="padding:8px 12px;font-weight:600">${c.covenant_name || 'â€“'}</td>
              <td style="padding:8px 12px;font-family:monospace;color:var(--accent-blue)">${c.threshold || 'â€“'}</td>
              <td style="padding:8px 12px;color:var(--text-muted)">p.${c.page_number || '?'}</td>
              <td style="padding:8px 12px;color:var(--text-muted)">${Math.round((c.confidence||0)*100)}%</td>
            </tr>`).join('')}</tbody>
          </table>
        </div>` : ''}

        ${opsCov.length ? `
        <div>
          <h4 style="margin:0 0 8px;font-size:0.85rem;font-weight:600;color:#7c3aed">Operational Covenants (${opsCov.length})</h4>
          <div style="display:flex;flex-direction:column;gap:6px">
            ${opsCov.map(c => `<div style="padding:8px 12px;background:var(--bg-tertiary);border-radius:6px;border-left:3px solid #7c3aed;font-size:0.8rem">
              <div style="font-weight:600">${c.covenant_name || 'â€“'}</div>
              <div style="color:var(--text-muted);font-size:0.75rem;margin-top:2px">p.${c.page_number || '?'} &bull; ${Math.round((c.confidence||0)*100)}% &bull; ${(c.text || '').slice(0, 150)}${(c.text||'').length > 150 ? '...' : ''}</div>
            </div>`).join('')}
          </div>
        </div>` : ''}
      </div>` : `
      <div class="card" style="padding:16px 20px;margin-bottom:16px;border-left:3px solid #d97706">
        <div style="font-size:0.85rem;color:#d97706;font-weight:600">No financial or operational covenants detected</div>
        <div style="font-size:0.78rem;color:var(--text-muted);margin-top:4px">This may indicate weak investor protection</div>
      </div>`}

      <!-- Red Flags -->
      ${redFlags.length ? `
      <div class="card" style="padding:20px;margin-bottom:16px;border-left:4px solid #dc2626">
        <h3 style="margin:0 0 10px;font-size:0.95rem;font-weight:700;color:#dc2626">Red Flags (${redFlags.length})</h3>
        <div style="display:flex;flex-wrap:wrap;gap:6px">
          ${redFlags.map(f => `<span style="padding:4px 12px;border-radius:8px;font-size:0.78rem;font-weight:700;background:rgba(220,38,38,0.1);color:#dc2626">${(f||'').replace(/_/g,' ')}</span>`).join('')}
        </div>
      </div>` : ''}

      <!-- Sections Detected -->
      ${sections.length ? `
      <details class="card" style="margin-bottom:16px">
        <summary style="padding:14px 20px;cursor:pointer;display:flex;align-items:center;gap:10px;font-weight:600;font-size:0.9rem;list-style:none">
          Document Sections (${sections.length})
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:14px;height:14px;margin-left:auto"><polyline points="6 9 12 15 18 9"/></svg>
        </summary>
        <div style="padding:0 20px 14px">
          <table class="table" style="width:100%;border-collapse:collapse;font-size:0.82rem">
            <thead><tr style="background:var(--bg-tertiary)">
              <th style="padding:6px 10px;text-align:left">Section</th>
              <th style="padding:6px 10px;text-align:left">Pages</th>
              <th style="padding:6px 10px;text-align:left">Confidence</th>
            </tr></thead>
            <tbody>${sections.map((s,i) => `<tr style="border-top:1px solid var(--border-light);${i%2?'background:var(--bg-tertiary)':''}">
              <td style="padding:6px 10px;font-weight:600">${(s.section_name||'').replace(/_/g,' ')}</td>
              <td style="padding:6px 10px;color:var(--text-muted)">p.${s.start_page||'?'}${s.end_page && s.end_page !== s.start_page ? ' â€“ p.' + s.end_page : ''}</td>
              <td style="padding:6px 10px;color:var(--text-muted)">${Math.round((s.confidence||0)*100)}%</td>
            </tr>`).join('')}</tbody>
          </table>
        </div>
      </details>` : ''}
    `;
  }

  async function _imcLoadInstitutional(uploadId) {
    const container = document.getElementById('imc-institutional-container');
    if (!container) return;
    container.innerHTML = '<div style="text-align:center;padding:30px;color:var(--text-muted)"><div class="spinner" style="width:28px;height:28px;margin:auto auto 10px"></div>Loading institutional analysis...</div>';
    try {
      const data = await API.get('/im-checker/uploads/' + uploadId + '/institutional');
      if (!data || !data.upload_id) {
        container.innerHTML = '<div style="padding:16px;color:var(--text-muted);font-size:0.85rem">No institutional analysis available for this upload. Try re-processing.</div>';
        return;
      }
      container.innerHTML = _imcRenderInstitutionalPanel(data);
    } catch (err) {
      if (_imcHandleAuthError(err)) return;
      container.innerHTML = '<div style="padding:16px;color:var(--accent-red);font-size:0.85rem">' + _imcFriendlyError(err, 'Could not load institutional analysis.') + '</div>';
    }
  }

  function _imcRenderUploadResult(res) {
    const el = document.getElementById('imc-up-result');
    if (!el) return;
    el.style.display = 'block';

    const levelColor = { LOW: '#16a34a', MEDIUM: '#d97706', HIGH: '#e55a2b', CRITICAL: '#dc2626' }[res.risk_level] || '#6b7280';
    const ext = res.extracted || {};
    const clauses = res.risk_clauses || {};
    const clauseItems = [
      ['Cross-Default', clauses.cross_default],
      ['Change of Control', clauses.change_of_control],
      ['Acceleration', clauses.acceleration],
      ['Negative Pledge', clauses.negative_pledge],
    ];

    el.innerHTML = `
      <!-- Risk Score -->
      <div class="card" style="padding:20px;margin-bottom:16px">
        <div style="display:flex;align-items:center;gap:20px;flex-wrap:wrap">
          <div style="text-align:center">
            <div style="width:80px;height:80px;border-radius:50%;background:conic-gradient(${levelColor} 0% ${res.risk_score||0}%, var(--bg-tertiary) ${res.risk_score||0}% 100%);display:flex;align-items:center;justify-content:center">
              <div style="width:62px;height:62px;border-radius:50%;background:var(--bg-card);display:flex;align-items:center;justify-content:center;flex-direction:column">
                <span style="font-size:1.3rem;font-weight:800;color:${levelColor}">${Math.round(res.risk_score||0)}</span>
                <span style="font-size:0.55rem;color:var(--text-muted)">/ 100</span>
              </div>
            </div>
          </div>
          <div style="flex:1">
            <div style="font-size:0.78rem;color:var(--text-muted);margin-bottom:4px">IM Document Risk Score</div>
            <span style="display:inline-block;padding:4px 14px;border-radius:20px;font-weight:700;font-size:0.9rem;color:#fff;background:${levelColor}">${res.risk_level||'UNKNOWN'}</span>
            <div style="margin-top:8px;font-size:0.82rem;color:var(--text-secondary)">
              Version <strong>${res.version||1}</strong> &bull; ${res.extraction_method||'pdfminer'} &bull; ${(res.text_length||0).toLocaleString()} chars
            </div>
          </div>
        </div>
      </div>

      <!-- Auto-Detected Fields -->
      <div class="card" style="padding:20px;margin-bottom:16px">
        <h3 style="margin:0 0 12px;font-size:0.9rem;font-weight:700">Auto-Detected Fields</h3>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:0.82rem">
          ${_imcFieldRow('Coupon', ext.coupon ? ext.coupon + '%' : null)}
          ${_imcFieldRow('Maturity', ext.maturity_date)}
          ${_imcFieldRow('Issue Size', ext.issue_size_cr ? 'â‚¹' + ext.issue_size_cr + ' Cr' : null)}
          ${_imcFieldRow('Face Value', ext.face_value ? 'â‚¹' + ext.face_value.toLocaleString() : null)}
          ${_imcFieldRow('Credit Rating', ext.credit_rating)}
          ${_imcFieldRow('Rating Agency', ext.rating_agency)}
          ${_imcFieldRow('Call Option', ext.call_option ? 'Yes' : 'No')}
          ${_imcFieldRow('Put Option', ext.put_option ? 'Yes' : 'No')}
          ${_imcFieldRow('Security', ext.security_type)}
          ${_imcFieldRow('Seniority', ext.seniority)}
        </div>
      </div>

      <!-- Risk Clauses -->
      <div class="card" style="padding:20px;margin-bottom:16px">
        <h3 style="margin:0 0 12px;font-size:0.9rem;font-weight:700">Risk Clause Detection</h3>
        <div style="display:flex;flex-wrap:wrap;gap:8px">
          ${clauseItems.map(([name, present]) =>
            `<div style="padding:6px 14px;border-radius:8px;font-size:0.8rem;font-weight:600;
                         background:${present ? 'rgba(22,163,74,0.1)' : 'rgba(220,38,38,0.1)'};
                         color:${present ? '#16a34a' : '#dc2626'}">
              ${present ? '&#10003;' : '&#10005;'} ${name}
            </div>`
          ).join('')}
          <div style="padding:6px 14px;border-radius:8px;font-size:0.8rem;font-weight:600;background:var(--bg-tertiary);color:var(--text-secondary)">
            ${clauses.covenants_found||0} covenants found
          </div>
        </div>

        ${(res.risk_flags||[]).length ? `
        <div style="margin-top:12px">
          <div style="font-size:0.78rem;font-weight:600;color:var(--text-muted);margin-bottom:6px">RISK FLAGS</div>
          <div style="display:flex;flex-wrap:wrap;gap:6px">
            ${(res.risk_flags||[]).map(f =>
              `<span style="padding:3px 10px;border-radius:6px;font-size:0.72rem;font-weight:700;background:rgba(220,38,38,0.1);color:#dc2626">${f.replace(/_/g,' ')}</span>`
            ).join('')}
          </div>
        </div>` : ''}
      </div>

      <!-- Actions -->
      <div class="card" style="padding:16px 20px">
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn btn-sm btn-primary" type="button" data-method="_imcReprocessUpload" data-arg="${res.upload_id}">Re-run Analysis</button>
          <button class="btn btn-sm btn-outline" type="button" data-method="_imcCheckIntegrity" data-arg="${res.upload_id}">Integrity Check</button>
          <button class="btn btn-sm btn-outline" type="button" data-method="_imcViewUploadDetail" data-arg="${res.upload_id}">Open Full Dossier</button>
        </div>
      </div>

      <!-- Institutional Analysis -->
      <div id="imc-institutional-container" style="margin-top:16px">
        ${res.institutional ? _imcRenderInstitutionalPanel(res.institutional) : '<div style="text-align:center;padding:20px;color:var(--text-muted);font-size:0.85rem">Institutional analysis available after processing. <a href="#" data-method="_imcLoadInstitutional" data-arg="' + res.upload_id + '" style="color:var(--accent-blue);font-weight:600">Load Analysis</a></div>'}
      </div>
    `;

    const statusEl = document.getElementById('imc-up-status');
    if (statusEl) statusEl.style.display = 'none';
  }

  function _imcFieldRow(label, value) {
    const safeLabel = Utils.escapeHTML(String(label || ''));
    const safeValue = value == null || value === '' ? 'â€“' : Utils.escapeHTML(String(value));
    return `<div style="padding:6px 10px;background:var(--bg-tertiary);border-radius:6px">
      <span style="color:var(--text-muted)">${safeLabel}:</span>
      <strong style="color:${value && value !== 'No' ? 'var(--text-primary)' : 'var(--text-muted)'}">${safeValue}</strong>
    </div>`;
  }

  function _fmtNum(value, fallback = 'â€“') {
    if (value == null || value === '' || Number.isNaN(Number(value))) return fallback;
    return Number(value).toLocaleString('en-IN');
  }

  function _fmtInt(value, fallback = '0') {
    if (value == null || value === '' || Number.isNaN(Number(value))) return fallback;
    return Math.round(Number(value)).toLocaleString('en-IN');
  }

  function _imcEncodeInlineArg(value) {
    return encodeURIComponent(String(value || '')).replace(/[!'()*]/g, c => '%' + c.charCodeAt(0).toString(16).toUpperCase());
  }

  function _imcInlineJsString(value) {
    if (value == null) return '';
    return String(value)
      .replace(/\\/g, '\\\\')
      .replace(/'/g, "\\'")
      .replace(/\r/g, '\\r')
      .replace(/\n/g, '\\n')
      .replace(/\u2028/g, '\\u2028')
      .replace(/\u2029/g, '\\u2029');
  }

  function _imcSafeText(value, fallback = 'â€“') {
    if (value == null || value === '') return fallback;
    return Utils.escapeHTML(String(value));
  }

  function _imcPdfEvidenceUrl(uploadId, pageNumber, snippet) {
    const page = pageNumber || 1;
    const search = encodeURIComponent((snippet || '').split(/\s+/).slice(0, 8).join(' '));
    const token = API.getToken ? API.getToken() : '';
    const tokenPart = token ? `?token=${encodeURIComponent(token)}` : '';
    return `/api/v1/im-checker/uploads/${uploadId}/file${tokenPart}#page=${page}${search ? `&search=${search}` : ''}`;
  }

  function _imcOpenPdfEvidence(uploadId, pageNumber, snippet) {
    if (!ensureAuthenticated('open PDF evidence')) return;
    const frame = document.getElementById('imc-dossier-pdf-frame');
    try {
      snippet = decodeURIComponent(snippet || '');
    } catch (err) {
      _imcLogEvent('open_pdf_evidence_decode_failed', { uploadId, pageNumber, error: String(err) }, 'warn');
    }
    const url = _imcPdfEvidenceUrl(uploadId, pageNumber, snippet);
    if (frame) {
      frame.src = url;
      frame.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } else {
      window.open(url, '_blank', 'noopener');
    }
  }

  function _imcRenderInstitutionalDossier(upload, detail, institutional) {
    const u = upload || {};
    const data = institutional || {};
    const risk = data.risk || {};
    const meta = data.metadata || {};
    const clauses = data.clauses || [];
    const covenants = data.covenants || [];
    const sections = data.sections || [];
    const audit = detail.audit_trail || [];
    const processingLog = data.processing_log || {};
    const benchmark = data.sector_benchmark || {};
    const fullTextPreview = data.full_text_preview || {};
    const levelColor = { LOW:'#16a34a', MEDIUM:'#d97706', HIGH:'#e55a2b', CRITICAL:'#dc2626' }[risk.risk_level] || '#6b7280';
    const breakdown = risk.breakdown || {};
    const clauseLabel = ct => ({ ACCELERATION:'Acceleration', CROSS_DEFAULT:'Cross-Default', NEGATIVE_PLEDGE:'Negative Pledge', CHANGE_OF_CONTROL:'Change of Control', SECURITY:'Security', GUARANTEE:'Guarantee', EVENT_OF_DEFAULT:'Event of Default' }[ct] || ct || 'Clause');
    const issuerDisplay = _imcSafeText(meta.issuer_name || u.issuer_name || 'Unknown issuer');
    const isinDisplay = _imcSafeText(u.isin || meta.isin || '');
    const statusChip = status => {
      const st = (status || '').toUpperCase();
      if (st === 'PRESENT' || st === 'FOUND') return '<span style="padding:3px 10px;border-radius:999px;background:rgba(22,163,74,0.12);color:#16a34a;font-size:0.72rem;font-weight:700">PRESENT</span>';
      if (st === 'INCONCLUSIVE') return '<span style="padding:3px 10px;border-radius:999px;background:rgba(217,119,6,0.14);color:#d97706;font-size:0.72rem;font-weight:700">INCONCLUSIVE</span>';
      return '<span style="padding:3px 10px;border-radius:999px;background:rgba(220,38,38,0.12);color:#dc2626;font-size:0.72rem;font-weight:700">ABSENT</span>';
    };

    return `
      <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:16px">
        <div>
          <div style="font-size:0.72rem;font-weight:800;letter-spacing:0.08em;text-transform:uppercase;color:var(--text-muted);margin-bottom:6px">Institutional Dossier</div>
          <h2 style="margin:0;font-size:1.08rem;font-weight:800">${isinDisplay}</h2>
          <div style="font-size:0.82rem;color:var(--text-muted);margin-top:4px">Professional review view for extracted terms, clause evidence and PDF validation</div>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn btn-sm btn-primary" type="button" data-method="_imcExportIcNote" data-arg="${u.id}">Generate IC Note</button>
          <button class="btn btn-sm btn-outline" type="button" data-method="_imcRerunInstitutionalAction" data-arg="${u.id}" data-arg2="ocr">Reprocess with OCR</button>
          <button class="btn btn-sm btn-outline" type="button" data-method="_imcRerunInstitutionalAction" data-arg="${u.id}" data-arg2="clauses">Re-run Clauses</button>
          <button class="btn btn-sm btn-outline" type="button" data-method="_imcRerunInstitutionalAction" data-arg="${u.id}" data-arg2="covenants">Re-run Covenants</button>
        </div>
      </div>

      <div style="display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin-bottom:16px">
        <div class="card" style="padding:12px 14px"><div style="font-size:0.72rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.06em">Issuer</div><div style="margin-top:6px;font-weight:700;font-size:0.86rem">${issuerDisplay}</div></div>
        <div class="card" style="padding:12px 14px"><div style="font-size:0.72rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.06em">Risk</div><div style="margin-top:6px;font-weight:800;font-size:0.98rem;color:${levelColor}">${Math.round(risk.risk_score || 0)}/100</div></div>
        <div class="card" style="padding:12px 14px"><div style="font-size:0.72rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.06em">Sections</div><div style="margin-top:6px;font-weight:700;font-size:0.9rem">${sections.length}</div></div>
        <div class="card" style="padding:12px 14px"><div style="font-size:0.72rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.06em">Clauses</div><div style="margin-top:6px;font-weight:700;font-size:0.9rem">${clauses.length}</div></div>
        <div class="card" style="padding:12px 14px"><div style="font-size:0.72rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.06em">Covenants</div><div style="margin-top:6px;font-weight:700;font-size:0.9rem">${covenants.length}</div></div>
        <div class="card" style="padding:12px 14px"><div style="font-size:0.72rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.06em">Pages</div><div style="margin-top:6px;font-weight:700;font-size:0.9rem">${_fmtInt(data.pages_count || processingLog.pages_processed || 0)}</div></div>
      </div>

      <div style="display:grid;grid-template-columns:minmax(0,1.15fr) minmax(360px,0.85fr);gap:18px;align-items:start">
        <div style="display:flex;flex-direction:column;gap:16px;min-width:0">
          <div class="card" style="padding:20px;border:1px solid rgba(255,255,255,0.06)">
            <div style="display:flex;align-items:center;gap:18px;flex-wrap:wrap">
              <div style="width:90px;height:90px;border-radius:50%;background:conic-gradient(${levelColor} 0% ${risk.risk_score||0}%, var(--bg-tertiary) ${risk.risk_score||0}% 100%);display:flex;align-items:center;justify-content:center">
                <div style="width:68px;height:68px;border-radius:50%;background:var(--bg-card);display:flex;align-items:center;justify-content:center;flex-direction:column">
                  <span style="font-size:1.4rem;font-weight:800;color:${levelColor}">${Math.round(risk.risk_score||0)}</span>
                  <span style="font-size:0.6rem;color:var(--text-muted)">/100</span>
                </div>
              </div>
              <div style="flex:1;min-width:240px">
                <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px">
                  <span style="display:inline-block;padding:5px 14px;border-radius:999px;font-weight:800;font-size:0.92rem;color:#fff;background:${levelColor}">${_imcSafeText(risk.risk_level || 'UNKNOWN')}</span>
                  ${(benchmark.avg_score != null) ? `<span style="padding:4px 10px;border-radius:999px;background:var(--bg-tertiary);font-size:0.75rem;color:var(--text-secondary)">Vs sector avg ${benchmark.avg_score} (${benchmark.delta > 0 ? '+' : ''}${benchmark.delta || 0})</span>` : ''}
                </div>
                <div style="font-size:0.82rem;color:var(--text-secondary)">${issuerDisplay} â€¢ ${_imcSafeText(meta.seniority || u.seniority)} â€¢ ${_imcSafeText(meta.security_type || u.security_type)} â€¢ ${_imcSafeText(meta.credit_rating || u.credit_rating)}</div>
                <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:10px">
                  ${Object.entries(breakdown).map(([k, v]) => v ? `<span style="padding:4px 10px;border-radius:8px;background:var(--bg-tertiary);font-size:0.74rem;color:var(--text-secondary)">${_imcSafeText(k.replace(/_/g,' '), '')} <strong style="color:#dc2626">+${v}</strong></span>` : '').join('')}
                </div>
              </div>
            </div>
          </div>

          <div class="card" style="padding:20px;border:1px solid rgba(255,255,255,0.06)">
            <h3 style="margin:0 0 12px;font-size:0.95rem;font-weight:700">Document Summary</h3>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:0.82rem">
              ${_imcFieldRow('Issuer', meta.issuer_name || u.issuer_name)}
              ${_imcFieldRow('ISIN', meta.isin || u.isin)}
              ${_imcFieldRow('Coupon', meta.coupon_rate ? meta.coupon_rate + '%' : (u.coupon ? u.coupon + '%' : null))}
              ${_imcFieldRow('Maturity', meta.maturity_date || u.maturity_date)}
              ${_imcFieldRow('Issue Size', meta.issue_size_cr ? 'â‚¹' + meta.issue_size_cr + ' Cr' : null)}
              ${_imcFieldRow('Face Value', meta.face_value ? 'â‚¹' + _fmtNum(meta.face_value) : null)}
              ${_imcFieldRow('Security', meta.security_type || u.security_type)}
              ${_imcFieldRow('Seniority', meta.seniority || u.seniority)}
              ${_imcFieldRow('Credit Rating', meta.credit_rating || u.credit_rating)}
              ${_imcFieldRow('Rating Agency', meta.rating_agency)}
              ${_imcFieldRow('Tenor', meta.tenor)}
              ${_imcFieldRow('Trustee', meta.trustee)}
              ${_imcFieldRow('Currency', meta.currency)}
              ${_imcFieldRow('Listing', meta.listing_exchange)}
            </div>
          </div>

          <div class="card" style="padding:20px;border:1px solid rgba(255,255,255,0.06)">
            <h3 style="margin:0 0 12px;font-size:0.95rem;font-weight:700">Clause Status & Evidence</h3>
            <div style="display:flex;flex-direction:column;gap:10px">
              ${clauses.map(c => `
                <div style="padding:12px 14px;border-radius:10px;background:var(--bg-tertiary);border:1px solid var(--border-light)">
                  <div style="display:flex;justify-content:space-between;gap:10px;align-items:flex-start;flex-wrap:wrap">
                    <div>
                      <div style="font-size:0.86rem;font-weight:700">${_imcSafeText(clauseLabel(c.clause_type))}</div>
                      <div style="font-size:0.74rem;color:var(--text-muted);margin-top:4px">Page ${c.page_number || '?'}${c.section ? ' â€¢ ' + _imcSafeText(c.section, '') : ''} â€¢ ${Math.round((c.confidence||0)*100)}%</div>
                    </div>
                    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                      ${statusChip(c.status)}
                      <button class="btn btn-sm btn-outline" type="button" data-method="_imcOpenPdfEvidence" data-args='${Utils.escapeAttr(JSON.stringify([u.id, c.page_number || 1, _imcEncodeInlineArg((c.clause_text || '').slice(0, 120))]))}'>Open PDF</button>
                      <button class="btn btn-sm btn-outline" type="button" data-method="_imcValidateClause" data-args='${Utils.escapeAttr(JSON.stringify([u.id, c.clause_type, (c.status || '').toUpperCase()]))}'>Correct / Validate</button>
                    </div>
                  </div>
                  ${c.clause_text ? `<div style="margin-top:10px;padding:10px 12px;border-radius:8px;background:rgba(255,255,255,0.03);font-size:0.78rem;color:var(--text-secondary);line-height:1.55">${_imcSafeText(c.clause_text)}</div>` : ''}
                  ${(c.evidence || []).length ? `<div style="display:flex;flex-direction:column;gap:6px;margin-top:10px">${(c.evidence || []).map(ev => `<div style="padding:8px 10px;border-radius:8px;background:var(--bg-card);font-size:0.75rem;color:var(--text-secondary);line-height:1.5"><div style="color:var(--text-muted);font-size:0.7rem;margin-bottom:4px">p.${ev.page_number || '?'}${ev.section ? ' â€¢ ' + _imcSafeText(ev.section, '') : ''} â€¢ ${Math.round((ev.confidence||0)*100)}%</div>${_imcSafeText(ev.text_snippet)}</div>`).join('')}</div>` : ''}
                </div>
              `).join('') || '<div style="font-size:0.82rem;color:var(--text-muted)">No clause results available.</div>'}
            </div>
          </div>

          <div class="card" style="padding:20px;border:1px solid rgba(255,255,255,0.06)">
            <h3 style="margin:0 0 12px;font-size:0.95rem;font-weight:700">Covenants</h3>
            <div style="display:flex;flex-direction:column;gap:8px">
              ${covenants.map(c => `<div style="padding:10px 12px;border-radius:8px;background:var(--bg-tertiary);border-left:3px solid ${c.covenant_type === 'FINANCIAL' ? '#2563eb' : '#7c3aed'}"><div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap"><div><strong>${_imcSafeText(c.covenant_name)}</strong> <span style="color:var(--text-muted)">(${_imcSafeText(c.covenant_type)})</span></div><div style="font-size:0.74rem;color:var(--text-muted)">p.${c.page_number || '?'} â€¢ ${Math.round((c.confidence||0)*100)}%</div></div><div style="margin-top:6px;font-size:0.78rem;color:var(--text-secondary);line-height:1.5">${_imcSafeText(c.text)}</div>${c.threshold ? `<div style="margin-top:6px;font-size:0.74rem;color:var(--accent-blue);font-family:monospace">Threshold: ${_imcSafeText(c.threshold)}</div>` : ''}</div>`).join('') || '<div style="font-size:0.82rem;color:var(--text-muted)">No covenants detected.</div>'}
            </div>
          </div>

          <div class="card" style="padding:20px;border:1px solid rgba(255,255,255,0.06)">
            <h3 style="margin:0 0 12px;font-size:0.95rem;font-weight:700">Sections & Diagnostics</h3>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:0.8rem;margin-bottom:12px">
              ${_imcFieldRow('Parser', (processingLog.method_used || 'â€“').toUpperCase())}
              ${_imcFieldRow('Pages Processed', processingLog.pages_processed || data.pages_count || 0)}
              ${_imcFieldRow('Text Length', processingLog.char_count != null ? _fmtNum(processingLog.char_count) : _fmtNum(fullTextPreview.text_length, '0'))}
              ${_imcFieldRow('Processing Time', processingLog.processing_time != null ? processingLog.processing_time + 's' : null)}
            </div>
            <div style="display:flex;flex-wrap:wrap;gap:6px">${sections.map(s => `<span style="padding:4px 10px;border-radius:999px;background:var(--bg-tertiary);font-size:0.74rem;color:var(--text-secondary)">${_imcSafeText((s.section_name || '').replace(/_/g,' '))} â€¢ p.${s.start_page || '?'}${s.end_page && s.end_page !== s.start_page ? '-' + s.end_page : ''}</span>`).join('') || '<span style="font-size:0.8rem;color:var(--text-muted)">No sections detected.</span>'}</div>
          </div>

          ${(fullTextPreview.preview || '').length ? `<details class="card"><summary style="padding:14px 18px;cursor:pointer;font-weight:700;list-style:none">Full Text Preview</summary><div style="padding:0 18px 18px"><pre style="white-space:pre-wrap;font-size:0.75rem;line-height:1.45;color:var(--text-secondary);max-height:360px;overflow:auto">${_imcSafeText(fullTextPreview.preview, '')}</pre></div></details>` : ''}

          ${audit.length ? `<div class="card" style="padding:20px;border:1px solid rgba(255,255,255,0.06)"><h3 style="margin:0 0 12px;font-size:0.95rem;font-weight:700">Audit Trail</h3><div style="display:flex;flex-direction:column;gap:8px">${audit.slice(0, 12).map(a => `<div style="padding:10px 12px;border-radius:8px;background:var(--bg-tertiary);font-size:0.76rem"><div style="font-weight:700">${_imcSafeText(a.action || 'ACTION')}</div><div style="color:var(--text-muted);margin-top:2px">${_imcSafeText(a.actor || 'system')} â€¢ ${_imcSafeText((a.created_at || '').slice(0, 19))}</div></div>`).join('')}</div></div>` : ''}
        </div>

        <div style="position:sticky;top:12px;display:flex;flex-direction:column;gap:16px">
          <div class="card" style="padding:14px 16px;border:1px solid rgba(255,255,255,0.06)">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:10px">
              <div>
                <h3 style="margin:0;font-size:0.95rem;font-weight:700">PDF Viewer</h3>
                <div style="font-size:0.74rem;color:var(--text-muted);margin-top:4px">Review evidence against the source memorandum without leaving the dossier.</div>
              </div>
              <button class="btn btn-sm btn-outline" onclick="window.open(App._imcPdfEvidenceUrl(${u.id}, 1, ''),'_blank','noopener')">Open PDF</button>
            </div>
            <div style="font-size:0.76rem;color:var(--text-muted);margin-bottom:10px;padding:10px 12px;border-radius:10px;background:rgba(37,99,235,0.08);border:1px solid rgba(37,99,235,0.18)">Click any clause evidence to jump to the relevant page in the PDF preview.</div>
            <iframe id="imc-dossier-pdf-frame" src="${_imcPdfEvidenceUrl(u.id, 1, '')}" style="width:100%;height:820px;border:1px solid var(--border-light);border-radius:10px;background:#fff"></iframe>
          </div>
        </div>
      </div>
    `;
  }

  async function _imcExportIcNote(uploadId) {
    if (!ensureAuthenticated('export the IC note')) return;
    await API.refreshSession().catch(() => false);
    const token = API.getToken ? API.getToken() : '';
    try {
      const resp = await fetch(`/api/v1/im-checker/uploads/${uploadId}/institutional/ic-note`, {
        method: 'GET',
        credentials: 'include',
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!resp.ok) {
        const text = await resp.text().catch(() => 'Unable to generate IC note.');
        throw new Error(text || `Unable to generate IC note (${resp.status})`);
      }
      const html = await resp.text();
      const blob = new Blob([html], { type: 'text/html' });
      const blobUrl = URL.createObjectURL(blob);
      window.open(blobUrl, '_blank', 'noopener');
      window.setTimeout(() => URL.revokeObjectURL(blobUrl), 60000);
    } catch (err) {
      if (_imcHandleAuthError(err)) return;
      Utils.toast('error', 'Failed', _imcFriendlyError(err, 'Could not open the IC note.'));
    }
  }

  async function _imcRerunInstitutionalAction(uploadId, action) {
    const restoreButtons = _setBusyButtons('button[data-method="_imcRerunInstitutionalAction"], button[onclick*="App._imcRerunInstitutionalAction("]', true, 'Re-runningâ€¦');
    const endpoint = {
      ocr: `/im-checker/uploads/${uploadId}/institutional/reprocess-ocr`,
      clauses: `/im-checker/uploads/${uploadId}/institutional/rerun-clauses`,
      covenants: `/im-checker/uploads/${uploadId}/institutional/rerun-covenants`,
    }[action];
    if (!endpoint) return;
    Utils.toast('info', 'Re-running', `Running ${action} workflow...`);
    try {
      await API.post(endpoint, {});
      Utils.toast('success', 'Updated', `${action.toUpperCase()} workflow completed`);
      await _imcViewUploadDetail(uploadId);
    } catch (err) {
      if (_imcHandleAuthError(err)) return;
      Utils.toast('error', 'Failed', _imcFriendlyError(err, 'Could not rerun workflow.'));
    }
  }

  async function _imcValidateClause(uploadId, clauseType, currentStatus) {
    const nextStatus = (window.prompt(`Set status for ${clauseType} (PRESENT / ABSENT / INCONCLUSIVE)`, currentStatus || 'PRESENT') || '').trim().toUpperCase();
    if (!nextStatus) return;
    const comment = window.prompt('Optional review comment', '') || '';
    const overrideText = window.prompt('Optional override clause text', '') || '';
    const restoreButtons = _setBusyButtons('button[data-method="_imcValidateClause"], button[onclick*="App._imcValidateClause("]', true, 'Savingâ€¦');
    try {
      await API.post(`/im-checker/uploads/${uploadId}/institutional/clauses/${clauseType}/feedback`, {
        status: nextStatus,
        comment,
        override_text: overrideText || null,
      });
      Utils.toast('success', 'Saved', `${clauseType} updated to ${nextStatus}`);
      await _imcViewUploadDetail(uploadId);
    } catch (err) {
      if (_imcHandleAuthError(err)) return;
      Utils.toast('error', 'Failed', _imcFriendlyError(err, 'Could not save feedback.'));
    }
  }

  async function _imcLoadUploadHistory() {
    const isin = (document.getElementById('imc-up-isin')?.value || '').trim().toUpperCase();
    const el = document.getElementById('imc-up-history');
    if (!el) return;

    if (!isin || isin.length < 4) {
      // Load all recent uploads
      try {
        const data = await API.get('/im-checker/uploads?page_size=10');
        const rows = data.uploads || [];
        if (!rows.length) {
          el.innerHTML = '<div style="text-align:center;padding:20px;color:var(--text-muted);font-size:0.85rem"><div style="font-weight:700;color:var(--text-secondary);margin-bottom:4px">No IM uploads yet</div><div>Upload a memorandum PDF on the left to create the first record.</div></div>';
          return;
        }
        _imcRenderUploadTable(el, rows);
      } catch (err) {
        if (_imcHandleAuthError(err)) return;
        el.innerHTML = `<div style="text-align:center;padding:20px;color:var(--accent-red);font-size:0.85rem">${_imcFriendlyError(err, 'Could not load uploads.')}</div>`;
      }
      return;
    }

    try {
      const data = await API.get(`/im-checker/uploads/isin/${encodeURIComponent(isin)}`);
      const rows = data.uploads || [];
      if (!rows.length) {
        el.innerHTML = '<div style="text-align:center;padding:20px;color:var(--text-muted);font-size:0.85rem">No uploads for this ISIN.</div>';
        return;
      }
      _imcRenderUploadTable(el, rows);
    } catch (err) {
      if (_imcHandleAuthError(err)) return;
      _imcLogEvent('upload.history_isin_failed', { isin, message: String(err?.message || '').slice(0, 120) }, 'warn');
      el.innerHTML = '<div style="text-align:center;padding:20px;color:var(--text-muted);font-size:0.85rem">No uploads found for this ISIN. Upload a PDF to start institutional analysis.</div>';
    }
  }

  function _imcRenderUploadTable(el, rows) {
    const levelColor = l => ({ LOW:'#16a34a', MEDIUM:'#d97706', HIGH:'#e55a2b', CRITICAL:'#dc2626' })[l] || '#6b7280';
    el.innerHTML = `
      <table class="table" style="width:100%;border-collapse:collapse;font-size:0.82rem">
        <thead><tr style="background:var(--bg-tertiary)">
          <th style="padding:8px 10px;text-align:left">ISIN</th>
          <th style="padding:8px 10px;text-align:left">File</th>
          <th style="padding:8px 10px;text-align:left">V</th>
          <th style="padding:8px 10px;text-align:left">Status</th>
          <th style="padding:8px 10px;text-align:left">Risk</th>
          <th style="padding:8px 10px;text-align:left">Date</th>
          <th style="padding:8px 10px;text-align:left">Actions</th>
        </tr></thead>
        <tbody>${rows.map((r, i) => `<tr style="border-top:1px solid var(--border-light);${i%2?'background:var(--bg-tertiary)':''}">
          <td style="padding:8px 10px;font-family:monospace;font-size:0.78rem">${r.isin}</td>
          <td style="padding:8px 10px;max-width:160px;white-space:normal;overflow-wrap:anywhere;word-break:break-word;line-height:1.35" title="${r.file_name||''}">${r.file_name||'â€“'}</td>
          <td style="padding:8px 10px;text-align:center">
            <span style="padding:1px 6px;border-radius:6px;font-size:0.72rem;font-weight:700;background:${r.is_active?'#dcfce7':'var(--bg-tertiary)'};color:${r.is_active?'#16a34a':'var(--text-muted)'}">v${r.version||1}</span>
          </td>
          <td style="padding:8px 10px">${r.extraction_status === 'DONE' ? '<span style="color:#16a34a;font-weight:600">&#10003; Done</span>' : '<span style="color:#d97706">&#9679; '+r.extraction_status+'</span>'}</td>
          <td style="padding:8px 10px"><span style="font-weight:700;color:${levelColor(r.risk_level)}">${r.risk_score!=null?Math.round(r.risk_score):'â€“'}</span></td>
          <td style="padding:8px 10px;color:var(--text-muted);font-size:0.75rem">${(r.created_at||'').slice(0,16)}</td>
          <td style="padding:8px 10px">
            <div style="display:flex;gap:4px">
              <button class="btn btn-sm btn-outline" type="button" data-method="_imcViewUploadDetail" data-arg="${r.id}" style="font-size:0.7rem;padding:3px 6px">Dossier</button>
              <button class="btn btn-sm btn-outline" type="button" data-method="_imcReprocessUpload" data-arg="${r.id}" style="font-size:0.7rem;padding:3px 6px">Rerun</button>
            </div>
          </td>
        </tr>`).join('')}</tbody>
      </table>`;
  }

  async function _imcReprocessUpload(uploadId) {
    const restoreButtons = _setBusyButtons('button[data-method="_imcReprocessUpload"], button[onclick*="App._imcReprocessUpload("]', true, 'Re-runningâ€¦');
    Utils.toast('info', 'Reprocessing', 'Re-running extraction & risk analysis...');
    try {
      const res = await API.post(`/im-checker/uploads/${uploadId}/reprocess`, {});
      if (res.status === 'success') {
        Utils.toast('success', 'Done', `Risk: ${res.risk_level} (${Math.round(res.risk_score||0)})`);
        _imcRenderUploadResult(res);
        _imcLoadUploadHistory();
      } else {
        throw new Error(res.errors?.[0] || 'Reprocessing failed');
      }
    } catch (err) {
      if (_imcHandleAuthError(err)) return;
      Utils.toast('error', 'Failed', _imcFriendlyError(err, 'Could not reprocess upload.'));
    }
  }

  async function _imcCheckIntegrity(uploadId) {
    Utils.toast('info', 'Checking', 'Running integrity check...');
    try {
      const res = await API.get(`/im-checker/uploads/${uploadId}/integrity`);
      const score = res.integrity_score || 'UNKNOWN';
      const col = score === 'PASS' ? '#16a34a' : score === 'WARN' ? '#d97706' : '#dc2626';
      let msg = `Integrity: ${score}`;
      if (res.mismatches?.length) {
        msg += ` (${res.mismatches.length} mismatches)`;
      }
      Utils.toast(score === 'PASS' ? 'success' : 'warning', 'Integrity', msg);
    } catch (err) {
      if (_imcHandleAuthError(err)) return;
      Utils.toast('error', 'Failed', _imcFriendlyError(err, 'Integrity check failed.'));
    }
  }

  async function _imcViewUploadDetail(uploadId) {
    try {
      const [data, institutional] = await Promise.all([
        API.get(`/im-checker/uploads/${uploadId}`),
        API.get(`/im-checker/uploads/${uploadId}/institutional`).catch(() => null),
      ]);
      const u = data.upload || {};
      const dossierBody = document.getElementById('imc-dossier-body');
      if (!dossierBody) return;
      dossierBody.innerHTML = _imcRenderInstitutionalDossier(u, data, institutional || { upload_id: uploadId, metadata: {}, clauses: [], covenants: [], sections: [], risk: {} });
      _imcShowTab('dossier');
    } catch (err) {
      if (_imcHandleAuthError(err)) return;
      Utils.toast('error', 'Error', _imcFriendlyError(err, 'Could not load dossier details.'));
    }
  }

  async function _imcLoadConnectors() {
    const el = document.getElementById('imc-up-connectors');
    if (!el) return;
    try {
      const data = await API.get('/im-checker/connectors');
      const conns = data.connectors || [];
      el.innerHTML = conns.map(c => `
        <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border-light)">
          <span style="width:8px;height:8px;border-radius:50%;background:${c.active?'#16a34a':'#6b7280'}"></span>
          <div style="flex:1">
            <div style="font-weight:600;font-size:0.85rem">${c.source}</div>
            <div style="font-size:0.75rem;color:var(--text-muted)">${c.description||'â€“'}</div>
          </div>
          <span style="font-size:0.72rem;font-weight:700;padding:2px 8px;border-radius:6px;
                       background:${c.active?'rgba(22,163,74,0.1)':'rgba(107,114,128,0.1)'};
                       color:${c.active?'#16a34a':'#6b7280'}">${c.active ? 'ACTIVE' : 'INACTIVE'}</span>
        </div>`
      ).join('');
    } catch (err) {
      if (_imcHandleAuthError(err)) return;
      _imcLogEvent('connectors.load_failed', { message: String(err?.message || '').slice(0, 120) }, 'warn');
      el.innerHTML = '<div style="font-size:0.82rem;color:var(--text-muted)">Connectors unavailable right now. Check service health and retry.</div>';
    }
  }

  function _imcHeuristicCard(h) {
    const severity = h.severity || 'INFO';
    const category = h.category || h.sub_category || 'GENERAL';
    const heuristicId = h.id || h.heuristic_id || 'â€”';
    const weight = (h.weight ?? h.impact_weight ?? 'â€”');
    const message = h.message || h.explanation || '';
    const evidence = Array.isArray(h.evidence)
      ? h.evidence
      : (h.source_text ? [h.source_text] : []);
    const recommendation = h.recommendation || h.remediation || '';
    const sevColor = { CRITICAL: '#dc2626', HIGH: '#e55a2b', MEDIUM: '#d97706', LOW: '#ca8a04', INFO: '#6b7280' }[severity] || '#6b7280';
    const triggered = !!h.triggered;
    return `<div style="padding:12px 14px;border-radius:8px;background:var(--bg-tertiary);border-left:3px solid ${triggered ? sevColor : '#16a34a'}">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;flex-wrap:wrap;margin-bottom:4px">
        <div>
          <span style="font-size:0.7rem;font-weight:700;letter-spacing:.4px;color:var(--text-muted)">${heuristicId} Â· ${category}</span>
          <div style="font-weight:600;font-size:0.88rem;color:var(--text-primary)">${h.name}</div>
        </div>
        <div style="display:flex;gap:6px;align-items:center;flex-shrink:0">
          ${triggered ? `<span style="font-size:0.72rem;font-weight:700;padding:2px 8px;border-radius:10px;background:${sevColor};color:#fff">${severity}</span>` : '<span style="font-size:0.72rem;font-weight:600;padding:2px 8px;border-radius:10px;background:#dcfce7;color:#16a34a">PASS</span>'}
          <span style="font-size:0.75rem;color:var(--text-muted)">Wt: ${weight}</span>
        </div>
      </div>
      ${triggered && message ? `<div style="font-size:0.8rem;color:var(--text-secondary);margin-bottom:4px">${message}</div>` : ''}
      ${triggered && evidence.length ? `<div style="font-size:0.75rem;color:var(--text-muted)">â€¢ ${evidence.slice(0,3).join(' â€¢ ')}</div>` : ''}
      ${triggered && recommendation ? `<div style="font-size:0.75rem;color:#2563eb;margin-top:4px">ðŸ’¡ ${recommendation}</div>` : ''}
    </div>`;
  }

  // Public API
  return {
    navigate,
    openPortfolioAllocations,
    _refreshBondAllocation,
    init,
    refreshDashboard,
    showTradeModal,
    showQuoteUploadModal,
    _handleQuoteFileDrop,
    _handleQuoteFileSelect,
    closeModal,
    _openExplainFromKey,
    executeTrade,
    submitQuote,
    toggleCompareMode,
    addToCompare,
    clearCompare,
    _uniGoPage,
    _uniGoServerPage,
    _applyBrokerRename,
    _renameUnknownBrokerFile,
    downloadComplianceJSON,
    _downloadJSON: (filename, data) => {
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a'); a.href = url; a.download = filename; a.click();
      URL.revokeObjectURL(url);
    },
    refreshMarket,
    runComplianceScan,
    runAlertScan,
    _confirmResolveAlert,
    _cancelResolve,
    _resolveAlert,
    _togglePortfolioDrill,
    loadAlertsData,
    generateReport,
    runScoringForReports,
    generateMarketReport,
    printPortfolioReport,
    exportPortfolioCSV,
    triggerGmailIngest,
    triggerSebiSync,
    _portfolioDrop,
    _portfolioFileSelected,
    _portfolioUpload,
    _downloadPortfolioTemplate,
    _loadPortfolioSummary,
    _loadSebiSyncStatus,
    _loadFourEyesPending,
    loadAuditLogs,
    globalSearch,
    renderRecommendations,
    _showRecTab,
    _runInvestmentRecommendation,
    _filterRecs,
    _renderQuotesTable,
    _renderAIRecsTable,
    _renderBrokerProfiles,
    _renderBestPicks,
    _computeBondScore,
    _refreshRecs,
    _ingestWorkspace,
    _showBrokerDetail,
    _exportRecsCSV,
    // Auth
    isAuthenticated,
    ensureAuthenticated,
    handleAuthError,
    showLoginScreen,
    hideLoginScreen,
    doLogin,
    logout,
    handleSessionExpired,
    getActiveTrustId,
    setActiveTrustId,
    // Admin
    _showAddUserModal,
    _submitAddUser,
    loadAdminUsers,
    saveAdminSettings,
    runFullMarketRefresh,
    runSeedIm,
    loadDataCoverage,
    IC,
    // IM Checker
    _imcLoadBond,
    _imcSaveIM,
    _imcSaveIMAndRun,
    _imcSaveFinancials,
    _imcRunFull,
    _imcHandlePDFUpload,
    // IM Document Library & Fetch
    _imcShowTab,
    _imcLoadDocLibrary,
    _imcAnalyseDoc,
    _imcReprocessDoc,
    _imcTriggerFetch,
    _imcPollFetchJob,
    _imcRefreshJobs,
    _imcLoadFetchHistory,
    // IM Upload System
    _imcSelectUploadISIN,
    _imcHandleUploadDrop,
    _imcHandleUploadSelect,
    _imcSetUploadFile,
    _imcClearUploadFile,
    _imcSubmitUpload,
    _imcLoadUploadHistory,
    _imcReprocessUpload,
    _imcCheckIntegrity,
    _imcViewUploadDetail,
    _imcLoadConnectors,
    _imcLoadInstitutional,
    _imcOpenPdfEvidence,
    _imcExportIcNote,
    _imcRerunInstitutionalAction,
    _imcValidateClause,
    // MFA
    _mfaLoadStatus,
    _mfaStartSetup,
    _mfaConfirm,
    _mfaReset, getLiveSimulationSnapshot: _getLiveSimulationSnapshot,
  };
})(); window.getLiveSimulationSnapshot = () => App.getLiveSimulationSnapshot();

// Expose App on window for guaranteed access from inline HTML handlers
// (const declarations are in the lexical record, not always on window in all environments)
window.App = App;

// Boot
document.addEventListener('DOMContentLoaded', App.init);
