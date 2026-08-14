let sniperTargets = new Map();
let sniperTimer = null;
let sniperRunning = false;
let sniperRequestRunning = false;
let sniperLastRequest = new Map();
let sniperAlertSignatures = new Map();
let sniperCandidateRefreshTimer = null;
let sniperTitlePulseTimer = null;
let sniperBaseTitle = document.title || "TornTools";
let sniperTitleFlip = false;

const SNIPER_TICK_MS = 250;
const SNIPER_AUTOSTART_KEY = "torntools.sniper.autostart";

function sniperTarget(id) { return sniperTargets.get(Number(id)) || null; }
function configuredSniperMax(target) { return Number(target?.configured_max_price ?? target?.max_price ?? 0); }
function effectiveSniperMax(target) { return Number(target?.effective_max_price ?? target?.max_price ?? 0); }

function ensureSniperVisualStyles() {
  if (document.getElementById("sniperVisualStyles")) return;
  const style = document.createElement("style");
  style.id = "sniperVisualStyles";
  style.textContent = `
    @keyframes ttBuyCandidatePulse {
      0%,100% { box-shadow: inset 0 0 0 2px rgba(255,75,75,.25), 0 0 0 rgba(255,75,75,0); filter: brightness(1); }
      50% { box-shadow: inset 0 0 0 3px rgba(255,90,90,.95), 0 0 18px rgba(255,65,65,.45); filter: brightness(1.18); }
    }
    #sniperRows tr.buy-candidate-pulse { animation: ttBuyCandidatePulse 1.1s ease-in-out infinite; }
    #sniperRows tr.buy-candidate-pulse td:first-child strong::before { content: "🔥 "; }
    .tt-live-edge-note { font-size: 11px; opacity: .72; display: block; margin-top: 2px; }
  `;
  document.head.appendChild(style);
}

function actionableSniperHits() {
  return [...sniperTargets.values()].filter(t => t.enabled && sniperHit(t, hiddenResults.get(Number(t.item_id))));
}

function updateSniperTitlePulse() {
  const hits = actionableSniperHits();
  if (!hits.length) {
    if (sniperTitlePulseTimer) clearInterval(sniperTitlePulseTimer);
    sniperTitlePulseTimer = null;
    sniperTitleFlip = false;
    document.title = sniperBaseTitle || "TornTools";
    return;
  }
  if (sniperTitlePulseTimer) return;
  sniperBaseTitle = document.title.replace(/^🔥 BUY CANDIDATE \(\d+\) · /, "") || "TornTools";
  sniperTitlePulseTimer = setInterval(() => {
    const liveHits = actionableSniperHits();
    if (!liveHits.length) { updateSniperTitlePulse(); return; }
    sniperTitleFlip = !sniperTitleFlip;
    document.title = sniperTitleFlip ? `🔥 BUY CANDIDATE (${liveHits.length}) · ${sniperBaseTitle}` : sniperBaseTitle;
  }, 700);
}

function sniperDueAt(target) {
  const id = Number(target.item_id), x = hiddenResults.get(id), last = Number(sniperLastRequest.get(id) || 0);
  if (!x) return last ? last + 5000 : 0;
  const cacheTs = Number(x.cache_timestamp || 0) * 1000, delay = Math.max(1, Number(x.cache_delay || 30)) * 1000;
  if (cacheTs) return Math.max(last + 3000, cacheTs + delay + 100);
  return last + 10000;
}

function nextSniperTarget() {
  const now = Date.now(), enabled = [...sniperTargets.values()].filter(x => x.enabled);
  let best = null, bestDue = Infinity;
  for (const target of enabled) { const due = sniperDueAt(target); if (due <= now && due < bestDue) { best = target; bestDue = due; } }
  return best;
}

function sniperHit(target, result) {
  return !!result && !result.error && Number(result.lowest || 0) > 0 && Number(result.lowest) <= effectiveSniperMax(target);
}

function processSniperResult(target) {
  const x = hiddenResults.get(Number(target.item_id)); if (!x || x.error) return;
  const hit = sniperHit(target, x), signature = `${x.cache_timestamp || 0}:${x.lowest || 0}:${x.qty_floor || 0}`, prior = sniperAlertSignatures.get(Number(target.item_id));
  if (!hit) { sniperAlertSignatures.set(Number(target.item_id), null); updateSniperTitlePulse(); return; }
  sniperAlertSignatures.set(Number(target.item_id), signature); if (signature === prior) { updateSniperTitlePulse(); return; }
  const max = effectiveSniperMax(target), spread = max - Number(x.lowest || 0);
  notify(`SNIPER · ${target.name}`, `${money.format(x.lowest)} is at/below your live-safe ${money.format(max)} max${spread > 0 ? ` · ${money.format(spread)} under max` : ""}. Click to open the market.`, x.market_url || target.market_url, "high");
  updateSniperTitlePulse();
}

function renderSniperTargets() {
  ensureSniperVisualStyles();
  const rows = $("sniperRows"), count = $("sniperCount"); if (!rows) return;
  const targets = [...sniperTargets.values()].sort((a,b) => Number(b.enabled)-Number(a.enabled) || String(a.name).localeCompare(String(b.name))), enabledCount = targets.filter(x => x.enabled).length;
  if (count) count.textContent = `${enabledCount} armed / ${targets.length} target${targets.length===1?"":"s"}`;
  if (!targets.length) { rows.innerHTML = '<tr><td colspan="6" class="muted">No sniper targets configured.</td></tr>'; updateSniperTitlePulse(); return; }
  rows.innerHTML = targets.map(t => {
    const x = hiddenResults.get(Number(t.item_id)), hit = sniperHit(t, x), due = sniperDueAt(t), wait = Math.max(0, Math.ceil((due-Date.now())/1000));
    const configured = configuredSniperMax(t), effective = effectiveSniperMax(t), limited = effective > 0 && configured > effective;
    const state = !t.enabled ? "DISABLED" : hit ? "🔥 BUY CANDIDATE" : x ? (wait ? `next useful check ~${wait}s` : "due now") : "waiting for first check";
    const rowClass = hit ? "priority-hit buy-candidate-pulse" : "", current = x && !x.error && x.lowest ? money.format(x.lowest) : "—";
    const maxDisplay = limited ? `<strong>${money.format(effective)}</strong><span class="tt-live-edge-note">configured ${money.format(configured)} · live edge gate active</span>` : `<strong>${money.format(effective || configured)}</strong>`;
    return `<tr class="${rowClass}"><td><strong>${t.name}</strong><br><small class="muted">Item #${t.item_id}</small></td><td>${maxDisplay}</td><td>${current}</td><td><strong>${state}</strong></td><td><button class="mini-btn" onclick="window.open('${t.market_url}','_blank','noopener')">${hit?"🔥 Open Buy Candidate":"Open Live Market"}</button></td><td><button class="mini-btn secondary" onclick="editSniperTarget(${t.item_id})">Edit</button> <button class="mini-btn secondary" onclick="toggleSniperTarget(${t.item_id})">${t.enabled?"Disable":"Enable"}</button> <button class="mini-btn secondary" onclick="removeSniperTarget(${t.item_id})">Remove</button></td></tr>`;
  }).join("");
  updateSniperTitlePulse();
}

async function loadSniperTargets() {
  const status = $("sniperStatus");
  try {
    const d = await call("/api/sniper/watchlist"); sniperTargets = new Map((d.items || []).map(x => [Number(x.item_id), x]));
    if (Array.isArray(d.discovery_ids)) discoveryIds = d.discovery_ids.map(Number); renderSniperTargets();
    if (status) status.textContent = "Sniper watchlist loaded. Flashing rows are live buy candidates; browser companion handles the final manual buy action."; return true;
  } catch (e) { if (status) status.innerHTML = `<span class="bad">Sniper backend unavailable: ${e.message}</span>`; return false; }
}

function ensureSniperCandidatePanel() {
  if ($("sniperCandidateRows")) return;
  const sniperSection = $("sniperRows")?.closest("section"); if (!sniperSection) return;
  sniperSection.insertAdjacentHTML("beforeend", `
    <div style="margin-top:24px" class="section-head"><div><h3 style="margin:0">Sniper Candidate Evidence</h3><p class="muted">Hidden Deals must prove repeatable opportunities before an item can enter Sniper.</p></div><span id="sniperCandidateCount" class="pill">Loading…</span></div>
    <div id="sniperCandidateRequirements" class="research-note"><strong>Evidence model:</strong> waiting for proof data…</div>
    <div class="table-wrap"><table class="research-table"><thead><tr><th>Item / score</th><th>Stage</th><th>Independent deals</th><th>Recoveries</th><th>False positives</th><th>Median edge</th><th>Frequency / lifetime</th><th>Learned baseline</th><th>Suggested max</th><th></th></tr></thead><tbody id="sniperCandidateRows"><tr><td colspan="10" class="muted">Loading evidence…</td></tr></tbody></table></div>`);
}

function candidateState(x) {
  if (x.already_sniper) return "ALREADY SNIPER";
  if (x.sniper_candidate) return "SNIPER CANDIDATE";
  if ((x.independent_events||0) >= 2) return "PROVING";
  if ((x.observations||0) >= 16) return "BUILDING CASE";
  return "LEARNING";
}

function renderSniperCandidates(items, requirements="") {
  const rows = $("sniperCandidateRows"), note = $("sniperCandidateRequirements"); if (!rows) return;
  if (note && requirements) note.innerHTML = `<strong>Sniper promotion bar:</strong> ${requirements}. Promotion stays manual even after the evidence bar is cleared.`;
  const filtered = [...(items||[])].slice(0, 16);
  if (!filtered.length) { rows.innerHTML = '<tr><td colspan="10" class="muted">No evidence yet. Run Hidden Deals to build proof.</td></tr>'; return; }
  rows.innerHTML = filtered.map(x => {
    const stage = candidateState(x), cls = x.sniper_candidate && !x.already_sniper ? "priority-hit" : x.already_sniper ? "research-hit" : "";
    const recovery = `${x.recovered_events||0}/${x.completed_events||0} · ${Number(x.recovery_rate||0).toFixed(0)}%`, falsePos = `${x.false_positive_events||0} · ${Number(x.false_positive_rate||0).toFixed(0)}%`, lifetime = x.median_deal_lifetime_seconds == null ? "—" : `${Math.round(Number(x.median_deal_lifetime_seconds))}s`;
    const approve = x.already_sniper ? '<span class="muted">Already armed</span>' : x.sniper_candidate ? `<button class="mini-btn" onclick="approveSniperCandidate(${x.id})">Approve to Sniper</button>` : '<span class="muted">Needs more proof</span>';
    return `<tr class="${cls}"><td><strong>${x.name}</strong><br><small class="muted">Score ${Number(x.sniper_score||0).toFixed(1)}/100 · ${x.observations||0} snapshots</small></td><td><strong>${stage}</strong></td><td>${x.independent_events||0}<br><small class="muted">${x.strong_events||0} strong</small></td><td>${recovery}</td><td>${falsePos}</td><td><strong>${Number(x.median_edge_pct||0).toFixed(1)}%</strong><br><small class="muted">best ${Number(x.best_edge_pct||0).toFixed(1)}%</small></td><td>${Number(x.opportunities_per_hour||0).toFixed(2)}/hr<br><small class="muted">life ${lifetime}</small></td><td>${x.rolling_baseline?money.format(x.rolling_baseline):"—"}<br><small class="muted">vol ${Number(x.floor_volatility_pct||0).toFixed(1)}%</small></td><td>${x.recommended_sniper_max?`<strong>${money.format(x.recommended_sniper_max)}</strong>`:"—"}</td><td>${approve}</td></tr>`;
  }).join("");
}

async function loadSniperCandidates() {
  try { const d = await call("/api/sniper/candidates"); renderSniperCandidates(d.items||[], d.requirements||""); const count=$("sniperCandidateCount"); if(count)count.textContent=`${Number(d.candidate_count||0)} ready for approval`; }
  catch(e){ const rows=$("sniperCandidateRows"); if(rows)rows.innerHTML=`<tr><td colspan="10" class="bad">Could not load candidate evidence: ${e.message}</td></tr>`; }
}

async function approveSniperCandidate(id) {
  try { const d=await call(`/api/sniper/candidates/${Number(id)}/approve`,{method:"POST"}); notify("Sniper target approved",`${d.item.name} armed at a learned max of ${money.format(d.item.max_price)}.`,d.item.market_url,"normal"); await loadSniperTargets(); await loadSniperCandidates(); if(!sniperRunning)startSniperWatch(); }
  catch(e){msg(e.message,true)}
}

function upgradeResearchLabForEvidence() {
  const table=$("liquidityRows")?.closest("table"); if(!table)return;
  const head=table.querySelector("thead tr");
  if(head)head.innerHTML="<th>Item / score</th><th>Stage</th><th>Current / baseline</th><th>Samples</th><th>Independent deals</th><th>Recoveries</th><th>False positives</th><th>Median edge</th><th>Opportunity rate</th><th>Activity</th><th>Floor volatility</th><th></th>";
  if(typeof renderLiquidity==="function"){
    renderLiquidity=function(items,requirements=""){
      const note=$("researchRequirements"); if(note&&requirements)note.innerHTML=`<strong>Case-maker graduation bar:</strong> ${requirements}. A bargain must disappear and recover before repeated snapshots count as repeated proof.`;
      if(!items.length){$("liquidityRows").innerHTML='<tr><td colspan="12" class="muted">Run samples to start building evidence.</td></tr>';return}
      $("liquidityRows").innerHTML=items.map(x=>`<tr class="${x.sniper_candidate?"priority-hit":x.graduated?"research-hit":""}"><td><strong>${x.name}</strong><br><small class="muted">Score ${Number(x.promotion_score||0).toFixed(1)}/100</small></td><td><strong>${x.stage||"LEARNING"}</strong></td><td>${x.lowest?money.format(x.lowest):"—"}<br><small class="muted">baseline ${x.rolling_baseline?money.format(x.rolling_baseline):"—"}</small></td><td>${x.observations||0}</td><td><strong>${x.independent_events||0}</strong><br><small class="muted">${x.strong_events||0} strong</small></td><td>${x.recovered_events||0}/${x.completed_events||0}<br><small class="muted">${Number(x.recovery_rate||0).toFixed(0)}%</small></td><td>${x.false_positive_events||0}<br><small class="muted">${Number(x.false_positive_rate||0).toFixed(0)}%</small></td><td><strong>${Number(x.median_edge_pct||0).toFixed(1)}%</strong><br><small class="muted">best ${Number(x.best_edge_pct||0).toFixed(1)}%</small></td><td>${Number(x.opportunities_per_hour||0).toFixed(2)}/hr<br><small class="muted">life ${x.median_deal_lifetime_seconds==null?"—":Math.round(Number(x.median_deal_lifetime_seconds))+"s"}</small></td><td>${x.activity||"Learning"}<br><small class="muted">${Number(x.activity_score||0).toFixed(0)}/100</small></td><td>${Number(x.floor_volatility_pct||0).toFixed(1)}%</td><td><button class="mini-btn" onclick="window.open('${x.market_url}','_blank','noopener')">Open</button></td></tr>`).join("");
    };
  }
}

async function saveSniperTarget() {
  const id=Number($("sniperItemId")?.value||0),name=($("sniperName")?.value||"").trim(),maxPrice=Math.trunc(Number($("sniperMaxPrice")?.value||0)); if(!id||maxPrice<=0)return msg("Sniper needs a valid item ID and max buy price.",true);
  const existing=sniperTargets.get(id);
  try{const d=await call("/api/sniper/watchlist",{method:"POST",body:JSON.stringify({item_id:id,name:name||existing?.name||`Item ${id}`,max_price:maxPrice,enabled:existing?.enabled??true})});if(Array.isArray(d.discovery_ids))discoveryIds=d.discovery_ids.map(Number);$("sniperItemId").value="";$("sniperName").value="";$("sniperMaxPrice").value="";await loadSniperTargets();await loadSniperCandidates();if(!sniperRunning)startSniperWatch()}catch(e){msg(e.message,true)}
}
function editSniperTarget(id){const t=sniperTargets.get(Number(id));if(!t)return;$("sniperItemId").value=t.item_id;$("sniperName").value=t.name;$("sniperMaxPrice").value=configuredSniperMax(t);$("sniperItemId").scrollIntoView({behavior:"smooth",block:"center")}
async function toggleSniperTarget(id){const t=sniperTargets.get(Number(id));if(!t)return;try{const d=await call("/api/sniper/watchlist",{method:"POST",body:JSON.stringify({item_id:t.item_id,name:t.name,max_price:configuredSniperMax(t),enabled:!t.enabled})});if(Array.isArray(d.discovery_ids))discoveryIds=d.discovery_ids.map(Number);await loadSniperTargets();await loadSniperCandidates()}catch(e){msg(e.message,true)}}
async function removeSniperTarget(id){try{const d=await call(`/api/sniper/watchlist/${Number(id)}`,{method:"DELETE"});if(Array.isArray(d.discovery_ids))discoveryIds=d.discovery_ids.map(Number);sniperLastRequest.delete(Number(id));sniperAlertSignatures.delete(Number(id));await loadSniperTargets();await loadSniperCandidates()}catch(e){msg(e.message,true)}}

function updateSniperStatus(next=null){const state=$("sniperState"),nextEl=$("sniperNext");if(!state||!nextEl)return;if(!sniperRunning){state.textContent="● Sniper idle";state.className="status-dot idle";nextEl.textContent="Next sniper check: —";return}const hits=actionableSniperHits();state.textContent=hits.length?`🔥 ${hits.length} BUY CANDIDATE${hits.length===1?"":"S"} · Sniper armed`:`● Sniper armed · ${[...sniperTargets.values()].filter(x=>x.enabled).length} target(s)`;state.className="status-dot live";if(next)nextEl.textContent=`Checking ${next.name} now…`;else{const enabled=[...sniperTargets.values()].filter(x=>x.enabled);if(!enabled.length)nextEl.textContent="No enabled sniper targets.";else{const upcoming=enabled.map(t=>({t,due:sniperDueAt(t)})).sort((a,b)=>a.due-b.due)[0],wait=Math.max(0,Math.ceil((upcoming.due-Date.now())/1000));nextEl.textContent=`${upcoming.t.name}: next useful API check in ~${wait}s · live userscript can react sooner while its market page is open`}}}
async function runSniperScheduler(){if(!sniperRunning)return;if(!sniperRequestRunning){const target=nextSniperTarget();updateSniperStatus(target);if(target){sniperRequestRunning=true;sniperLastRequest.set(Number(target.item_id),Date.now());try{await discoverNow([Number(target.item_id)]);processSniperResult(target)}catch(e){const status=$("sniperStatus");if(status)status.innerHTML=`<span class="bad">Sniper check error: ${e.message}</span>`}finally{sniperRequestRunning=false;renderSniperTargets()}}}updateSniperStatus();sniperTimer=setTimeout(runSniperScheduler,SNIPER_TICK_MS)}
function startSniperWatch(){if(sniperRunning)return;sniperRunning=true;localStorage.setItem(SNIPER_AUTOSTART_KEY,"1");const btn=$("sniperToggleBtn");if(btn){btn.textContent="Stop Sniper Watch";btn.classList.remove("secondary")}runSniperScheduler()}
function stopSniperWatch(){sniperRunning=false;sniperRequestRunning=false;localStorage.setItem(SNIPER_AUTOSTART_KEY,"0");if(sniperTimer)clearTimeout(sniperTimer);sniperTimer=null;const btn=$("sniperToggleBtn");if(btn){btn.textContent="Start Sniper Watch";btn.classList.add("secondary")}updateSniperStatus();updateSniperTitlePulse()}
function toggleSniperWatch(){if(sniperRunning)stopSniperWatch();else startSniperWatch()}

window.addEventListener("DOMContentLoaded",async()=>{sniperBaseTitle=document.title||"TornTools";ensureSniperVisualStyles();ensureSniperCandidatePanel();upgradeResearchLabForEvidence();const ok=await loadSniperTargets();await loadSniperCandidates();try{await loadLiquidity()}catch{}if(ok&&localStorage.getItem(SNIPER_AUTOSTART_KEY)!=="0")startSniperWatch();setInterval(()=>{if(sniperTargets.size)renderSniperTargets()},1000);sniperCandidateRefreshTimer=setInterval(loadSniperCandidates,30000)});