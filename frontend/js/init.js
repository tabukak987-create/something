/* ═══════════════════════════════════════════════════════════════════
   PF Trust — Inline Init Script (extracted from index.html)
   Runs after DOM is ready to set topbar date/time and restore theme.
   ═══════════════════════════════════════════════════════════════════ */
(function () {
  // ── Global error catcher (console only) ──
  window.addEventListener('error', function(ev) {
    var loc = ev.filename ? ev.filename.split('/').pop() + ':' + ev.lineno : 'unknown';
    console.error('[GlobalError]', ev.message || 'Unknown error', loc);
  });
  window.addEventListener('unhandledrejection', function(ev) {
    var r = ev.reason;
    console.error('[UnhandledRejection]', r && r.stack ? r.stack : (r && r.message ? r.message : String(r)));
  });

  // Topbar date/time
  var dateEl = document.getElementById('topbar-date');
  var timeEl = document.getElementById('topbar-time');
  if (dateEl) dateEl.textContent = new Date().toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' });
  if (timeEl) timeEl.textContent = new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' });

  // Restore theme
  var t = localStorage.getItem('theme');
  if (t === 'dark') {
    document.documentElement.setAttribute('data-theme', 'dark');
    var cb = document.getElementById('dark-mode-toggle');
    if (cb) cb.checked = true;
  }

  var trustSelector = document.getElementById('trust-selector');
  var trustApply = document.getElementById('trust-selector-apply');
  var trustOptions = document.getElementById('trust-selector-options');

  function loadTrustDirectory() {
    if (!trustSelector || !trustOptions || typeof API === 'undefined' || !API.listTrusts) return;

    return API.listTrusts({ limit: 25 })
      .then(function (payload) {
        var trusts = payload && Array.isArray(payload.trusts) ? payload.trusts : [];
        trustOptions.innerHTML = '';
        trusts.forEach(function (trust) {
          var option = document.createElement('option');
          option.value = trust && trust.trust_id ? trust.trust_id : 'default';
          if (trust && trust.label) option.label = trust.label;
          trustOptions.appendChild(option);
        });
        if (trusts.length && trustSelector.value === '') {
          trustSelector.value = (typeof App !== 'undefined' && App.getActiveTrustId) ? App.getActiveTrustId() : 'default';
        }
      })
      .catch(function () {
        return null;
      });
  }

  if (trustSelector && typeof App !== 'undefined' && App.getActiveTrustId) {
    trustSelector.value = App.getActiveTrustId();
  }
  if (trustSelector && trustApply && typeof App !== 'undefined' && App.setActiveTrustId) {
    var applyTrust = function () {
      App.setActiveTrustId(trustSelector.value);
    };
    trustApply.addEventListener('click', applyTrust);
    trustSelector.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        applyTrust();
      }
    });
  }
  if (trustSelector) {
    trustSelector.setAttribute('title', 'Type a trust id or choose one of the suggestions');
  }
  if (typeof window !== 'undefined') {
    window.addEventListener('pf:auth-changed', function (e) {
      if (e && e.detail && e.detail.authenticated) {
        loadTrustDirectory();
      }
    });
  }

  var alertsButton = document.getElementById('alerts-button');
  if (alertsButton && typeof App !== 'undefined' && App.navigate) {
    alertsButton.addEventListener('click', function (e) {
      e.preventDefault();
      App.navigate('alerts');
    });
  }

  var modalOverlay = document.getElementById('modal-overlay');
  if (modalOverlay && typeof App !== 'undefined' && App.closeModal) {
    modalOverlay.addEventListener('click', function (e) {
      if (e.target === modalOverlay) App.closeModal();
    });
  }

  var darkToggle = document.getElementById('dark-mode-toggle');
  if (darkToggle) {
    darkToggle.addEventListener('change', function () {
      document.documentElement.setAttribute('data-theme', this.checked ? 'dark' : 'light');
      localStorage.setItem('theme', this.checked ? 'dark' : 'light');
    });
  }

  function invokeDataMethod(actionEl) {
    if (!actionEl || typeof App === 'undefined') return false;
    var method = actionEl.getAttribute('data-method');
    if (!method || typeof App[method] !== 'function') return false;

    if (method === 'navigate') {
      var page = actionEl.getAttribute('data-page') || actionEl.getAttribute('data-route') || actionEl.getAttribute('data-arg') || '';
      if (!page) return false;
      var params = {};
      var isin = actionEl.getAttribute('data-isin');
      if (isin) params.isin = isin;
      if (Object.keys(params).length) {
        App.navigate(page, params);
      } else {
        App.navigate(page);
      }
      return true;
    }

    var args = [];
    var argsJson = actionEl.getAttribute('data-args');
    if (argsJson) {
      try {
        args = JSON.parse(argsJson);
      } catch (err) {
        args = [];
      }
    } else {
      var arg1 = actionEl.getAttribute('data-arg');
      var arg2 = actionEl.getAttribute('data-arg2');
      if (arg1 != null && arg1 !== '') args.push(arg1);
      if (arg2 != null && arg2 !== '') args.push(arg2);
    }

    App[method].apply(App, args);
    return true;
  }

  document.addEventListener('click', function (e) {
    var actionEl = e.target.closest('[data-action], [data-method]');
    if (!actionEl) return;
    var action = actionEl.getAttribute('data-action');
    if (action === 'close-modal') {
      if (typeof App !== 'undefined' && App.closeModal) App.closeModal();
      return;
    }
    if (action === 'close-and-navigate') {
      if (typeof App !== 'undefined' && App.closeModal) App.closeModal();
      if (typeof App !== 'undefined' && App.navigate) {
        var route = actionEl.getAttribute('data-route') || actionEl.getAttribute('data-page') || actionEl.getAttribute('data-arg') || 'dashboard';
        var params = {};
        var isin = actionEl.getAttribute('data-isin');
        if (isin) params.isin = isin;
        if (Object.keys(params).length) {
          App.navigate(route, params);
        } else {
          App.navigate(route);
        }
      }
      return;
    }
    if (action === 'submit-quote') {
      if (typeof App !== 'undefined' && App.submitQuote) App.submitQuote();
      return;
    }
    if (action === 'print-page') {
      window.print();
      return;
    }
    if (action === 'copy-clipboard') {
      var text = actionEl.getAttribute('data-text') || '';
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () {
          if (typeof Utils !== 'undefined' && Utils.toast) Utils.toast('success', 'Copied', 'Copied to clipboard');
        }).catch(function () {});
      }
      return;
    }
    if (action === 'open-url') {
      var url = actionEl.getAttribute('data-url');
      if (url) window.open(url, '_blank', 'noopener');
      return;
    }
    if (action === 'toggle-group') {
      var targetId = actionEl.getAttribute('data-target-id');
      var target = targetId ? document.getElementById(targetId) : null;
      if (target) {
        var isOpen = target.style.display !== 'none';
        target.style.display = isOpen ? 'none' : 'block';
        var chevron = actionEl.querySelector('.ag-chev');
        if (chevron) chevron.style.transform = isOpen ? '' : 'rotate(90deg)';
      }
      return;
    }
    if (invokeDataMethod(actionEl)) return;
  });

  document.addEventListener('input', function (e) {
    var actionEl = e.target.closest('[data-method]');
    if (!actionEl) return;
    invokeDataMethod(actionEl);
  });

  document.addEventListener('change', function (e) {
    var actionEl = e.target.closest('[data-method]');
    if (!actionEl) return;
    invokeDataMethod(actionEl);
  });

  // Close dropdown on outside click
  document.addEventListener('click', function (e) {
    var dd = document.getElementById('user-dropdown');
    if (dd && dd.classList.contains('show') && !e.target.closest('.topbar-avatar') && !e.target.closest('#user-dropdown')) {
      dd.classList.remove('show');
    }
  });

  // Belt-and-suspenders: also wire up login form via addEventListener
  // in case the onsubmit="App.doLogin(event)" inline attribute fails
  var loginForm = document.getElementById('login-form');
  if (loginForm && typeof App !== 'undefined' && App.doLogin) {
    loginForm.addEventListener('submit', function (e) {
      e.preventDefault();
      App.doLogin(e);
    });
  }

  // Programmatic nav-item click listener — fallback for CSP environments
  // that block inline onclick attributes in static HTML
  document.querySelectorAll('.nav-item[data-page]').forEach(function (el) {
    el.addEventListener('click', function (e) {
      e.preventDefault();
      var page = el.getAttribute('data-page');
      if (page && typeof App !== 'undefined' && App.navigate) {
        App.navigate(page);
      }
    });
  });
})();
