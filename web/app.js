const money = new Intl.NumberFormat("en-US", {style:"currency", currency:"USD", maximumFractionDigits:0});

let timer = null;
let learnTimer = null;
let discoverTimer = null;
let learnCountdownTimer = null;
let discoverCountdownTimer = null;
let metadata = [];
let lastAlertKey = null;
let lastHiddenAlertKeys = new Set();
let lastResearchCandidates = new Set();

const $ = id => document.getElementById(id);

async function call(path, opts={}) {
  const r = await fetch(path, {headers:{"Content-Type":"application/json"}, ...opts});
  let d = {};
  try { d = await r.json(); } catch {}
  if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
  return d;
}

function msg(text, bad=false) {
  const e = $("msg");
  e.textContent = text;
  e.className = bad ? "bad" : "good";
}

function saveSettings() {
  localStorage.setItem("torntools.v042.settings", JSON.stringify({
    interval: $("interval").value,
    minProfit: $("minProfit").value,
    minRoi: $("minRoi").value,
    stockDiscount: $("stockDiscount").value,
    sound: $("sound").checked,
    hiddenSound: $("hiddenSound").checked,
    watched: [...document.querySelectorAll(".watch-toggle")].filter(x=>x.checked).map(x=>Number(x.value))
  }));
}

function loadSavedSettings() {
  try {
    const raw = localStorage.getItem("torntools.v042.settings") || localStorage.getItem("torntools.v04.settings") || localStorage.getItem("torntools.v03.settings") || localStorage.getItem("torntools.v02.settings") || "{}";
    const s = JSON.parse(raw);
    if (s.interval) $("interval").value = s.interval;
    if (s.minProfit !== undefined) $("minProfit").value = s.minProfit;
    if (s.minRoi !== undefined) $("minRoi").value = s.minRoi;
    if (s.stockDiscount !== undefined) $("stockDiscount").value = s.stockDiscount;
    if (s.sound !== undefined) $("sound").checked = !!s.sound;
    if (s.hiddenSound !== undefined) $("hiddenSound").checked = !!s.hiddenSound;
    return Array.isArray(s.watched) ? s.watched : null;
  } catch { return null; }
}

async function appStatus() {
  try {
    const d = await call("/api/status");
    metadata = d.items || [];
    const e = $("status");
    e.textContent = d.key_loaded ? `● V${d.version} · key loaded` : `● V${d.version} · key needed`;
    e.className = d.key_loaded ? "pill good" : "pill warn";
    renderWatchlist();
    await loadLiquidity();
    updateNotificationButton();
  } catch {
    const e = $("status");
    e.textContent = "Backend unavailable";
    e.className = "pill bad";
  }
}

function renderWatchlist() {
  const h = $("watchlist");
  const saved = loadSavedSettings();
  h.innerHTML = "";
  metadata.forEach(i => {
    const checked = saved ? saved.includes(i.id) : i.enabled;
    h.insertAdjacentHTML("beforeend", `<label class="watch-chip ${i.mode}"><input class="watch-toggle" type="checkbox" value="${i.id}" ${checked?"checked":""} onchange="watchChanged()"><span class="watch-name">${i.name}</span><span class="mode-tag">${i.mode==="stock"?"PERSONAL USE":"RESALE"}</span><small>${i.note}</small></label>`);
  });
  watchChanged(false);
}

function watchChanged(save=true) {
  $("watchingCount").textContent = document.querySelectorAll(".watch-toggle:checked").length;
  if (save) saveSettings();
}

async function loadKey() {
  const k = $("key").value.trim();
  if (!k) return msg("Paste a key first.", true);
  try {
    $("loadKeyBtn").disabled = true;
    msg("Loading key…");
    const r = await call("/api/key", {method:"POST", body:JSON.stringify({api_key:k})});
    $("key").value = "";
    msg(r.message || "Key loaded.");
    await appStatus();
    await scanNow();
  } catch(e) { msg(e.message, true); }
  finally { $("loadKeyBtn").disabled = false; }
}

async function forgetKey() {
  try {
    await call("/api/key", {method:"DELETE"});
    msg("Key forgotten for this session. Your local .env will load again after restart if it still contains a key.");
    stopAuto();
    stopLearnAuto();
    stopDiscoverAuto();
    await appStatus();
  } catch(e) { msg(e.message, true); }
}

function selectedIds() {
  return [...document.querySelectorAll(".watch-toggle:checked")].map(x=>x.value);
}

function isDeal(i) {
  if (i.error) return false;
  if (i.mode === "stock") return Number(i.discount_pct || 0) >= Number($("stockDiscount").value || 0);
  return Number(i.floor_clear_profit_after_fee || 0) >= Number($("minProfit").value || 0) && Number(i.net_roi_after_fee || 0) >= Number($("minRoi").value || 0);
}

function score(i) {
  if (i.error) return -999;
  if (i.mode === "stock") return Number(i.discount_pct || 0) * 10;
  return Math.max(0, Number(i.net_roi_after_fee || 0) * 8 + Number(i.floor_clear_profit_after_fee || 0) / 50000);
}

function card(i) {
  if (i.error) return `<article class="deal-card error-card"><div class="card-top"><div><h3>${i.name}</h3><span class="mode-tag">ERROR</span></div></div><p>${i.error}</p></article>`;
  const d=isDeal(i), personal=i.mode==="stock", disc=Number(i.discount_pct||0), profit=Number(i.floor_clear_profit_after_fee||0), roi=Number(i.net_roi_after_fee||0);
  return `<article class="deal-card ${d?"deal":""}"><div class="card-top"><div><h3>${i.name}</h3><span class="mode-tag">${personal?"PERSONAL USE":"RESALE"}</span></div><span class="deal-badge">${d?"GOOD BUY":"WATCH"}</span></div><div class="hero-price">${money.format(i.lowest)}</div><div class="hero-sub">${i.qty_floor} available at this price</div><div class="metric-list">${personal?`<div><span>Typical price</span><strong>${money.format(i.reference||0)}</strong></div><div><span>Discount</span><strong>${disc.toFixed(2)}%</strong></div>`:`<div><span>Estimated profit</span><strong>${money.format(profit)}</strong></div><div><span>ROI after fee</span><strong>${roi.toFixed(2)}%</strong></div><div><span>Total buy cost</span><strong>${money.format(i.floor_clear_capital||0)}</strong></div>`}<div><span>Next listing</span><strong>${i.next_higher?money.format(i.next_higher):"—"}</strong></div></div><button class="open-market" onclick="window.open('${i.market_url}','_blank','noopener')">Open Market</button></article>`;
}

function beep(priority="normal") {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain); gain.connect(ctx.destination);
    osc.frequency.value = priority === "high" ? 1150 : 880;
    gain.gain.value = priority === "high" ? 0.09 : 0.05;
    osc.start();
    osc.stop(ctx.currentTime + (priority === "high" ? 0.28 : 0.16));
  } catch {}
}

function maybeSound(items) {
  if (!$("sound").checked) return;
  const deals=items.filter(isDeal).sort((a,b)=>score(b)-score(a));
  if (!deals.length) return;
  const t=deals[0], k=`${t.id}:${t.lowest}:${Math.round(score(t))}`;
  if (k===lastAlertKey) return;
  lastAlertKey=k;
  beep("normal");
}

function renderScan(d) {
  const items=[...(d.items||[])].sort((a,b)=>score(b)-score(a)), deals=items.filter(isDeal);
  $("cards").innerHTML=items.length?items.map(card).join(""):`<div class="empty">Nothing selected.</div>`;
  $("dealCount").textContent=deals.length;
  if (deals.length) {
    const b=deals[0];
    $("bestDeal").textContent=b.name;
    $("bestDealSub").textContent=b.mode==="stock"?`${b.discount_pct.toFixed(2)}% below typical price`:`${money.format(b.floor_clear_profit_after_fee)} estimated profit · ${b.net_roi_after_fee.toFixed(2)}% ROI`;
  } else {
    $("bestDeal").textContent="None";
    $("bestDealSub").textContent="Nothing meets your buy settings right now";
  }
  $("lastUpdated").textContent=`Updated ${new Date(d.scanned_at*1000).toLocaleTimeString()}`;
  maybeSound(items);
}

async function scanNow() {
  const ids=selectedIds();
  if (!ids.length) return msg("Select at least one item to watch.",true);
  saveSettings();
  $("scanBtn").disabled=true; $("scanBtn").textContent="Scanning…";
  try {
    const d=await call(`/api/scan?ids=${encodeURIComponent(ids.join(","))}`);
    renderScan(d);
    const errors=(d.items||[]).filter(x=>x.error);
    msg(errors.length?`Scan finished, but ${errors.length} item(s) had an error.`:`Scanned ${d.items.length} item(s).`,!!errors.length);
  } catch(e) { msg(e.message,true); }
  finally { $("scanBtn").disabled=false; $("scanBtn").textContent="Scan Now"; }
}

function stopAuto(){ if(timer)clearInterval(timer);timer=null;$("autoBtn").textContent="Start Auto Scan";$("autoBtn").classList.add("secondary"); }
function toggleAuto(){ if(timer)return stopAuto();saveSettings();scanNow();const s=Math.max(15,Number($("interval").value||30));timer=setInterval(scanNow,s*1000);$("autoBtn").textContent=`Stop Auto Scan (${s}s)`;$("autoBtn").classList.remove("secondary"); }

function renderLiquidity(items) {
  if (!items.length) { $("liquidityRows").innerHTML=`<tr><td colspan="7" class="muted">Run a few samples to start learning.</td></tr>`; return; }
  $("liquidityRows").innerHTML=items.map(x=>{
    const candidate=isResearchCandidate(x);
    return `<tr class="${candidate?"research-candidate":""}"><td><strong>${x.name}</strong>${candidate?`<br><small class="candidate-label">PROMOTION CANDIDATE</small>`:""}</td><td>${x.label||"Learning"}</td><td>${Number(x.score||0).toFixed(0)}</td><td>${x.observations||0}</td><td>${x.gap_events||0}</td><td>${Number(x.largest_gap_pct||0).toFixed(2)}%</td><td><button class="mini-btn" onclick="window.open('${x.market_url}','_blank','noopener')">Open</button></td></tr>`;
  }).join("");
}

function isResearchCandidate(x) {
  return Number(x.observations||0) >= 8 && ["Active","Very active"].includes(x.label) && Number(x.gap_events||0) >= 3 && Number(x.largest_gap_pct||0) >= 3;
}

async function loadLiquidity(){ try{const d=await call("/api/liquidity");renderLiquidity(d.items||[])}catch{} }

function notify(title, body, url=null, priority="normal") {
  if (priority && $("hiddenSound")?.checked) beep(priority);
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  try {
    const n = new Notification(title, {body, tag:`torntools-${title}`, renotify:true});
    if (url) n.onclick = () => { window.focus(); window.open(url,"_blank","noopener"); n.close(); };
    setTimeout(()=>n.close(), 15000);
  } catch {}
}

function updateNotificationButton() {
  const btn=$("notifyBtn");
  if (!btn) return;
  if (!("Notification" in window)) { btn.textContent="Desktop Alerts Unsupported"; btn.disabled=true; return; }
  if (Notification.permission === "granted") { btn.textContent="Desktop Alerts Enabled"; btn.classList.add("enabled-alerts"); }
  else if (Notification.permission === "denied") { btn.textContent="Desktop Alerts Blocked"; btn.disabled=true; }
  else { btn.textContent="Enable Desktop Alerts"; }
}

async function enableNotifications() {
  if (!("Notification" in window)) return msg("This browser does not support desktop notifications.", true);
  try {
    const result=await Notification.requestPermission();
    updateNotificationButton();
    if (result==="granted") msg("Desktop alerts enabled. Hidden Deals can now notify you even when this tab is in the background.");
    else msg("Desktop alerts were not enabled. You can still use the sound and highlighted rows.", true);
  } catch(e) { msg(`Could not enable notifications: ${e.message}`, true); }
}

function renderDiscovery(items) {
  if(!items.length){$("discoverRows").innerHTML=`<tr><td colspan="8" class="muted">No discovery results.</td></tr>`;return;}
  $("discoverRows").innerHTML=items.map(x=>{
    if(x.error)return`<tr><td><strong>${x.name}</strong></td><td colspan="6" class="bad">${x.error}</td><td></td></tr>`;
    const high=x.kind==="NPC FLOOR";
    const normal=x.kind==="UNDER MARKET" && Number(x.discount_pct||0)>=15;
    const mild=x.kind==="UNDER MARKET" && !normal;
    const rowClass=high?"priority-hit":normal?"research-hit":mild?"mild-hit":"";
    const potential=x.floor_profit>0?`${money.format(x.floor_profit)} guaranteed spread`:(x.discount_pct>=8?`${x.discount_pct.toFixed(1)}% under market`:"—");
    return`<tr class="${rowClass}"><td><strong>${x.name}</strong><br><small class="muted">${x.activity||"Learning"} · ${x.samples||0} samples</small></td><td><strong>${x.kind}</strong></td><td>${money.format(x.lowest||0)}${x.qty_floor>1?` × ${x.qty_floor}`:""}</td><td>${x.market_value?money.format(x.market_value):"—"}</td><td>${Number(x.discount_pct||0).toFixed(1)}%</td><td>${x.hard_floor?money.format(x.hard_floor):"—"}</td><td>${potential}</td><td><button class="mini-btn" onclick="window.open('${x.market_url}','_blank','noopener')">Open</button></td></tr>`;
  }).join("");
}

function processHiddenAlerts(items) {
  const currentKeys=new Set();
  for (const x of items) {
    if (x.error) continue;
    let priority=null;
    if (x.kind==="NPC FLOOR") priority="high";
    else if (x.kind==="UNDER MARKET" && Number(x.discount_pct||0)>=15) priority="normal";
    if (!priority) continue;
    const key=`${x.id}:${x.lowest}:${x.qty_floor}:${x.kind}`;
    currentKeys.add(key);
    if (lastHiddenAlertKeys.has(key)) continue;
    const body=x.kind==="NPC FLOOR"
      ? `${money.format(x.lowest)} buy · ${money.format(x.hard_floor)} NPC floor · ${money.format(x.floor_profit||0)} potential spread`
      : `${money.format(x.lowest)} buy · ${Number(x.discount_pct||0).toFixed(1)}% below Torn market value`;
    notify(`${priority==="high"?"HIGH PRIORITY · ":""}${x.name}`, body, x.market_url, priority);
  }
  lastHiddenAlertKeys=currentKeys;
}

function processResearchCandidates(items) {
  const current=new Set();
  for (const x of items) {
    if (!isResearchCandidate(x)) continue;
    current.add(String(x.id));
    if (lastResearchCandidates.has(String(x.id))) continue;
    if ("Notification" in window && Notification.permission==="granted") {
      try {
        const n=new Notification(`Research candidate · ${x.name}`, {body:`${x.label} · ${x.observations} samples · ${x.gap_events} gap events · largest gap ${Number(x.largest_gap_pct||0).toFixed(1)}%`, tag:`research-${x.id}`});
        n.onclick=()=>{window.focus();window.open(x.market_url,"_blank","noopener");n.close();};
      } catch {}
    }
  }
  lastResearchCandidates=current;
}

function startCountdown(kind, seconds) {
  const isLearn=kind==="learn";
  const state=$(isLearn?"learnState":"discoverState");
  const next=$(isLearn?"learnNext":"discoverNext");
  const progress=$(isLearn?"learnProgress":"discoverProgress");
  if (isLearn && learnCountdownTimer) clearInterval(learnCountdownTimer);
  if (!isLearn && discoverCountdownTimer) clearInterval(discoverCountdownTimer);
  let remaining=seconds;
  state.textContent=isLearn?"● Quiet Learning active":"● Hidden auto scan active";
  state.className="status-dot live";
  const tick=()=>{
    next.textContent=`Next ${isLearn?"sample":"scan"}: ${Math.floor(remaining/60)}m ${String(remaining%60).padStart(2,"0")}s`;
    progress.style.width=`${Math.max(0,Math.min(100,(1-remaining/seconds)*100))}%`;
    if (remaining<=0) remaining=seconds; else remaining--;
  };
  tick();
  const handle=setInterval(tick,1000);
  if (isLearn) learnCountdownTimer=handle; else discoverCountdownTimer=handle;
}

function stopCountdown(kind) {
  const isLearn=kind==="learn";
  const timerHandle=isLearn?learnCountdownTimer:discoverCountdownTimer;
  if (timerHandle) clearInterval(timerHandle);
  if (isLearn) learnCountdownTimer=null; else discoverCountdownTimer=null;
  const state=$(isLearn?"learnState":"discoverState"), next=$(isLearn?"learnNext":"discoverNext"), progress=$(isLearn?"learnProgress":"discoverProgress");
  state.textContent=isLearn?"● Learning idle":"● Hidden scan idle";
  state.className="status-dot idle";
  next.textContent=`Next ${isLearn?"sample":"scan"}: —`;
  progress.style.width="0%";
}

async function learnNow() {
  $("learnBtn").disabled=true; $("learnBtn").textContent="Sampling…";
  try {
    const d=await call("/api/learn",{method:"POST"});
    renderLiquidity(d.items||[]);
    processResearchCandidates(d.items||[]);
    $("learnUpdated").textContent=`Last sample ${new Date(d.learned_at*1000).toLocaleTimeString()}`;
    const errors=(d.items||[]).filter(x=>x.error);
    if(errors.length)msg(`Research sample completed with ${errors.length} item error(s).`,true);
    if (learnTimer) startCountdown("learn",120);
  } catch(e){msg(e.message,true)}
  finally{$("learnBtn").disabled=false;$("learnBtn").textContent="Sample Research Markets"}
}

function stopLearnAuto(){if(learnTimer)clearInterval(learnTimer);learnTimer=null;$("learnAutoBtn").textContent="Start Quiet Learning";$("learnAutoBtn").classList.add("secondary");stopCountdown("learn")}
function toggleLearnAuto(){if(learnTimer)return stopLearnAuto();learnNow();learnTimer=setInterval(learnNow,120000);$("learnAutoBtn").textContent="Stop Quiet Learning (2m)";$("learnAutoBtn").classList.remove("secondary");startCountdown("learn",120)}

async function discoverNow() {
  $("discoverBtn").disabled=true; $("discoverBtn").textContent="Scanning…";
  try {
    const d=await call("/api/discover",{method:"POST"});
    renderDiscovery(d.items||[]);
    processHiddenAlerts(d.items||[]);
    $("discoverUpdated").textContent=`Last scan ${new Date(d.scanned_at*1000).toLocaleTimeString()}`;
    const high=(d.items||[]).filter(x=>x.kind==="NPC FLOOR").length;
    const normal=(d.items||[]).filter(x=>x.kind==="UNDER MARKET"&&Number(x.discount_pct||0)>=15).length;
    const mild=(d.items||[]).filter(x=>x.kind==="UNDER MARKET"&&Number(x.discount_pct||0)<15).length;
    msg(high||normal||mild?`Hidden Deals: ${high} high-priority, ${normal} alert-worthy, ${mild} watch-only hit(s).`:`Hidden Deals checked the niche pool. No hits right now.`);
    if (discoverTimer) startCountdown("discover",120);
  } catch(e){msg(e.message,true)}
  finally{$("discoverBtn").disabled=false;$("discoverBtn").textContent="Scan Hidden Deals"}
}

function stopDiscoverAuto(){if(discoverTimer)clearInterval(discoverTimer);discoverTimer=null;$("discoverAutoBtn").textContent="Auto Scan Hidden Deals";$("discoverAutoBtn").classList.add("secondary");stopCountdown("discover")}
function toggleDiscoverAuto(){if(discoverTimer)return stopDiscoverAuto();discoverNow();discoverTimer=setInterval(discoverNow,120000);$("discoverAutoBtn").textContent="Stop Hidden Auto Scan (2m)";$("discoverAutoBtn").classList.remove("secondary");startCountdown("discover",120)}

["interval","minProfit","minRoi","stockDiscount","sound","hiddenSound"].forEach(id=>document.addEventListener("change",e=>{if(e.target.id===id)saveSettings()}));
$("key").addEventListener("keydown",e=>{if(e.key==="Enter")loadKey()});
loadSavedSettings();
appStatus();