const money = new Intl.NumberFormat("en-US", {style:"currency", currency:"USD", maximumFractionDigits:0});
let timer = null;
let learnTimer = null;
let metadata = [];
let lastAlertKey = null;

async function call(path, opts={}) {
  const r = await fetch(path, {headers:{"Content-Type":"application/json"}, ...opts});
  let data = {}; try { data = await r.json(); } catch {}
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

function msg(text, bad=false) {
  const e = document.getElementById("msg"); e.textContent = text; e.className = bad ? "bad" : "good";
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
  localStorage.setItem("torntools.v03.settings", JSON.stringify(settings));
}

function loadSavedSettings() {
  try {
    const s = JSON.parse(localStorage.getItem("torntools.v03.settings") || localStorage.getItem("torntools.v02.settings") || "{}");
    if (s.interval) interval.value = s.interval;
    if (s.minProfit !== undefined) minProfit.value = s.minProfit;
    if (s.minRoi !== undefined) minRoi.value = s.minRoi;
    if (s.stockDiscount !== undefined) stockDiscount.value = s.stockDiscount;
    if (s.sound !== undefined) sound.checked = !!s.sound;
    return Array.isArray(s.watched) ? s.watched : null;
  } catch { return null; }
}

async function status() {
  try {
    const d = await call("/api/status"); metadata = d.items || [];
    const e = document.getElementById("status");
    e.textContent = d.key_loaded ? `● V${d.version} · key loaded` : `● V${d.version} · key needed`;
    e.className = d.key_loaded ? "pill good" : "pill warn";
    renderWatchlist();
    await loadLiquidity();
  } catch {
    status.textContent = "Backend unavailable"; status.className = "pill bad";
  }
}

function renderWatchlist() {
  const host = document.getElementById("watchlist"); const saved = loadSavedSettings(); host.innerHTML = "";
  metadata.forEach(item => {
    const checked = saved ? saved.includes(item.id) : item.enabled;
    host.insertAdjacentHTML("beforeend", `<label class="watch-chip ${item.mode}"><input class="watch-toggle" type="checkbox" value="${item.id}" ${checked ? "checked" : ""} onchange="watchChanged()"><span class="watch-name">${item.name}</span><span class="mode-tag">${item.mode === "stock" ? "PERSONAL USE" : "RESALE"}</span><small>${item.note}</small></label>`);
  });
  watchChanged(false);
}

function watchChanged(save=true) { watchingCount.textContent = document.querySelectorAll(".watch-toggle:checked").length; if (save) saveSettings(); }

async function loadKey() {
  const key = document.getElementById("key").value.trim(); if (!key) return msg("Paste a key first.", true);
  try {
    loadKeyBtn.disabled = true; msg("Loading key…");
    const result = await call("/api/key", {method:"POST", body:JSON.stringify({api_key:key})});
    document.getElementById("key").value = ""; msg(result.message || "Key loaded."); await status(); await scanNow();
  } catch(e) { msg(e.message, true); } finally { loadKeyBtn.disabled = false; }
}

async function forgetKey() { try { await call("/api/key", {method:"DELETE"}); msg("Key forgotten."); stopAuto(); stopLearnAuto(); await status(); } catch(e) { msg(e.message, true); } }
function selectedIds() { return [...document.querySelectorAll(".watch-toggle:checked")].map(x=>x.value); }

function isDeal(item) {
  if (item.error) return false;
  if (item.mode === "stock") return Number(item.discount_pct || 0) >= Number(stockDiscount.value || 0);
  return Number(item.floor_clear_profit_after_fee || 0) >= Number(minProfit.value || 0) && Number(item.net_roi_after_fee || 0) >= Number(minRoi.value || 0);
}
function score(item) { if (item.error) return -999; if (item.mode === "stock") return Number(item.discount_pct || 0) * 10; return Math.max(0, Number(item.net_roi_after_fee || 0) * 8 + Number(item.floor_clear_profit_after_fee || 0) / 50000); }

function card(item) {
  if (item.error) return `<article class="deal-card error-card"><div class="card-top"><div><h3>${item.name}</h3><span class="mode-tag">ERROR</span></div></div><p>${item.error}</p></article>`;
  const deal = isDeal(item), personal = item.mode === "stock", discount = Number(item.discount_pct || 0), profit = Number(item.floor_clear_profit_after_fee || 0), roi = Number(item.net_roi_after_fee || 0);
  return `<article class="deal-card ${deal ? "deal" : ""}"><div class="card-top"><div><h3>${item.name}</h3><span class="mode-tag">${personal ? "PERSONAL USE" : "RESALE"}</span></div><span class="deal-badge">${deal ? "GOOD BUY" : "WATCH"}</span></div><div class="hero-price">${money.format(item.lowest)}</div><div class="hero-sub">${item.qty_floor} available at this price</div><div class="metric-list">${personal ? `<div><span>Typical price</span><strong>${money.format(item.reference || 0)}</strong></div><div><span>Discount</span><strong>${discount.toFixed(2)}%</strong></div>` : `<div><span>Estimated profit</span><strong>${money.format(profit)}</strong></div><div><span>ROI after fee</span><strong>${roi.toFixed(2)}%</strong></div><div><span>Total buy cost</span><strong>${money.format(item.floor_clear_capital || 0)}</strong></div>`}<div><span>Next listing</span><strong>${item.next_higher ? money.format(item.next_higher) : "—"}</strong></div></div><button class="open-market" onclick="window.open('${item.market_url}','_blank','noopener')">Open Market</button></article>`;
}

function maybeSound(items) {
  if (!sound.checked) return; const deals = items.filter(isDeal).sort((a,b)=>score(b)-score(a)); if (!deals.length) return;
  const top = deals[0], key = `${top.id}:${top.lowest}:${Math.round(score(top))}`; if (key === lastAlertKey) return; lastAlertKey = key;
  try { const ctx = new (window.AudioContext || window.webkitAudioContext)(); const osc = ctx.createOscillator(); const gain = ctx.createGain(); osc.connect(gain); gain.connect(ctx.destination); osc.frequency.value = 880; gain.gain.value = 0.04; osc.start(); osc.stop(ctx.currentTime + 0.14); } catch {}
}

function renderScan(data) {
  const items = [...(data.items || [])].sort((a,b)=>score(b)-score(a)), deals = items.filter(isDeal);
  cards.innerHTML = items.length ? items.map(card).join("") : `<div class="empty">Nothing selected.</div>`; dealCount.textContent = deals.length;
  if (deals.length) { const best = deals[0]; bestDeal.textContent = best.name; bestDealSub.textContent = best.mode === "stock" ? `${best.discount_pct.toFixed(2)}% below typical price` : `${money.format(best.floor_clear_profit_after_fee)} estimated profit · ${best.net_roi_after_fee.toFixed(2)}% ROI`; }
  else { bestDeal.textContent = "None"; bestDealSub.textContent = "Nothing meets your buy settings right now"; }
  lastUpdated.textContent = `Updated ${new Date(data.scanned_at * 1000).toLocaleTimeString()}`; maybeSound(items);
}

async function scanNow() {
  const ids = selectedIds(); if (!ids.length) return msg("Select at least one item to watch.", true); saveSettings(); scanBtn.disabled = true; scanBtn.textContent = "Scanning…";
  try { const d = await call(`/api/scan?ids=${encodeURIComponent(ids.join(","))}`); renderScan(d); const errors = (d.items || []).filter(x=>x.error); msg(errors.length ? `Scan finished, but ${errors.length} item(s) had an error.` : `Scanned ${d.items.length} item(s).`, !!errors.length); }
  catch(e) { msg(e.message, true); } finally { scanBtn.disabled = false; scanBtn.textContent = "Scan Now"; }
}

function stopAuto() { if (timer) clearInterval(timer); timer = null; autoBtn.textContent = "Start Auto Scan"; autoBtn.classList.add("secondary"); }
function toggleAuto() { if (timer) return stopAuto(); saveSettings(); scanNow(); const seconds = Math.max(15, Number(interval.value || 30)); timer = setInterval(scanNow, seconds * 1000); autoBtn.textContent = `Stop Auto Scan (${seconds}s)`; autoBtn.classList.remove("secondary"); }

function renderLiquidity(items) {
  const host = document.getElementById("liquidityRows");
  if (!items.length) { host.innerHTML = `<tr><td colspan="7" class="muted">Run a few samples to start learning.</td></tr>`; return; }
  host.innerHTML = items.map(x => `<tr><td><strong>${x.name}</strong></td><td>${x.label || "Learning"}</td><td>${Number(x.score || 0).toFixed(0)}</td><td>${x.observations || 0}</td><td>${x.gap_events || 0}</td><td>${Number(x.largest_gap_pct || 0).toFixed(2)}%</td><td><button class="mini-btn" onclick="window.open('${x.market_url}','_blank','noopener')">Open</button></td></tr>`).join("");
}

async function loadLiquidity() { try { const d = await call("/api/liquidity"); renderLiquidity(d.items || []); } catch {} }

async function learnNow() {
  learnBtn.disabled = true; learnBtn.textContent = "Sampling…";
  try {
    const d = await call("/api/learn", {method:"POST"}); renderLiquidity(d.items || []); learnUpdated.textContent = `Sampled ${new Date(d.learned_at * 1000).toLocaleTimeString()}`;
    const errors = (d.items || []).filter(x=>x.error); if (errors.length) msg(`Research sample completed with ${errors.length} item error(s).`, true);
  } catch(e) { msg(e.message, true); } finally { learnBtn.disabled = false; learnBtn.textContent = "Sample Research Markets"; }
}

function stopLearnAuto() { if (learnTimer) clearInterval(learnTimer); learnTimer = null; learnAutoBtn.textContent = "Start Quiet Learning"; learnAutoBtn.classList.add("secondary"); }
function toggleLearnAuto() { if (learnTimer) return stopLearnAuto(); learnNow(); learnTimer = setInterval(learnNow, 120000); learnAutoBtn.textContent = "Stop Quiet Learning (2m)"; learnAutoBtn.classList.remove("secondary"); }

["interval","minProfit","minRoi","stockDiscount","sound"].forEach(id => document.addEventListener("change", e => { if (e.target.id === id) saveSettings(); }));
document.getElementById("key").addEventListener("keydown", e => { if (e.key === "Enter") loadKey(); });
loadSavedSettings(); status();
