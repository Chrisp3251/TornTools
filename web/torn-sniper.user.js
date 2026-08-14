// ==UserScript==
// @name         TornTools Live Market Sniper
// @namespace    http://127.0.0.1:8765/
// @version      0.1.1
// @description  Watches the Torn Item Market page you are actively viewing and alerts when a TornTools sniper target appears at or below your max price. Does not auto-buy.
// @match        https://www.torn.com/page.php*
// @grant        GM_xmlhttpRequest
// @grant        GM_notification
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';

  const CONFIG_URL = 'http://127.0.0.1:8765/api/sniper/config';
  const TELEMETRY_URL = 'http://127.0.0.1:8765/api/sniper/telemetry';
  const REFRESH_CONFIG_MS = 30000;
  const SCAN_DEBOUNCE_MS = 80;
  const HIGHLIGHT_CLASS = 'torntools-sniper-hit';
  const PANEL_ID = 'torntools-sniper-panel';

  let targets = new Map();
  let currentItemId = null;
  let scanTimer = null;
  let lastAlertSignature = null;
  let bestHit = null;
  let armed = true;

  function parseCurrentItemId() {
    const text = `${location.search}&${location.hash}`;
    const m = text.match(/(?:itemID|itemId|itemid)=(\d+)/i);
    return m ? Number(m[1]) : null;
  }

  function money(n) {
    return new Intl.NumberFormat('en-US', {style:'currency',currency:'USD',maximumFractionDigits:0}).format(Number(n || 0));
  }

  function fetchTargets() {
    GM_xmlhttpRequest({
      method: 'GET',
      url: CONFIG_URL,
      timeout: 3000,
      onload: response => {
        try {
          const data = JSON.parse(response.responseText || '{}');
          targets = new Map((data.items || []).filter(x => x.enabled !== false).map(x => [Number(x.item_id), x]));
          renderPanel();
          scheduleScan();
        } catch (e) {
          renderPanel(`Config error: ${e.message}`);
        }
      },
      onerror: () => renderPanel('TornTools localhost backend not reachable'),
      ontimeout: () => renderPanel('TornTools localhost backend timed out')
    });
  }

  function recordTelemetry(target, hit, signature, rowText) {
    try {
      GM_xmlhttpRequest({
        method: 'POST',
        url: TELEMETRY_URL,
        headers: {'Content-Type':'application/json'},
        data: JSON.stringify({
          item_id: Number(currentItemId),
          source: 'live_page',
          event_type: 'alert',
          price: Number(hit.price),
          max_price: Number(target.max_price),
          signature: `live:${signature}`,
          metadata: {
            page_url: location.href,
            row_text: rowText,
            observed_at_ms: Date.now()
          }
        }),
        timeout: 2500
      });
    } catch {}
  }

  function ensureStyles() {
    if (document.getElementById('torntools-sniper-style')) return;
    const style = document.createElement('style');
    style.id = 'torntools-sniper-style';
    style.textContent = `
      #${PANEL_ID}{position:fixed;right:18px;top:110px;z-index:2147483646;background:#111820;color:#eef4ff;border:1px solid #44536a;border-radius:10px;padding:10px 12px;min-width:260px;max-width:360px;font:13px/1.35 Arial,sans-serif;box-shadow:0 8px 28px rgba(0,0,0,.45)}
      #${PANEL_ID}.hit{background:#3a0d0d;border-color:#ff5b5b;box-shadow:0 0 0 2px rgba(255,70,70,.22),0 8px 28px rgba(0,0,0,.55)}
      #${PANEL_ID} .tt-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:6px}
      #${PANEL_ID} .tt-title{font-weight:800;letter-spacing:.04em}
      #${PANEL_ID} .tt-state{font-size:11px;opacity:.8}
      #${PANEL_ID} .tt-price{font-size:22px;font-weight:900;margin:4px 0}
      #${PANEL_ID} button{cursor:pointer;border:1px solid #60738d;border-radius:6px;background:#1f2b3a;color:#fff;padding:5px 8px;margin-top:7px}
      #${PANEL_ID} button:hover{background:#2a3a4d}
      .${HIGHLIGHT_CLASS}{outline:3px solid #ff4242 !important;outline-offset:-2px !important;background:rgba(255,55,55,.13) !important}
    `;
    document.documentElement.appendChild(style);
  }

  function renderPanel(errorText = '') {
    ensureStyles();
    let panel = document.getElementById(PANEL_ID);
    if (!panel) {
      panel = document.createElement('div');
      panel.id = PANEL_ID;
      document.body.appendChild(panel);
    }
    const target = currentItemId ? targets.get(currentItemId) : null;
    panel.classList.toggle('hit', !!bestHit);
    if (errorText) {
      panel.innerHTML = `<div class="tt-head"><span class="tt-title">TornTools Sniper</span><span class="tt-state">ERROR</span></div><div>${errorText}</div>`;
      return;
    }
    if (!currentItemId) {
      panel.innerHTML = `<div class="tt-head"><span class="tt-title">TornTools Sniper</span><span class="tt-state">${armed?'ARMED':'PAUSED'}</span></div><div>Open a specific Item Market page to arm a target.</div><button id="tt-toggle">${armed?'Pause':'Arm'} live watcher</button>`;
    } else if (!target) {
      panel.innerHTML = `<div class="tt-head"><span class="tt-title">TornTools Sniper</span><span class="tt-state">${armed?'ARMED':'PAUSED'}</span></div><div>Item #${currentItemId} is not on your TornTools sniper watchlist.</div><button id="tt-toggle">${armed?'Pause':'Arm'} live watcher</button>`;
    } else if (bestHit) {
      panel.innerHTML = `<div class="tt-head"><span class="tt-title">SNIPE NOW · ${target.name}</span><span class="tt-state">LIVE PAGE</span></div><div class="tt-price">${money(bestHit.price)}</div><div>At/below your ${money(target.max_price)} max. Click below to jump to the qualifying listing.</div><button id="tt-jump">Jump to listing</button> <button id="tt-toggle">Pause</button>`;
    } else {
      panel.innerHTML = `<div class="tt-head"><span class="tt-title">${target.name}</span><span class="tt-state">${armed?'LIVE WATCH':'PAUSED'}</span></div><div>Sniper max: <strong>${money(target.max_price)}</strong></div><div style="opacity:.75;margin-top:4px">Watching the market page you already loaded. No auto-buy.</div><button id="tt-toggle">${armed?'Pause':'Arm'} live watcher</button>`;
    }
    panel.querySelector('#tt-toggle')?.addEventListener('click', () => { armed = !armed; bestHit = null; clearHighlights(); renderPanel(); if (armed) scheduleScan(); });
    panel.querySelector('#tt-jump')?.addEventListener('click', () => bestHit?.row?.scrollIntoView({behavior:'smooth',block:'center'}));
  }

  function clearHighlights() {
    document.querySelectorAll(`.${HIGHLIGHT_CLASS}`).forEach(el => el.classList.remove(HIGHLIGHT_CLASS));
  }

  function isVisible(el) {
    if (!el || !(el instanceof Element)) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
  }

  function priceNumbers(text) {
    const out = [];
    const re = /\$\s*([0-9][0-9,]*)/g;
    let m;
    while ((m = re.exec(text || ''))) {
      const n = Number(m[1].replace(/,/g, ''));
      if (Number.isFinite(n) && n > 0) out.push(n);
    }
    return out;
  }

  function listingRowForBuyControl(control) {
    let node = control;
    for (let depth = 0; depth < 7 && node && node !== document.body; depth++, node = node.parentElement) {
      const text = (node.innerText || '').trim();
      if (text.includes('$') && text.length < 2500) return node;
    }
    return null;
  }

  function findQualifyingRows(target) {
    const controls = [...document.querySelectorAll('button,[role="button"],input[type="button"],input[type="submit"]')]
      .filter(isVisible)
      .filter(el => /\bbuy\b/i.test((el.innerText || el.value || el.getAttribute('aria-label') || '').trim()));
    const seen = new Set();
    const hits = [];
    for (const control of controls) {
      const row = listingRowForBuyControl(control);
      if (!row || seen.has(row)) continue;
      seen.add(row);
      const prices = priceNumbers(row.innerText || '');
      if (!prices.length) continue;
      const price = Math.min(...prices);
      if (price <= Number(target.max_price)) hits.push({row, control, price});
    }
    hits.sort((a,b) => a.price-b.price);
    return hits;
  }

  function alertHit(target, hit) {
    const rowText = (hit.row.innerText || '').replace(/\s+/g, ' ').slice(0, 300);
    const signature = `${currentItemId}:${hit.price}:${rowText}`;
    if (signature === lastAlertSignature) return;
    lastAlertSignature = signature;
    recordTelemetry(target, hit, signature, rowText);
    try {
      GM_notification({
        title: `TornTools SNIPE · ${target.name}`,
        text: `${money(hit.price)} is at/below your ${money(target.max_price)} max.`,
        timeout: 12000,
        onclick: () => { window.focus(); hit.row.scrollIntoView({behavior:'smooth',block:'center'}); }
      });
    } catch {}
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      const ctx = new Ctx();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain); gain.connect(ctx.destination);
      osc.frequency.value = 1250; gain.gain.value = 0.12;
      osc.start(); osc.stop(ctx.currentTime + 0.35);
    } catch {}
  }

  function scanPage() {
    scanTimer = null;
    const newItem = parseCurrentItemId();
    if (newItem !== currentItemId) {
      currentItemId = newItem;
      bestHit = null;
      lastAlertSignature = null;
      clearHighlights();
    }
    if (!armed || !currentItemId) { renderPanel(); return; }
    const target = targets.get(currentItemId);
    if (!target) { clearHighlights(); bestHit = null; renderPanel(); return; }
    const hits = findQualifyingRows(target);
    clearHighlights();
    hits.forEach(h => h.row.classList.add(HIGHLIGHT_CLASS));
    bestHit = hits[0] || null;
    if (bestHit) alertHit(target, bestHit);
    renderPanel();
  }

  function scheduleScan() {
    if (scanTimer) return;
    scanTimer = setTimeout(scanPage, SCAN_DEBOUNCE_MS);
  }

  const observer = new MutationObserver(scheduleScan);
  observer.observe(document.documentElement, {childList:true,subtree:true,characterData:true});

  let lastUrl = location.href;
  setInterval(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      scheduleScan();
    }
  }, 300);

  fetchTargets();
  setInterval(fetchTargets, REFRESH_CONFIG_MS);
  scheduleScan();
})();
