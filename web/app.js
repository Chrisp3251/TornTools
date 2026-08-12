const money = new Intl.NumberFormat("en-US", {style:"currency", currency:"USD", maximumFractionDigits:0});

let timer=null, learnTimer=null, discoverTimer=null, learnCountdownTimer=null, discoverCountdownTimer=null;
let metadata=[], discoveryIds=[], discoverBatchIndex=0, hiddenResults=new Map();
let lastAlertKey=null, hiddenAlertState=new Map(), lastResearchCandidates=new Set();

const DISCOVER_BATCH_SIZE=3;
const DISCOVER_BATCH_SECONDS=5;
const $=id=>document.getElementById(id);

async function call(path,opts={}) {
  const r=await fetch(path,{headers:{"Content-Type":"application/json"},...opts});
  let d={}; try{d=await r.json()}catch{}
  if(!r.ok) throw new Error(d.detail||`HTTP ${r.status}`);
  return d;
}

function msg(text,bad=false){const e=$("msg");if(!e)return;e.textContent=text;e.className=bad?"bad":"good"}

function saveSettings(){
  localStorage.setItem("torntools.v044.settings",JSON.stringify({
    interval:$("interval").value,minProfit:$("minProfit").value,minRoi:$("minRoi").value,stockDiscount:$("stockDiscount").value,
    sound:$("sound").checked,hiddenSound:$("hiddenSound").checked,
    watched:[...document.querySelectorAll(".watch-toggle")].filter(x=>x.checked).map(x=>Number(x.value))
  }));
}

function loadSavedSettings(){
  try{
    const raw=localStorage.getItem("torntools.v044.settings")||localStorage.getItem("torntools.v043.settings")||localStorage.getItem("torntools.v042.settings")||localStorage.getItem("torntools.v04.settings")||"{}";
    const s=JSON.parse(raw);
    if(s.interval)$("interval").value=s.interval;
    if(s.minProfit!==undefined)$("minProfit").value=s.minProfit;
    if(s.minRoi!==undefined)$("minRoi").value=s.minRoi;
    if(s.stockDiscount!==undefined)$("stockDiscount").value=s.stockDiscount;
    if(s.sound!==undefined)$("sound").checked=!!s.sound;
    if(s.hiddenSound!==undefined)$("hiddenSound").checked=!!s.hiddenSound;
    return Array.isArray(s.watched)?s.watched:null;
  }catch{return null}
}

async function appStatus(){
  try{
    const d=await call("/api/status"); metadata=d.items||[]; discoveryIds=d.discovery_ids||[];
    const e=$("status");e.textContent=d.key_loaded?`● V${d.version} · key loaded`:`● V${d.version} · key needed`;e.className=d.key_loaded?"pill good":"pill warn";
    const p=$("apiPanel");if(p)p.hidden=!!d.key_loaded;
    renderWatchlist();await loadLiquidity();updateNotificationButton();
  }catch{const e=$("status");e.textContent="Backend unavailable";e.className="pill bad"}
}

function renderWatchlist(){
  const h=$("watchlist"),saved=loadSavedSettings();h.innerHTML="";
  metadata.forEach(i=>{const checked=saved?saved.includes(i.id):i.enabled;h.insertAdjacentHTML("beforeend",`<label class="watch-chip ${i.mode}"><input class="watch-toggle" type="checkbox" value="${i.id}" ${checked?"checked":""} onchange="watchChanged()"><span class="watch-name">${i.name}</span><span class="mode-tag">${i.mode==="stock"?"PERSONAL USE":"RESALE"}</span><small>${i.note}</small></label>`)});
  watchChanged(false);
}
function watchChanged(save=true){$("watchingCount").textContent=document.querySelectorAll(".watch-toggle:checked").length;if(save)saveSettings()}

async function loadKey(){
  const k=$("key").value.trim();if(!k)return msg("Paste a key first.",true);
  try{$("loadKeyBtn").disabled=true;const r=await call("/api/key",{method:"POST",body:JSON.stringify({api_key:k})});$("key").value="";msg(r.message||"Key loaded.");await appStatus()}
  catch(e){msg(e.message,true)}finally{$("loadKeyBtn").disabled=false}
}
async function forgetKey(){try{await call("/api/key",{method:"DELETE"});stopAuto();stopLearnAuto();stopDiscoverAuto();await appStatus()}catch(e){msg(e.message,true)}}

function selectedIds(){return[...document.querySelectorAll(".watch-toggle:checked")].map(x=>x.value)}
function isDeal(i){if(i.error)return false;if(i.mode==="stock")return Number(i.discount_pct||0)>=Number($("stockDiscount").value||0);return Number(i.floor_clear_profit_after_fee||0)>=Number($("minProfit").value||0)&&Number(i.net_roi_after_fee||0)>=Number($("minRoi").value||0)}
function score(i){if(i.error)return-999;if(i.mode==="stock")return Number(i.discount_pct||0)*10;return Math.max(0,Number(i.net_roi_after_fee||0)*8+Number(i.floor_clear_profit_after_fee||0)/50000)}
function card(i){
  if(i.error)return`<article class="deal-card error-card"><div class="card-top"><div><h3>${i.name}</h3><span class="mode-tag">ERROR</span></div></div><p>${i.error}</p></article>`;
  const d=isDeal(i),personal=i.mode==="stock",disc=Number(i.discount_pct||0),profit=Number(i.floor_clear_profit_after_fee||0),roi=Number(i.net_roi_after_fee||0);
  return`<article class="deal-card ${d?"deal":""}"><div class="card-top"><div><h3>${i.name}</h3><span class="mode-tag">${personal?"PERSONAL USE":"RESALE"}</span></div><span class="deal-badge">${d?"GOOD BUY":"WATCH"}</span></div><div class="hero-price">${money.format(i.lowest)}</div><div class="hero-sub">${i.qty_floor} available at this price</div><div class="metric-list">${personal?`<div><span>Typical price</span><strong>${money.format(i.reference||0)}</strong></div><div><span>Discount</span><strong>${disc.toFixed(2)}%</strong></div>`:`<div><span>Estimated profit</span><strong>${money.format(profit)}</strong></div><div><span>ROI after fee</span><strong>${roi.toFixed(2)}%</strong></div><div><span>Total buy cost</span><strong>${money.format(i.floor_clear_capital||0)}</strong></div>`}<div><span>Next listing</span><strong>${i.next_higher?money.format(i.next_higher):"—"}</strong></div></div><button class="open-market" onclick="window.open('${i.market_url}','_blank','noopener')">Open Market</button></article>`;
}

function beep(priority="normal"){
  try{const c=new(window.AudioContext||window.webkitAudioContext)(),o=c.createOscillator(),g=c.createGain();o.connect(g);g.connect(c.destination);o.frequency.value=priority==="high"?1150:880;g.gain.value=priority==="high"?.09:.05;o.start();o.stop(c.currentTime+(priority==="high"?.28:.16))}catch{}
}
function maybeSound(items){if(!$("sound").checked)return;const deals=items.filter(isDeal).sort((a,b)=>score(b)-score(a));if(!deals.length)return;const t=deals[0],k=`${t.id}:${t.lowest}:${Math.round(score(t))}`;if(k===lastAlertKey)return;lastAlertKey=k;beep("normal")}
function renderScan(d){
  const items=[...(d.items||[])].sort((a,b)=>score(b)-score(a)),deals=items.filter(isDeal);$("cards").innerHTML=items.length?items.map(card).join(""):`<div class="empty">Nothing selected.</div>`;$("dealCount").textContent=deals.length;
  if(deals.length){const b=deals[0];$("bestDeal").textContent=b.name;$("bestDealSub").textContent=b.mode==="stock"?`${b.discount_pct.toFixed(2)}% below typical price`:`${money.format(b.floor_clear_profit_after_fee)} estimated profit · ${b.net_roi_after_fee.toFixed(2)}% ROI`}else{$("bestDeal").textContent="None";$("bestDealSub").textContent="Nothing meets your buy settings right now"}
  $("lastUpdated").textContent=`Updated ${new Date(d.scanned_at*1000).toLocaleTimeString()}`;maybeSound(items);
}
async function scanNow(){const ids=selectedIds();if(!ids.length)return msg("Select at least one item to watch.",true);saveSettings();$("scanBtn").disabled=true;$("scanBtn").textContent="Scanning…";try{renderScan(await call(`/api/scan?ids=${encodeURIComponent(ids.join(","))}`))}catch(e){msg(e.message,true)}finally{$("scanBtn").disabled=false;$("scanBtn").textContent="Scan Now"}}
function stopAuto(){if(timer)clearInterval(timer);timer=null;$("autoBtn").textContent="Start Auto Scan";$("autoBtn").classList.add("secondary")}
function toggleAuto(){if(timer)return stopAuto();saveSettings();scanNow();const s=Math.max(15,Number($("interval").value||30));timer=setInterval(scanNow,s*1000);$("autoBtn").textContent=`Stop Auto Scan (${s}s)`;$("autoBtn").classList.remove("secondary")}

function isResearchCandidate(x){return Number(x.observations||0)>=8&&["Active","Very active"].includes(x.label)&&Number(x.gap_events||0)>=3&&Number(x.largest_gap_pct||0)>=3}
function researchDiscount(x){const low=Number(x.lowest||0),avg=Number(x.average_price||0);return low>0&&avg>0?((avg-low)/avg*100):0}
function renderLiquidity(items){
  if(!items.length){$("liquidityRows").innerHTML=`<tr><td colspan="10" class="muted">Run a few samples to start learning.</td></tr>`;return}
  $("liquidityRows").innerHTML=items.map(x=>{if(x.error)return`<tr><td><strong>${x.name}</strong></td><td colspan="8" class="bad">${x.error}</td><td></td></tr>`;const candidate=isResearchCandidate(x),disc=researchDiscount(x),signal=candidate?"PROMOTION CANDIDATE":Number(x.observations||0)<4?"LEARNING":"WATCH";return`<tr class="${candidate?"research-candidate":""}"><td><strong>${x.name}</strong></td><td><strong>${signal}</strong></td><td>${x.lowest?money.format(x.lowest):"—"}</td><td>${x.average_price?money.format(x.average_price):"—"}</td><td>${disc.toFixed(1)}%</td><td>${x.label||"Learning"} · ${Number(x.score||0).toFixed(0)}</td><td>${x.observations||0}</td><td>${x.gap_events||0}</td><td>${Number(x.largest_gap_pct||0).toFixed(2)}%</td><td><button class="mini-btn" onclick="window.open('${x.market_url}','_blank','noopener')">Open</button></td></tr>`}).join("");
}
async function loadLiquidity(){try{const d=await call("/api/liquidity");renderLiquidity(d.items||[])}catch{}}

function notify(title,body,url=null,priority="normal"){
  if(priority&&$("hiddenSound")?.checked)beep(priority);
  if(!("Notification" in window)||Notification.permission!=="granted")return;
  try{const n=new Notification(title,{body,tag:`torntools-${title}`,renotify:true});if(url)n.onclick=()=>{window.focus();window.open(url,"_blank","noopener");n.close()};setTimeout(()=>n.close(),15000)}catch{}
}
function updateNotificationButton(){const b=$("notifyBtn");if(!b)return;if(!("Notification" in window)){b.textContent="Desktop Alerts Unsupported";b.disabled=true;return}if(Notification.permission==="granted"){b.textContent="Desktop Alerts Enabled";b.classList.add("enabled-alerts")}else if(Notification.permission==="denied"){b.textContent="Desktop Alerts Blocked";b.disabled=true}else b.textContent="Enable Desktop Alerts"}
async function enableNotifications(){if(!("Notification" in window))return;try{await Notification.requestPermission();updateNotificationButton()}catch{}}

function cacheAge(x){if(!x.cache_timestamp)return null;return Math.max(0,Math.floor(Date.now()/1000-Number(x.cache_timestamp)))}
function freshnessInfo(x){
  const age=cacheAge(x),delay=Number(x.cache_delay||30);
  if(age===null)return{label:"UNKNOWN",text:"Unknown cache age",className:"fresh-unknown",age:null,delay};
  if(age<10)return{label:"FRESH",text:`Fresh · ${age}s old`,className:"fresh-good",age,delay};
  if(age<delay)return{label:"RECENT",text:`Recent · ${age}s old`,className:"fresh-recent",age,delay};
  return{label:"CACHE DUE",text:`Cached · ${age}s old`,className:"fresh-stale",age,delay};
}

function renderDiscovery(items){
  if(!items.length){$("discoverRows").innerHTML=`<tr><td colspan="9" class="muted">No discovery results yet.</td></tr>`;return}
  const sorted=[...items].sort((a,b)=>(b.deal_score||-1)-(a.deal_score||-1));
  $("discoverRows").innerHTML=sorted.map(x=>{
    if(x.error)return`<tr><td><strong>${x.name}</strong></td><td colspan="7" class="bad">${x.error}</td><td></td></tr>`;
    const f=freshnessInfo(x),high=x.kind==="NPC FLOOR",normal=x.kind==="UNDER MARKET"&&Number(x.discount_pct||0)>=15,mild=x.kind==="UNDER MARKET"&&!normal;
    const rowClass=high?"priority-hit":normal?"research-hit":mild?"mild-hit":"";
    const potential=x.floor_profit>0?`${money.format(x.floor_profit)} guaranteed spread`:(x.discount_pct>=8?`${x.discount_pct.toFixed(1)}% under market`:"—");
    return`<tr class="${rowClass}"><td><strong>${x.name}</strong><br><small class="muted">${x.activity||"Learning"} · ${x.samples||0} samples</small></td><td><strong>${x.kind}</strong></td><td><span class="freshness ${f.className}">${f.label}</span><br><small class="muted">${f.age===null?"timestamp unavailable":`${f.age}s old · ${f.delay}s cache`}</small></td><td>${money.format(x.lowest||0)}${x.qty_floor>1?` × ${x.qty_floor}`:""}</td><td>${x.market_value?money.format(x.market_value):"—"}</td><td>${Number(x.discount_pct||0).toFixed(1)}%</td><td>${x.hard_floor?money.format(x.hard_floor):"—"}</td><td>${potential}</td><td><button class="mini-btn" onclick="window.open('${x.market_url}','_blank','noopener')">Open</button></td></tr>`;
  }).join("");
}

function processHiddenAlerts(items){
  for(const x of items){
    if(x.error)continue;
    let priority=null;if(x.kind==="NPC FLOOR")priority="high";else if(x.kind==="UNDER MARKET"&&Number(x.discount_pct||0)>=15)priority="normal";
    const prior=hiddenAlertState.get(Number(x.id))||null;
    if(!priority){hiddenAlertState.set(Number(x.id),null);continue}
    const f=freshnessInfo(x),signature=`${x.cache_timestamp||0}:${x.lowest}:${x.qty_floor}:${x.kind}`;
    hiddenAlertState.set(Number(x.id),signature);
    if(prior===signature)continue;
    if(f.age!==null&&f.age>f.delay)continue;
    const ageText=f.age===null?"cache age unknown":`${f.age}s-old Torn snapshot`;
    const body=x.kind==="NPC FLOOR"?`${money.format(x.lowest)} buy · ${money.format(x.hard_floor)} NPC floor · ${money.format(x.floor_profit||0)} spread · ${ageText}`:`${money.format(x.lowest)} buy · ${Number(x.discount_pct||0).toFixed(1)}% below market · ${ageText}`;
    notify(`${priority==="high"?"HIGH PRIORITY · ":""}${x.name}`,body,x.market_url,priority);
  }
}

function processResearchCandidates(items){
  const current=new Set();for(const x of items){if(!isResearchCandidate(x))continue;current.add(String(x.id));if(lastResearchCandidates.has(String(x.id)))continue;if("Notification" in window&&Notification.permission==="granted"){try{const n=new Notification(`Research candidate · ${x.name}`,{body:`${x.label} · ${x.observations} samples · ${x.gap_events} gap events · largest gap ${Number(x.largest_gap_pct||0).toFixed(1)}%`});n.onclick=()=>window.open(x.market_url,"_blank","noopener")}catch{}}}lastResearchCandidates=current;
}

function startCountdown(kind,seconds){
  const isLearn=kind==="learn",state=$(isLearn?"learnState":"discoverState"),next=$(isLearn?"learnNext":"discoverNext"),progress=$(isLearn?"learnProgress":"discoverProgress");
  if(isLearn&&learnCountdownTimer)clearInterval(learnCountdownTimer);if(!isLearn&&discoverCountdownTimer)clearInterval(discoverCountdownTimer);
  let remaining=seconds;state.textContent=isLearn?"● Quiet Learning active":`● Cache-aware Hidden scan · ${DISCOVER_BATCH_SIZE} items/batch`;state.className="status-dot live";
  const tick=()=>{next.textContent=`Next ${isLearn?"sample":"batch"}: ${Math.floor(remaining/60)}m ${String(remaining%60).padStart(2,"0")}s`;progress.style.width=`${Math.max(0,Math.min(100,(1-remaining/seconds)*100))}%`;if(remaining<=0)remaining=seconds;else remaining--};tick();const h=setInterval(tick,1000);if(isLearn)learnCountdownTimer=h;else discoverCountdownTimer=h;
}
function stopCountdown(kind){const isLearn=kind==="learn",h=isLearn?learnCountdownTimer:discoverCountdownTimer;if(h)clearInterval(h);if(isLearn)learnCountdownTimer=null;else discoverCountdownTimer=null;const state=$(isLearn?"learnState":"discoverState"),next=$(isLearn?"learnNext":"discoverNext"),progress=$(isLearn?"learnProgress":"discoverProgress");state.textContent=isLearn?"● Learning idle":"● Hidden scan idle";state.className="status-dot idle";next.textContent=`Next ${isLearn?"sample":"batch"}: —`;progress.style.width="0%"}

async function learnNow(){$("learnBtn").disabled=true;$("learnBtn").textContent="Sampling…";try{const d=await call("/api/learn",{method:"POST"});renderLiquidity(d.items||[]);processResearchCandidates(d.items||[]);$("learnUpdated").textContent=`Last sample ${new Date(d.learned_at*1000).toLocaleTimeString()}`;if(learnTimer)startCountdown("learn",120)}catch(e){msg(e.message,true)}finally{$("learnBtn").disabled=false;$("learnBtn").textContent="Sample Research Markets"}}
function stopLearnAuto(){if(learnTimer)clearInterval(learnTimer);learnTimer=null;$("learnAutoBtn").textContent="Start Quiet Learning";$("learnAutoBtn").classList.add("secondary");stopCountdown("learn")}
function toggleLearnAuto(){if(learnTimer)return stopLearnAuto();learnNow();learnTimer=setInterval(learnNow,120000);$("learnAutoBtn").textContent="Stop Quiet Learning (2m)";$("learnAutoBtn").classList.remove("secondary");startCountdown("learn",120)}

function nextDiscoveryBatch(){if(!discoveryIds.length)return[];const batch=[];for(let n=0;n<Math.min(DISCOVER_BATCH_SIZE,discoveryIds.length);n++)batch.push(discoveryIds[(discoverBatchIndex+n)%discoveryIds.length]);discoverBatchIndex=(discoverBatchIndex+DISCOVER_BATCH_SIZE)%discoveryIds.length;return batch}
async function discoverNow(batchIds=null){
  const isBatch=Array.isArray(batchIds)&&batchIds.length>0;if(!isBatch){$("discoverBtn").disabled=true;$("discoverBtn").textContent="Scanning full pool…"}
  try{const suffix=isBatch?`?ids=${encodeURIComponent(batchIds.join(","))}`:"",d=await call(`/api/discover${suffix}`,{method:"POST"});for(const x of d.items||[])hiddenResults.set(Number(x.id),x);renderDiscovery([...hiddenResults.values()]);processHiddenAlerts(d.items||[]);$("discoverUpdated").textContent=`Last batch ${new Date(d.scanned_at*1000).toLocaleTimeString()} · ${hiddenResults.size}/${d.pool_count||discoveryIds.length} items seen`;if(discoverTimer)startCountdown("discover",DISCOVER_BATCH_SECONDS)}catch(e){msg(e.message,true)}finally{if(!isBatch){$("discoverBtn").disabled=false;$("discoverBtn").textContent="Scan Full Hidden Pool"}}
}
async function runDiscoveryBatch(){const batch=nextDiscoveryBatch();if(batch.length)await discoverNow(batch)}
function stopDiscoverAuto(){if(discoverTimer)clearInterval(discoverTimer);discoverTimer=null;$("discoverAutoBtn").textContent="Start Cache-Aware Hidden Scan";$("discoverAutoBtn").classList.add("secondary");stopCountdown("discover")}
function toggleDiscoverAuto(){if(discoverTimer)return stopDiscoverAuto();discoverBatchIndex=0;runDiscoveryBatch();discoverTimer=setInterval(runDiscoveryBatch,DISCOVER_BATCH_SECONDS*1000);$("discoverAutoBtn").textContent="Stop Hidden Scan (5s batches)";$("discoverAutoBtn").classList.remove("secondary");startCountdown("discover",DISCOVER_BATCH_SECONDS)}

["interval","minProfit","minRoi","stockDiscount","sound","hiddenSound"].forEach(id=>document.addEventListener("change",e=>{if(e.target.id===id)saveSettings()}));
$("key").addEventListener("keydown",e=>{if(e.key==="Enter")loadKey()});
setInterval(()=>{if(hiddenResults.size)renderDiscovery([...hiddenResults.values()])},1000);
loadSavedSettings();appStatus();
