// ==UserScript==
// @name         TornTools Live Market Sniper
// @namespace    http://127.0.0.1:8765/
// @version      0.3.0
// @description  Watches the Torn Item Market page you are actively viewing, highlights live buy candidates, and relays a manual dashboard BUY 1 to one verified native Buy control. Never auto-confirms or buys stacks.
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
  const BUY_INTENT_CLAIM_URL = 'http://127.0.0.1:8765/api/sniper/manual-buy-intent/claim';
  const REFRESH_CONFIG_MS = 30000;
  const SCAN_DEBOUNCE_MS = 80;
  const BUY_INTENT_POLL_MS = 300;
  const HIGHLIGHT_CLASS = 'torntools-sniper-hit';
  const PANEL_ID = 'torntools-sniper-panel';

  let targets = new Map();
  let currentItemId = null;
  let scanTimer = null;
  let lastAlertSignature = null;
  let bestHit = null;
  let armed = true;
  let titleTimer = null;
  let titleFlip = false;
  let baseTitle = document.title;
  let intentPollBusy = false;

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

  function postTelemetry(target, hit, eventType, signature, metadata = {}) {
    try {
      GM_xmlhttpRequest({
        method: 'POST',
        url: TELEMETRY_URL,
        headers: {'Content-Type':'application/json'},
        data: JSON.stringify({
          item_id: Number(currentItemId),
          source: 'live_page',
          event_type: eventType,
          price: Number(hit?.price || 0) || null,
          max_price: Number(target?.max_price || 0) || null,
          baseline: Number(target?.learned_baseline || 0) || null,
          edge_pct: target?.learned_baseline && hit?.price ? ((Number(target.learned_baseline)-Number(hit.price))/Number(target.learned_baseline)*100) : null,
          signature,
          metadata: {
            page_url: location.href,
            observed_at_ms: Date.now(),
            configured_max_price: Number(target?.configured_max_price ?? target?.max_price ?? 0) || null,
            effective_max_price: Number(target?.effective_max_price ?? target?.max_price ?? 0) || null,
            ...metadata
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
      @keyframes ttSniperPulse{0%,100%{box-shadow:0 0 0 2px rgba(255,70,70,.18),0 8px 28px rgba(0,0,0,.55)}50%{box-shadow:0 0 0 4px rgba(255,85,85,.9),0 0 30px rgba(255,55,55,.55),0 8px 28px rgba(0,0,0,.65)}}
      @keyframes ttRowPulse{0%,100%{outline-color:rgba(255,65,65,.5);background:rgba(255,55,55,.10)}50%{outline-color:#ff3030;background:rgba(255,55,55,.24)}}
      #${PANEL_ID}{position:fixed;right:18px;top:110px;z-index:2147483646;background:#111820;color:#eef4ff;border:1px solid #44536a;border-radius:10px;padding:10px 12px;min-width:280px;max-width:380px;font:13px/1.35 Arial,sans-serif;box-shadow:0 8px 28px rgba(0,0,0,.45)}
      #${PANEL_ID}.hit{background:#3a0d0d;border-color:#ff5b5b;animation:ttSniperPulse 1.1s ease-in-out infinite}
      #${PANEL_ID} .tt-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:6px}
      #${PANEL_ID} .tt-title{font-weight:800;letter-spacing:.04em}
      #${PANEL_ID} .tt-state{font-size:11px;opacity:.8}
      #${PANEL_ID} .tt-price{font-size:22px;font-weight:900;margin:4px 0}
      #${PANEL_ID} .tt-edge{font-size:12px;opacity:.85;margin:3px 0 7px}
      #${PANEL_ID} button{cursor:pointer;border:1px solid #60738d;border-radius:6px;background:#1f2b3a;color:#fff;padding:6px 9px;margin-top:7px;font-weight:700}
      #${PANEL_ID} button:hover{background:#2a3a4d}
      #${PANEL_ID} #tt-buy-one{background:#7b1212;border-color:#ff6666;font-size:14px;padding:8px 12px}
      #${PANEL_ID} #tt-buy-one:hover{background:#a01818}
      .${HIGHLIGHT_CLASS}{outline:3px solid #ff4242 !important;outline-offset:-2px !important;animation:ttRowPulse 1.1s ease-in-out infinite !important}
    `;
    document.documentElement.appendChild(style);
  }

  function updateTitlePulse() {
    if (!bestHit) {
      if (titleTimer) clearInterval(titleTimer);
      titleTimer = null;
      titleFlip = false;
      if (baseTitle) document.title = baseTitle;
      return;
    }
    if (titleTimer) return;
    baseTitle = document.title.replace(/^🔥 BUY CANDIDATE · /, '') || document.title;
    titleTimer = setInterval(() => {
      if (!bestHit) { updateTitlePulse(); return; }
      titleFlip = !titleFlip;
      document.title = titleFlip ? `🔥 BUY CANDIDATE · ${baseTitle}` : baseTitle;
    }, 650);
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
      const baseline = Number(target.learned_baseline || 0);
      const edge = baseline ? ((baseline - Number(bestHit.price)) / baseline * 100) : null;
      const edgeText = edge == null ? '' : `<div class="tt-edge">Learned baseline ${money(baseline)} · live edge <strong>${edge.toFixed(1)}%</strong></div>`;
      panel.innerHTML = `<div class="tt-head"><span class="tt-title">🔥 BUY CANDIDATE · ${target.name}</span><span class="tt-state">LIVE PAGE</span></div><div class="tt-price">${money(bestHit.price)}</div>${edgeText}<div>Live-safe max: <strong>${money(target.max_price)}</strong>. BUY 1 invokes only this listing's native Buy control; Torn's normal confirmation remains manual.</div><button id="tt-buy-one">BUY 1 · ${money(bestHit.price)}</button> <button id="tt-jump">Show listing</button> <button id="tt-toggle">Pause</button>`;
    } else {
      const configured = Number(target.configured_max_price ?? target.max_price), effective = Number(target.max_price);
      const limited = configured > effective ? `<div style="opacity:.75;margin-top:4px">Configured ${money(configured)} · live edge gate currently limits alerts to ${money(effective)}.</div>` : '';
      panel.innerHTML = `<div class="tt-head"><span class="tt-title">${target.name}</span><span class="tt-state">${armed?'LIVE WATCH':'PAUSED'}</span></div><div>Sniper max: <strong>${money(effective)}</strong></div>${limited}<div style="opacity:.75;margin-top:4px">Watching the market page you already loaded. No automatic purchases.</div><button id="tt-toggle">${armed?'Pause':'Arm'} live watcher</button>`;
    }
    panel.querySelector('#tt-toggle')?.addEventListener('click', () => { armed = !armed; bestHit = null; clearHighlights(); updateTitlePulse(); renderPanel(); if (armed) scheduleScan(); });
    panel.querySelector('#tt-jump')?.addEventListener('click', () => bestHit?.row?.scrollIntoView({behavior:'smooth',block:'center'}));
    panel.querySelector('#tt-buy-one')?.addEventListener('click', manualBuyOne);
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

  function explicitListingQuantity(row) {
    if (!row) return null;
    const text = (row.innerText || '').replace(/\s+/g, ' ');
    const patterns = [
      /\b(?:qty|quantity|available)\s*[:x×]?\s*(\d+)\b/i,
      /\b(?:x|×)\s*(\d+)\b/i
    ];
    for (const re of patterns) {
      const m = text.match(re);
      if (m) {
        const n = Number(m[1]);
        if (Number.isFinite(n) && n > 0) return n;
      }
    }
    const dataQty = row.querySelector('[data-quantity]')?.getAttribute('data-quantity');
    if (dataQty && Number(dataQty) > 0) return Number(dataQty);
    return null;
  }

  function findQualifyingRows(target) {
    const controls = [...document.querySelectorAll('button,[role="button"],input[type="button"],input[type="submit"]')]
      .filter(isVisible)
      .filter(el => !el.closest(`#${PANEL_ID}`))
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
      if (price <= Number(target.max_price)) hits.push({row, control, price, explicit_qty: explicitListingQuantity(row)});
    }
    hits.sort((a,b) => a.price-b.price);
    return hits;
  }

  function liveCurrentHit(target) {
    const hits = findQualifyingRows(target);
    return hits[0] || null;
  }

  function manualBuyOne() {
    const target = currentItemId ? targets.get(currentItemId) : null;
    if (!target || !bestHit) return;
    const current = liveCurrentHit(target);
    if (!current || Number(current.price) !== Number(bestHit.price) || Number(current.price) > Number(target.max_price)) {
      bestHit = current;
      renderPanel();
      return;
    }
    if (current.explicit_qty != null && Number(current.explicit_qty) > 1) {
      current.row.scrollIntoView({behavior:'smooth',block:'center'});
      return;
    }
    const rowText = (current.row.innerText || '').replace(/\s+/g, ' ').slice(0, 300);
    const signature = `buy:${currentItemId}:${current.price}:${Date.now()}`;
    postTelemetry(target, current, 'buy_clicked', signature, {row_text: rowText, action: 'native_buy_control', quantity_intent: 1, origin: 'torn_page_button'});
    current.control.click();
  }

  function notifyRelayBlocked(text, hit) {
    try {
      GM_notification({
        title: 'TornTools BUY 1 blocked',
        text,
        timeout: 8000,
        onclick: () => { window.focus(); hit?.row?.scrollIntoView({behavior:'smooth',block:'center'}); }
      });
    } catch {}
  }

  function handleClaimedIntent(intent) {
    const target = targets.get(Number(intent.item_id));
    if (!target || Number(currentItemId) !== Number(intent.item_id)) return;
    const current = liveCurrentHit(target);
    const expectedPrice = Number(intent.expected_price || 0);
    const max = Math.min(Number(intent.effective_max || 0), Number(target.max_price || 0));
    if (!current || Number(current.price) !== expectedPrice || Number(current.price) > max) {
      postTelemetry(target, current, 'buy_intent_blocked', `blocked:${intent.intent_id}`, {reason:'listing_changed', expected_price:expectedPrice});
      notifyRelayBlocked('Listing changed or disappeared. Nothing was clicked; review the open market page.', current);
      return;
    }
    if (current.explicit_qty != null && Number(current.explicit_qty) > 1) {
      postTelemetry(target, current, 'buy_intent_blocked', `blocked:${intent.intent_id}`, {reason:'stack_detected', explicit_qty:Number(current.explicit_qty)});
      current.row.scrollIntoView({behavior:'smooth',block:'center'});
      notifyRelayBlocked(`A stack of ${current.explicit_qty} is visible. TornTools did not click Buy; the market page is open for you.`, current);
      return;
    }
    const rowText = (current.row.innerText || '').replace(/\s+/g, ' ').slice(0, 300);
    postTelemetry(target, current, 'buy_clicked', `relaybuy:${intent.intent_id}`, {
      row_text: rowText,
      action:'native_buy_control',
      quantity_intent:1,
      origin:'dashboard_manual_intent',
      intent_id:intent.intent_id,
      api_floor_qty:1,
      explicit_live_qty:current.explicit_qty
    });
    current.control.click();
  }

  function pollManualBuyIntent() {
    if (intentPollBusy) return;
    const itemId = parseCurrentItemId();
    if (!itemId || !targets.has(Number(itemId))) return;
    currentItemId = Number(itemId);
    intentPollBusy = true;
    GM_xmlhttpRequest({
      method: 'POST',
      url: BUY_INTENT_CLAIM_URL,
      headers: {'Content-Type':'application/json'},
      data: JSON.stringify({item_id:Number(itemId)}),
      timeout: 1500,
      onload: response => {
        intentPollBusy = false;
        try {
          const data = JSON.parse(response.responseText || '{}');
          if (data && data.claimed && data.intent) handleClaimedIntent(data.intent);
        } catch {}
      },
      onerror: () => { intentPollBusy = false; },
      ontimeout: () => { intentPollBusy = false; }
    });
  }

  function alertHit(target, hit) {
    const rowText = (hit.row.innerText || '').replace(/\s+/g, ' ').slice(0, 300);
    const signature = `${currentItemId}:${hit.price}:${rowText}`;
    if (signature === lastAlertSignature) return;
    lastAlertSignature = signature;
    postTelemetry(target, hit, 'alert_fired', `live:${signature}`, {row_text: rowText});
    try {
      GM_notification({
        title: `TornTools SNIPE · ${target.name}`,
        text: `${money(hit.price)} is at/below your ${money(target.max_price)} live-safe max.`,
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
    if (!armed || !currentItemId) { bestHit = null; updateTitlePulse(); renderPanel(); return; }
    const target = targets.get(currentItemId);
    if (!target) { clearHighlights(); bestHit = null; updateTitlePulse(); renderPanel(); return; }
    const hits = findQualifyingRows(target);
    clearHighlights();
    hits.forEach(h => h.row.classList.add(HIGHLIGHT_CLASS));
    bestHit = hits[0] || null;
    if (bestHit) alertHit(target, bestHit);
    updateTitlePulse();
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
      baseTitle = document.title;
      scheduleScan();
    }
  }, 300);

  fetchTargets();
  setInterval(fetchTargets, REFRESH_CONFIG_MS);
  setInterval(pollManualBuyIntent, BUY_INTENT_POLL_MS);
  scheduleScan();
})();