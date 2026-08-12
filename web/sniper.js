let sniperTargets = new Map();
let sniperTimer = null;
let sniperRunning = false;
let sniperRequestRunning = false;
let sniperLastRequest = new Map();
let sniperAlertSignatures = new Map();

const SNIPER_TICK_MS = 250;
const SNIPER_AUTOSTART_KEY = "torntools.sniper.autostart";

function sniperTarget(id) {
  return sniperTargets.get(Number(id)) || null;
}

function sniperDueAt(target) {
  const id = Number(target.item_id);
  const x = hiddenResults.get(id);
  const last = Number(sniperLastRequest.get(id) || 0);
  if (!x) return last ? last + 5000 : 0;
  const cacheTs = Number(x.cache_timestamp || 0) * 1000;
  const delay = Math.max(1, Number(x.cache_delay || 30)) * 1000;
  if (cacheTs) return Math.max(last + 1000, cacheTs + delay + 100);
  return last + 10000;
}

function nextSniperTarget() {
  const now = Date.now();
  const enabled = [...sniperTargets.values()].filter(x => x.enabled);
  let best = null;
  let bestDue = Infinity;
  for (const target of enabled) {
    const due = sniperDueAt(target);
    if (due <= now && due < bestDue) {
      best = target;
      bestDue = due;
    }
  }
  return best;
}

function sniperHit(target, result) {
  return !!result && !result.error && Number(result.lowest || 0) > 0 && Number(result.lowest) <= Number(target.max_price);
}

function processSniperResult(target) {
  const x = hiddenResults.get(Number(target.item_id));
  if (!x || x.error) return;
  const hit = sniperHit(target, x);
  const signature = `${x.cache_timestamp || 0}:${x.lowest || 0}:${x.qty_floor || 0}`;
  const prior = sniperAlertSignatures.get(Number(target.item_id));
  if (!hit) {
    sniperAlertSignatures.set(Number(target.item_id), null);
    return;
  }
  sniperAlertSignatures.set(Number(target.item_id), signature);
  if (signature === prior) return;
  const spread = Number(target.max_price) - Number(x.lowest || 0);
  notify(
    `SNIPER · ${target.name}`,
    `${money.format(x.lowest)} is at/below your ${money.format(target.max_price)} max${spread > 0 ? ` · ${money.format(spread)} under max` : ""}. Click to open the market.`,
    x.market_url || target.market_url,
    "high"
  );
}

function renderSniperTargets() {
  const rows = $("sniperRows");
  const count = $("sniperCount");
  if (!rows) return;
  const targets = [...sniperTargets.values()].sort((a,b) => Number(b.enabled)-Number(a.enabled) || String(a.name).localeCompare(String(b.name)));
  const enabledCount = targets.filter(x => x.enabled).length;
  if (count) count.textContent = `${enabledCount} armed / ${targets.length} target${targets.length===1?"":"s"}`;
  if (!targets.length) {
    rows.innerHTML = '<tr><td colspan="6" class="muted">No sniper targets configured.</td></tr>';
    return;
  }
  rows.innerHTML = targets.map(t => {
    const x = hiddenResults.get(Number(t.item_id));
    const hit = sniperHit(t, x);
    const due = sniperDueAt(t);
    const wait = Math.max(0, Math.ceil((due-Date.now())/1000));
    const state = !t.enabled ? "DISABLED" : hit ? "SNIPE NOW" : x ? (wait ? `next useful check ~${wait}s` : "due now") : "waiting for first check";
    const rowClass = hit ? "priority-hit" : "";
    const current = x && !x.error && x.lowest ? money.format(x.lowest) : "—";
    return `<tr class="${rowClass}">
      <td><strong>${t.name}</strong><br><small class="muted">Item #${t.item_id}</small></td>
      <td><strong>${money.format(t.max_price)}</strong></td>
      <td>${current}</td>
      <td><strong>${state}</strong></td>
      <td><button class="mini-btn" onclick="window.open('${t.market_url}','_blank','noopener')">Open Live Market</button></td>
      <td><button class="mini-btn secondary" onclick="editSniperTarget(${t.item_id})">Edit</button> <button class="mini-btn secondary" onclick="toggleSniperTarget(${t.item_id})">${t.enabled?"Disable":"Enable"}</button> <button class="mini-btn secondary" onclick="removeSniperTarget(${t.item_id})">Remove</button></td>
    </tr>`;
  }).join("");
}

async function loadSniperTargets() {
  const status = $("sniperStatus");
  try {
    const d = await call("/api/sniper/watchlist");
    sniperTargets = new Map((d.items || []).map(x => [Number(x.item_id), x]));
    if (Array.isArray(d.discovery_ids)) discoveryIds = d.discovery_ids.map(Number);
    renderSniperTargets();
    if (status) status.textContent = "Sniper watchlist loaded. API checks are cache-aware; the userscript companion handles live-page detection.";
    return true;
  } catch (e) {
    if (status) status.innerHTML = `<span class="bad">Sniper backend unavailable: ${e.message}</span>`;
    return false;
  }
}

async function saveSniperTarget() {
  const id = Number($("sniperItemId")?.value || 0);
  const name = ($("sniperName")?.value || "").trim();
  const maxPrice = Math.trunc(Number($("sniperMaxPrice")?.value || 0));
  if (!id || maxPrice <= 0) return msg("Sniper needs a valid item ID and max buy price.", true);
  const existing = sniperTargets.get(id);
  try {
    const d = await call("/api/sniper/watchlist", {
      method: "POST",
      body: JSON.stringify({item_id:id,name:name || existing?.name || `Item ${id}`,max_price:maxPrice,enabled:existing?.enabled ?? true})
    });
    if (Array.isArray(d.discovery_ids)) discoveryIds = d.discovery_ids.map(Number);
    if ($("sniperItemId")) $("sniperItemId").value = "";
    if ($("sniperName")) $("sniperName").value = "";
    if ($("sniperMaxPrice")) $("sniperMaxPrice").value = "";
    await loadSniperTargets();
    if (!sniperRunning) startSniperWatch();
  } catch(e) { msg(e.message, true); }
}

function editSniperTarget(id) {
  const t = sniperTargets.get(Number(id));
  if (!t) return;
  $("sniperItemId").value = t.item_id;
  $("sniperName").value = t.name;
  $("sniperMaxPrice").value = t.max_price;
  $("sniperItemId").scrollIntoView({behavior:"smooth", block:"center"});
}

async function toggleSniperTarget(id) {
  const t = sniperTargets.get(Number(id));
  if (!t) return;
  try {
    const d = await call("/api/sniper/watchlist", {
      method:"POST",
      body:JSON.stringify({item_id:t.item_id,name:t.name,max_price:t.max_price,enabled:!t.enabled})
    });
    if (Array.isArray(d.discovery_ids)) discoveryIds = d.discovery_ids.map(Number);
    await loadSniperTargets();
  } catch(e) { msg(e.message,true); }
}

async function removeSniperTarget(id) {
  try {
    const d = await call(`/api/sniper/watchlist/${Number(id)}`, {method:"DELETE"});
    if (Array.isArray(d.discovery_ids)) discoveryIds = d.discovery_ids.map(Number);
    sniperLastRequest.delete(Number(id));
    sniperAlertSignatures.delete(Number(id));
    await loadSniperTargets();
  } catch(e) { msg(e.message,true); }
}

function updateSniperStatus(next=null) {
  const state = $("sniperState");
  const nextEl = $("sniperNext");
  if (!state || !nextEl) return;
  if (!sniperRunning) {
    state.textContent = "● Sniper idle";
    state.className = "status-dot idle";
    nextEl.textContent = "Next sniper check: —";
    return;
  }
  state.textContent = `● Sniper armed · ${[...sniperTargets.values()].filter(x=>x.enabled).length} target(s)`;
  state.className = "status-dot live";
  if (next) nextEl.textContent = `Checking ${next.name} now…`;
  else {
    const enabled = [...sniperTargets.values()].filter(x=>x.enabled);
    if (!enabled.length) nextEl.textContent = "No enabled sniper targets.";
    else {
      const upcoming = enabled.map(t=>({t,due:sniperDueAt(t)})).sort((a,b)=>a.due-b.due)[0];
      const wait = Math.max(0, Math.ceil((upcoming.due-Date.now())/1000));
      nextEl.textContent = `${upcoming.t.name}: next useful API check in ~${wait}s · live userscript can react sooner while its market page is open`;
    }
  }
}

async function runSniperScheduler() {
  if (!sniperRunning) return;
  if (!sniperRequestRunning) {
    const target = nextSniperTarget();
    updateSniperStatus(target);
    if (target) {
      sniperRequestRunning = true;
      sniperLastRequest.set(Number(target.item_id), Date.now());
      try {
        await discoverNow([Number(target.item_id)]);
        processSniperResult(target);
      } catch(e) {
        const status = $("sniperStatus");
        if (status) status.innerHTML = `<span class="bad">Sniper check error: ${e.message}</span>`;
      } finally {
        sniperRequestRunning = false;
        renderSniperTargets();
      }
    }
  }
  updateSniperStatus();
  sniperTimer = setTimeout(runSniperScheduler, SNIPER_TICK_MS);
}

function startSniperWatch() {
  if (sniperRunning) return;
  sniperRunning = true;
  localStorage.setItem(SNIPER_AUTOSTART_KEY, "1");
  const btn = $("sniperToggleBtn");
  if (btn) { btn.textContent = "Stop Sniper Watch"; btn.classList.remove("secondary"); }
  runSniperScheduler();
}

function stopSniperWatch() {
  sniperRunning = false;
  sniperRequestRunning = false;
  localStorage.setItem(SNIPER_AUTOSTART_KEY, "0");
  if (sniperTimer) clearTimeout(sniperTimer);
  sniperTimer = null;
  const btn = $("sniperToggleBtn");
  if (btn) { btn.textContent = "Start Sniper Watch"; btn.classList.add("secondary"); }
  updateSniperStatus();
}

function toggleSniperWatch() {
  if (sniperRunning) stopSniperWatch(); else startSniperWatch();
}

window.addEventListener("DOMContentLoaded", async () => {
  const ok = await loadSniperTargets();
  if (ok && localStorage.getItem(SNIPER_AUTOSTART_KEY) !== "0") startSniperWatch();
  setInterval(() => { if (sniperTargets.size) renderSniperTargets(); }, 1000);
});
