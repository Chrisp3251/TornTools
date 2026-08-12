const money = new Intl.NumberFormat("en-US", {style:"currency", currency:"USD", maximumFractionDigits:0});
let timer = null;
let metadata = [];
let lastAlertKey = null;

async function call(path, opts={}) {
  const r = await fetch(path, {headers:{"Content-Type":"application/json"}, ...opts});
  let data = {};
  try { data = await r.json(); } catch {}
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

function msg(text, bad=false) {
  const e = document.getElementById("msg");
  e.textContent = text;
  e.className = bad ? "bad" : "good";
}

function saveSettings() {
  const settings = {
    interval: document.getElementById("interval").value,
    minProfit: document.getElementById("minProfit").value,
    minRoi: document.getElementById("minRoi").value,
    stockDiscount: document.getElementById("stockDiscount").value,
    sound: document.getElementById("sound").checked,
    watched: [...document.querySelectorAll(".watch-toggle")].filter(x=>x.checked).map(x=>Number(x.value))
  };
  localStorage.setItem("torntools.v02.settings", JSON.stringify(settings));
}

function loadSavedSettings() {
  try {
    const s = JSON.parse(localStorage.getItem("torntools.v02.settings") || "{}");
    if (s.interval) document.getElementById("interval").value = s.interval;
    if (s.minProfit !== undefined) document.getElementById("minProfit").value = s.minProfit;
    if (s.minRoi !== undefined) document.getElementById("minRoi").value = s.minRoi;
    if (s.stockDiscount !== undefined) document.getElementById("stockDiscount").value = s.stockDiscount;
    if (s.sound !== undefined) document.getElementById("sound").checked = !!s.sound;
    return Array.isArray(s.watched) ? s.watched : null;
  } catch { return null; }
}

async function status() {
  try {
    const d = await call("/api/status");
    metadata = d.items || [];
    const e = document.getElementById("status");
    e.textContent = d.key_loaded ? `● V${d.version} · key loaded` : `● V${d.version} · key needed`;
    e.className = d.key_loaded ? "pill good" : "pill warn";
    renderWatchlist();
  } catch {
    const e = document.getElementById("status");
    e.textContent = "Backend unavailable";
    e.className = "pill bad";
  }
}

function renderWatchlist() {
  const host = document.getElementById("watchlist");
  const saved = loadSavedSettings();
  host.innerHTML = "";
  metadata.forEach(item => {
    const checked = saved ? saved.includes(item.id) : item.enabled;
    host.insertAdjacentHTML("beforeend", `
      <label class="watch-chip ${item.mode}">
        <input class="watch-toggle" type="checkbox" value="${item.id}" ${checked ? "checked" : ""} onchange="watchChanged()">
        <span class="watch-name">${item.name}</span>
        <span class="mode-tag">${item.mode === "stock" ? "KEEP / STOCK" : "FLIP"}</span>
        <small>${item.note}</small>
      </label>
    `);
  });
  watchChanged(false);
}

function watchChanged(save=true) {
  const count = document.querySelectorAll(".watch-toggle:checked").length;
  document.getElementById("watchingCount").textContent = count;
  if (save) saveSettings();
}

async function loadKey() {
  const key = document.getElementById("key").value.trim();
  if (!key) return msg("Paste a key first.", true);
  try {
    document.getElementById("loadKeyBtn").disabled = true;
    msg("Validating key…");
    await call("/api/key", {method:"POST", body:JSON.stringify({api_key:key})});
    document.getElementById("key").value = "";
    msg("Key validated and loaded into backend memory.");
    await status();
    await scanNow();
  } catch(e) { msg(e.message, true); }
  finally { document.getElementById("loadKeyBtn").disabled = false; }
}

async function forgetKey() {
  try {
    await call("/api/key", {method:"DELETE"});
    msg("Key forgotten.");
    stopAuto();
    await status();
  } catch(e) { msg(e.message, true); }
}

function selectedIds() {
  return [...document.querySelectorAll(".watch-toggle:checked")].map(x=>x.value);
}

function isDeal(item) {
  if (item.error) return false;
  const minProfit = Number(document.getElementById("minProfit").value || 0);
  const minRoi = Number(document.getElementById("minRoi").value || 0);
  const stockDiscount = Number(document.getElementById("stockDiscount").value || 0);
  if (item.mode === "stock") return Number(item.discount_pct || 0) >= stockDiscount;
  return Number(item.net_profit_after_fee || 0) >= minProfit && Number(item.net_roi_after_fee || 0) >= minRoi;
}

function score(item) {
  if (item.error) return -999;
  if (item.mode === "stock") return Number(item.discount_pct || 0) * 10;
  return Math.max(0, Number(item.net_roi_after_fee || 0) * 8 + Number(item.net_profit_after_fee || 0) / 50000);
}

function card(item) {
  if (item.error) {
    return `<article class="deal-card error-card"><div class="card-top"><div><h3>${item.name}</h3><span class="mode-tag">ERROR</span></div></div><p>${item.error}</p></article>`;
  }
  const deal = isDeal(item);
  const stock = item.mode === "stock";
  const headline = stock
    ? `${Number(item.discount_pct || 0).toFixed(2)}% below local reference`
    : `${money.format(item.net_profit_after_fee || 0)} theoretical net`;
  const sub = stock
    ? `Reference ${money.format(item.reference)} · useful to keep rather than resell`
    : `${Number(item.net_roi_after_fee || 0).toFixed(2)}% after 5% market fee`;
  return `
    <article class="deal-card ${deal ? "deal" : ""}">
      <div class="card-top">
        <div>
          <h3>${item.name}</h3>
          <span class="mode-tag">${stock ? "KEEP / STOCK" : "FLIP"}</span>
        </div>
        <span class="deal-badge">${deal ? "DEAL" : "WATCH"}</span>
      </div>
      <div class="hero-price">${money.format(item.lowest)}</div>
      <div class="hero-sub">${item.qty_floor} item(s) at floor</div>
      <div class="metric-list">
        <div><span>${stock ? "Discount" : "Net profit"}</span><strong>${headline}</strong></div>
        <div><span>${stock ? "Context" : "Net ROI"}</span><strong>${sub}</strong></div>
        <div><span>Next higher</span><strong>${item.next_higher ? money.format(item.next_higher) : "—"}</strong></div>
      </div>
      <button class="open-market" onclick="window.open('${item.market_url}','_blank','noopener')">Open ${item.name} Market</button>
    </article>
  `;
}

function maybeSound(items) {
  if (!document.getElementById("sound").checked) return;
  const deals = items.filter(isDeal).sort((a,b)=>score(b)-score(a));
  if (!deals.length) return;
  const top = deals[0];
  const key = `${top.id}:${top.lowest}:${Math.round(score(top))}`;
  if (key === lastAlertKey) return;
  lastAlertKey = key;
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain); gain.connect(ctx.destination);
    osc.frequency.value = 880; gain.gain.value = 0.04;
    osc.start(); osc.stop(ctx.currentTime + 0.14);
  } catch {}
}

function renderScan(data) {
  const items = [...(data.items || [])].sort((a,b)=>score(b)-score(a));
  const deals = items.filter(isDeal);
  const host = document.getElementById("cards");
  host.innerHTML = items.length ? items.map(card).join("") : `<div class="empty">Nothing selected.</div>`;
  document.getElementById("dealCount").textContent = deals.length;
  if (deals.length) {
    const best = deals[0];
    document.getElementById("bestDeal").textContent = best.name;
    document.getElementById("bestDealSub").textContent = best.mode === "stock"
      ? `${best.discount_pct.toFixed(2)}% below local reference`
      : `${money.format(best.net_profit_after_fee)} · ${best.net_roi_after_fee.toFixed(2)}% ROI`;
  } else {
    document.getElementById("bestDeal").textContent = "None";
    document.getElementById("bestDealSub").textContent = "No item meets your thresholds right now";
  }
  document.getElementById("lastUpdated").textContent = `Updated ${new Date(data.scanned_at * 1000).toLocaleTimeString()}`;
  maybeSound(items);
}

async function scanNow() {
  const ids = selectedIds();
  if (!ids.length) return msg("Select at least one item to watch.", true);
  saveSettings();
  const btn = document.getElementById("scanBtn");
  btn.disabled = true; btn.textContent = "Scanning…";
  try {
    const d = await call(`/api/scan?ids=${encodeURIComponent(ids.join(","))}`);
    renderScan(d);
    msg(`Scanned ${d.items.length} item(s).`);
  } catch(e) { msg(e.message, true); }
  finally { btn.disabled = false; btn.textContent = "Scan Now"; }
}

function stopAuto() {
  if (timer) clearInterval(timer);
  timer = null;
  const btn = document.getElementById("autoBtn");
  btn.textContent = "Start Auto Scan";
  btn.classList.add("secondary");
}

function toggleAuto() {
  if (timer) return stopAuto();
  saveSettings();
  scanNow();
  const seconds = Math.max(15, Number(document.getElementById("interval").value || 30));
  timer = setInterval(scanNow, seconds * 1000);
  const btn = document.getElementById("autoBtn");
  btn.textContent = `Stop Auto Scan (${seconds}s)`;
  btn.classList.remove("secondary");
}

["interval","minProfit","minRoi","stockDiscount","sound"].forEach(id => {
  document.addEventListener("change", e => { if (e.target.id === id) saveSettings(); });
});

document.getElementById("key").addEventListener("keydown", e => { if (e.key === "Enter") loadKey(); });
loadSavedSettings();
status();
