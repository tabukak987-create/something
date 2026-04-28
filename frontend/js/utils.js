/* ═══════════════════════════════════════════════════════════════════
   PF Trust — Utility Functions
   ═══════════════════════════════════════════════════════════════════ */

const Utils = (() => {
  function toFiniteNumber(val) {
    if (val == null) return null;
    if (typeof val === 'string' && val.trim() === '') return null;
    const numeric = Number(val);
    return Number.isFinite(numeric) ? numeric : null;
  }

  // ── Number Formatting ────────────────────────────────────────
  function formatINR(val, decimals = 2) {
    const safeVal = toFiniteNumber(val);
    if (safeVal == null) return '₹0';
    const abs = Math.abs(safeVal);
    let str;
    if (abs >= 1e7) str = '₹' + (safeVal / 1e7).toFixed(decimals) + ' Cr';
    else if (abs >= 1e5) str = '₹' + (safeVal / 1e5).toFixed(decimals) + ' L';
    else str = '₹' + safeVal.toLocaleString('en-IN', { maximumFractionDigits: decimals });
    return str;
  }

  function formatCrores(val, dp = 2) {
    const safeVal = toFiniteNumber(val);
    if (safeVal == null) return '₹0 Cr';
    return '₹' + safeVal.toFixed(dp) + ' Cr';
  }

  function formatPct(val, dp = 2) {
    const safeVal = toFiniteNumber(val);
    if (safeVal == null) return '—';
    return safeVal.toFixed(dp) + '%';
  }

  function normalizePercentValue(val) {
    const numeric = toFiniteNumber(val);
    if (numeric == null) return null;
    // Yield feeds can arrive as decimal fractions (0.086) or percent (8.6).
    if (Math.abs(numeric) > 0 && Math.abs(numeric) <= 1) return numeric * 100;
    return numeric;
  }

  function formatYieldPct(val, dp = 4) {
    const normalized = normalizePercentValue(val);
    if (normalized == null) return '—';
    return normalized.toFixed(dp) + '%';
  }

  function formatNum(val, dp = 2) {
    const safeVal = toFiniteNumber(val);
    if (safeVal == null) return '—';
    return safeVal.toLocaleString('en-IN', { minimumFractionDigits: dp, maximumFractionDigits: dp });
  }

  function parseDateSafe(d) {
    if (!d) return null;
    const dt = new Date(d);
    if (isNaN(dt)) return null;
    const y = dt.getFullYear();
    // Guard against common placeholder/bad dates in vendor files.
    if (y < 1950 || y > 2200) return null;
    return dt;
  }

  function formatDate(d) {
    const dt = parseDateSafe(d);
    if (!dt) return '—';
    return dt.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' });
  }

  function formatDateTime(d) {
    const dt = parseDateSafe(d);
    if (!dt) return '—';
    return dt.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' }) +
      ' ' + dt.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' });
  }

  function timeAgo(d) {
    const dt = parseDateSafe(d);
    if (!dt) return '';
    const diff = Math.max(0, Date.now() - dt.getTime());
    if (diff < 60000) return 'just now';
    if (diff < 3600000) return Math.floor(diff / 60000) + 'm ago';
    if (diff < 86400000) return Math.floor(diff / 3600000) + 'h ago';
    return Math.floor(diff / 86400000) + 'd ago';
  }

  // ── Rating Utilities ─────────────────────────────────────────
  const RATING_ORDER = { 'SOVEREIGN': 0, 'AAA': 1, 'AA+': 2, 'AA': 3, 'AA-': 4, 'A+': 5, 'A': 6, 'A-': 7, 'BBB+': 8, 'BBB': 9 };
  const RATING_COLORS = { 'SOVEREIGN': '#16a34a', 'AAA': '#16a34a', 'AA+': '#22c55e', 'AA': '#84cc16', 'AA-': '#eab308', 'A+': '#f59e0b', 'A': '#f97316', 'BBB': '#ef4444' };

  function ratingRank(r) {
    // parseRatingStr strips compound formats like 'AAA&CARE' before lookup
    const { rating } = parseRatingStr(r);
    return RATING_ORDER[rating] ?? 99;
  }
  function ratingColor(r) { const { rating } = parseRatingStr(r); return RATING_COLORS[rating] || '#94a3b8'; }
  function isInvestmentGrade(r) { return ratingRank(r) <= 4; }

  function compactChip(label, tone = 'neutral') {
    return `<span class="compact-chip compact-chip-${tone}">${escapeHTML(String(label))}</span>`;
  }

  /**
   * Parse a raw broker-supplied rating string into { rating, agency }.
   * Handles compound formats like "AAA&CARE", "Sovereign", "AA+&ICRA&BRICKWORK".
   * @param {string} raw  - The raw rating string (may be compound)
   * @param {string} [agencyHint] - Pre-known agency value to prefer over extraction
   */
  function parseRatingStr(raw, agencyHint) {
    if (!raw) return { rating: null, agency: agencyHint || null };
    const s = raw.toString().trim();
    if (!s || /^select/i.test(s) || s === 'N/A' || s === 'n/a' || s === '-') {
      return { rating: null, agency: agencyHint || null };
    }
    // Sovereign variants
    if (/^sovereign/i.test(s)) return { rating: 'SOVEREIGN', agency: agencyHint || 'Govt of India' };
    // Known AGENCY token patterns embedded in rating string
    const AGENCY_TOKENS = ['INDIARATINGS', 'INDIARATING', 'BRICKWORKS', 'BRICKWORK', 'CRISIL', 'ICRA', 'CARE', 'ACUITE'];
    const AGENCY_LABELS = { 'INDIARATINGS': 'India Ratings', 'INDIARATING': 'India Ratings', 'BRICKWORKS': 'Brickwork', 'BRICKWORK': 'Brickwork', 'CRISIL': 'CRISIL', 'ICRA': 'ICRA', 'CARE': 'CARE Ratings', 'ACUITE': 'Acuité' };
    // Split on & delimiter; first part = rating, rest = agency tokens
    const parts = s.split(/&/).map(p => p.trim()).filter(Boolean);
    let ratingRaw = parts[0].replace(/ by /gi, '').replace(/\(CE\)|\(SO\)|\(IND\)|\(NA\)/gi, '').trim().toUpperCase();
    // Validate rating looks like a proper rating symbol (starts with A-Z, not all digits)
    if (!/^[A-Z]/.test(ratingRaw)) ratingRaw = null;
    const agencyTokens = parts.slice(1).map(p => p.toUpperCase().trim()).filter(p => p.length > 0);
    let derivedAgency = agencyHint || null;
    if (!derivedAgency && agencyTokens.length) {
      const known = agencyTokens.find(t => AGENCY_TOKENS.includes(t));
      derivedAgency = known ? AGENCY_LABELS[known] : agencyTokens.join('/');
    }
    return { rating: ratingRaw, agency: derivedAgency };
  }

  function ratingBadge(r, agencyHint) {
    const { rating } = parseRatingStr(r, agencyHint);
    if (!rating) return compactChip('NR', 'neutral');
    const rank = ratingRank(rating);
    const tone = rank <= 1 ? 'success' : rank <= 4 ? 'info' : 'warning';
    return compactChip(rating, tone);
  }

  /**
   * Render a small muted agency tag.
   * Falls back to extracting the agency from the raw rating string if agency field is blank.
   * @param {string} agency      - The rating_agency field value
   * @param {string} [rawRating] - The raw credit_rating string (for extraction fallback)
   */
  function ratingAgencyTag(agency, rawRating) {
    const { agency: extracted } = parseRatingStr(rawRating || '', agency);
    const label = (agency && !/^select/i.test(agency.trim())) ? agency.trim() : extracted;
    if (!label) return '';
    return `<span style="font-size:0.62rem;color:var(--text-muted);margin-left:4px;font-weight:500">${label}</span>`;
  }

  // ── Sector Colors ────────────────────────────────────────────
  const SECTOR_COLORS = [
    '#2563eb', '#7c3aed', '#0d9488', '#d97706', '#dc2626',
    '#16a34a', '#4f46e5', '#0891b2', '#be185d', '#65a30d',
    '#8b5cf6', '#ea580c', '#0284c7', '#c026d3', '#ca8a04'
  ];

  function sectorColor(idx) { return SECTOR_COLORS[idx % SECTOR_COLORS.length]; }

  // ── Status Helpers ───────────────────────────────────────────
  function statusBadge(status) {
    const s = (status || '').toUpperCase();
    if (s === 'PASS' || s === 'GREEN' || s === 'OK' || s === 'RESOLVED') return compactChip('✓ ' + s, 'success');
    if (s === 'FAIL' || s === 'RED' || s === 'CRITICAL' || s === 'OPEN') return compactChip('✗ ' + s, 'danger');
    if (s === 'WARN' || s === 'AMBER' || s === 'WARNING') return compactChip('⚠ ' + s, 'warning');
    if (s === 'INFO') return compactChip(s, 'info');
    return compactChip(s || '—', 'neutral');
  }

  function severityClass(sev) {
    const s = (sev || '').toUpperCase();
    if (s === 'CRITICAL') return 'critical';
    if (s === 'WARNING') return 'warning';
    return 'info';
  }

  function confidenceBadge(level) {
    const v = String(level || 'low').toLowerCase();
    if (v === 'high') return compactChip('HIGH', 'success');
    if (v === 'medium') return compactChip('MED', 'warning');
    return compactChip('LOW', 'neutral');
  }

  // ── Loading / Empty helpers ──────────────────────────────────
  function loadingHTML(msg = 'Loading data...') {
    return `<div class="empty-state"><div class="spinner" style="width:32px;height:32px;margin-bottom:16px"></div><div class="empty-state-title">${escapeHTML(msg)}</div></div>`;
  }

  function emptyHTML(title, desc) {
    return `<div class="empty-state">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4"/></svg>
      <div class="empty-state-title">${escapeHTML(title)}</div>
      <div class="empty-state-desc">${escapeHTML(desc || '')}</div>
    </div>`;
  }

  function skeletonCards(n = 6) {
    return '<div class="metric-grid">' + Array(n).fill('<div class="skeleton skeleton-card"></div>').join('') + '</div>';
  }

  function skeletonTable(rows = 5) {
    return '<div style="padding:16px">' +
      Array(rows).fill('<div class="skeleton skeleton-text" style="margin-bottom:12px"></div>').join('') +
      '</div>';
  }

  // ── Toast ────────────────────────────────────────────────────
  function toast(type, title, message, duration = 4000) {
    const container = document.getElementById('toast-container');
    if (!container) return;
    const icons = {
      success: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>',
      error: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 9v2m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>',
      warning: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg>',
      info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>',
    };
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.innerHTML = `
      <div class="toast-icon">${icons[type] || icons.info}</div>
      <div class="toast-content"><div class="toast-title">${escapeHTML(title)}</div><div class="toast-message">${escapeHTML(message || '')}</div></div>
      <button class="toast-close" onclick="this.parentElement.remove()">✕</button>
    `;
    container.appendChild(el);
    setTimeout(() => el.remove(), duration);
  }

  // ── Debounce ─────────────────────────────────────────────────
  function debounce(fn, ms = 300) {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  }

  // ── Sort helper for tables ───────────────────────────────────
  function sortData(data, key, asc = true) {
    return [...data].sort((a, b) => {
      let va = a[key], vb = b[key];
      if (va == null) return 1;
      if (vb == null) return -1;
      if (typeof va === 'string') va = va.toLowerCase();
      if (typeof vb === 'string') vb = vb.toLowerCase();
      if (va < vb) return asc ? -1 : 1;
      if (va > vb) return asc ? 1 : -1;
      return 0;
    });
  }

  // ── Export to CSV ────────────────────────────────────────────
  function exportCSV(data, filename) {
    if (!data.length) return;
    const headers = Object.keys(data[0]);
    const csv = [headers.join(','), ...data.map(r => headers.map(h => JSON.stringify(r[h] ?? '')).join(','))].join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
  }

  // ── HTML Sanitization ─────────────────────────────────────────
  function escapeHTML(str) {
    if (str == null) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /** Sanitize a value for safe embedding in an HTML attribute (e.g. onclick) */
  function escapeAttr(str) {
    if (str == null) return '';
    return String(str).replace(/[&<>"'`=\/]/g, c => `&#${c.charCodeAt(0)};`);
  }

  return {
    formatINR, formatCrores, formatPct, formatYieldPct, normalizePercentValue, formatNum, formatNumber: formatNum, parseDateSafe, formatDate, formatDateTime, timeAgo,
    parseRatingStr, ratingRank, ratingColor, isInvestmentGrade, ratingBadge, ratingAgencyTag, sectorColor,
    statusBadge, severityClass, confidenceBadge,
    loadingHTML, emptyHTML, skeletonCards, skeletonTable,
    toast, debounce, sortData, exportCSV, escapeHTML, escapeAttr, safe: escapeHTML
  };
})();
