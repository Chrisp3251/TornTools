const money = new Intl.NumberFormat("en-US", {style:"currency", currency:"USD", maximumFractionDigits:0});

let timer=null, learnTimer=null, discoverTimer=null, learnCountdownTimer=null, discoverCountdownTimer=null;
let metadata=[], discoveryIds=[], discoverBatchIndex=0, hiddenResults=new Map();
let lastAlertKey=null, hiddenAlertState=new Map(), hiddenCacheState=new Map(), lastResearchCandidates=new Set();
let discoverRunning=false, discoverRequestRunning=false;
let hiddenPriorityStats=new Map();

const DISCOVER_ITEM_SECONDS=1;
const HIDDEN_STATS_KEY="torntools.hiddenPriority.v1";
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

function loadHiddenPriorityStats(){
  try{
    const raw=JSON.parse(localStorage.getItem(HIDDEN_STATS_KEY)||"{}");
    hiddenPriorityStats=new Map(Object.entries(raw).map(([id,v])=>[Number(id),v]));
  }catch{hiddenPriorityStats=new Map()}
}
function saveHiddenPriorityStats(){
  try{localStorage.setItem(HIDDEN_STATS_KEY,JSON.stringify(Object.fromEntries(hiddenPriorityStats)))}catch{}
}
function hiddenStat(id){
  id=Number(id);
  if(!hiddenPriorityStats.has(id))hiddenPriorityStats.set(id,{snapshots:0,hits:0,strongHits:0,npcHits:0,lastSnapshotTs:0,lastHitAt:0,lastRequestAt:0,consecutiveMisses:0});
  return hiddenPriorityStats.get(id);
}
function isHiddenHit(x){return x&& !x.error && (x.kind==="NPC FLOOR" || (x.kind==="UNDER MARKET"&&Number(x.discount_pct||0)>=8))}
function isStrongHiddenHit(x){return x&& !x.error && (x.kind==="NPC FLOOR" || (x.kind==="UNDER MARKET"&&Number(x.discount_pct||0)>=15))}
function recordHiddenOutcome(x,isNewSnapshot){
  if(!x||x.error)return;
  const s=hiddenStat(x.id),now=Date.now();s.lastRequestAt=now;
  if(!isNewSnapshot)return;
  s.snapshots=(s.snapshots||0)+1;
  if(isHiddenHit(x)){s.hits=(s.hits||0)+1;s.lastHitAt=now;s.consecutiveMisses=0}else s.consecutiveMisses=(s.consecutiveMisses||0)+1;
  if(isStrongHiddenHit(x))s.strongHits=(s.strongHits||0)+1;
  if(x.kind==="NPC FLOOR")s.npcHits=(s.npcHits||0)+1;
  s.lastSnapshotTs=Number(x.cache_timestamp||0);
  hiddenPriorityStats.set(Number(x.id),s);saveHiddenPriorityStats();
}
function hiddenPriorityInfo(id,x=null){
  const s=hiddenStat(id),n=Number(s.snapshots||0),hits=Number(s.hits||0),strong=Number(s.strongHits||0),npc=Number(s.npcHits||0);
  const hitRate=n?hits/n:0,strongRate=n?strong/n:0,currentHit=isHiddenHit(x),hardFloor=!!x?.hard_floor;
  let label="LEARNING",rank=2;
  if(n>=3&&(npc>=1||strongRate>=.25||hitRate>=.45)){label="HOT";rank=4}
  else if(n>=3&&(strongRate>=.10||hitRate>=.18)){label="WARM";rank=3}
  else if(n>=6&&hitRate<.08&&(s.consecutiveMisses||0)>=4){label="COLD";rank=1}
  if(hardFloor&&rank<3){label="WARM";rank=3}
  if(currentHit){label="HOT";rank=5}
  return{label,rank,n,hits,strong,npc,hitRate,strongRate,hardFloor};
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

function recordHiddenSnapshot(x){
  if(!x||x.error)return false;
  const id=Number(x.id),ts=Number(x.cache_timestamp||0),now=Date.now(),prior=hiddenCacheState.get(id);
  if(!prior){hiddenCacheState.set(id,{lastTimestamp:ts,changedAt:now,requestsSinceChange:0,lastRequestAt:now});return true}
  const changed=!!ts&&!!prior.lastTimestamp&&ts!==prior.lastTimestamp;
  hiddenCacheState.set(id,{lastTimestamp:ts||prior.lastTimestamp,changedAt:changed?now:prior.changedAt,requestsSinceChange:changed?0:(prior.requestsSinceChange||0)+1,lastRequestAt:now});
  return changed;
}

function freshnessInfo(x){
  const age=cacheAge(x),delay=Number(x.cache_delay||30),state=hiddenCacheState.get(Number(x.id));
  if(!state)return{label:"TRACKING",text:"First snapshot",className:"fresh-unknown",age,delay};
  const justChanged=state.changedAt&&Date.now()-state.changedAt<12000&&state.requestsSinceChange===0;
  if(justChanged)return{label:"NEW SNAPSHOT",text:"New Torn cache detected",className:"fresh-good",age,delay};
  if(state.requestsSinceChange>0)return{label:"REFRESH PENDING",text:"Waiting for Torn's next global snapshot",className:"fresh-recent",age,delay};
  return{label:"TRACKING",text:"Watching cache timestamp",className:"fresh-unknown",age,delay};
}

function renderDiscovery(items){
  if(!items.length){$("discoverRows").innerHTML=`<tr><td colspan="9" class="muted">No discovery results yet.</td></tr>`;return}
  const sorted=[...items].sort((a,b)=>{
    const pa=hiddenPriorityInfo(a.id,a),pb=hiddenPriorityInfo(b.id,b);
    return pb.rank-pa.rank || (b.deal_score||-1)-(a.deal_score||-1);
  });
  $("discoverRows").innerHTML=sorted.map(x=>{
    if(x.error)return`<tr><td><strong>${x.name}</strong></td><td colspan="7" class="bad">${x.error}</td><td></td></tr>`;
    const f=freshnessInfo(x),p=hiddenPriorityInfo(x.id,x),high=x.kind==="NPC FLOOR",normal=x.kind==="UNDER MARKET"&&Number(x.discount_pct||0)>=15,mild=x.kind==="UNDER MARKET"&&!normal;
    const rowClass=high?"priority-hit":normal?"research-hit":mild?"mild-hit":"";
    const potential=x.floor_profit>0?`${money.format(x.floor_profit)} direct cash-out spread`:(x.discount_pct>=8?`${x.discount_pct.toFixed(1)}% under market`:"—");
    const ageText=f.age===null?"Torn cache timestamp unavailable":`Torn snapshot ${f.age}s old · ${f.delay}s enforced delay`;
    const hitText=p.n?`${p.hits}/${p.n} hit snapshots · ${(p.hitRate*100).toFixed(0)}%`:`learning hit rate`;
    return`<tr class="${rowClass}"><td><strong>${x.name}</strong><br><small class="muted"><strong>${p.label}</strong> · ${hitText}</small></td><td><strong>${x.kind}</strong></td><td><span class="freshness ${f.className}">${f.label}</span><br><small class="muted">${ageText}</small></td><td>${money.format(x.lowest||0)}${x.qty_floor>1?` × ${x.qty_floor}`:""}</td><td>${x.market_value?money.format(x.market_value):"—"}</td><td>${Number(x.discount_pct||0).toFixed(1)}%</td><td>${x.hard_floor?money.format(x.hard_floor):"—"}</td><td>${potential}</td><td><button class="mini-btn" onclick="window.open('${x.market_url}','_blank','noopener')">Open</button></td></tr>`;
  }).join("");
}

function processHiddenAlerts(items){
  for(const x of items){
    if(x.error)continue;
    let priority=null;if(x.kind==="NPC FLOOR")priority="high";else if(x.kind==="UNDER MARKET"&&Number(x.discount_pct||0)>=15)priority="normal";
    const prior=hiddenAlertState.get(Number(x.id))||null;
    if(!priority){hiddenAlertState.set(Number(x.id),null);continue}
    const signature=`${x.cache_timestamp||0}:${x.lowest}:${x.qty_floor}:${x.kind}`;
    hiddenAlertState.set(Number(x.id),signature);
    if(prior===signature)continue;
    const age=cacheAge(x),ageText=age===null?"Torn cache age unknown":`${age}s-old Torn snapshot`;
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
  let remaining=seconds;state.textContent=isLearn?"● Quiet Learning active":"● Hidden scan active";state.className="status-dot live";
  const tick=()=>{next.textContent=`Next ${isLearn?"sample":"check"}: ${Math.floor(remaining/60)}m ${String(remaining%60).padStart(2,"0")}s`;progress.style.width=`${Math.max(0,Math.min(100,(1-remaining/seconds)*100))}%`;if(remaining<=0)remaining=seconds;else remaining--};tick();const h=setInterval(tick,1000);if(isLearn)learnCountdownTimer=h;else discoverCountdownTimer=h;
}
function stopCountdown(kind){const isLearn=kind==="learn",h=isLearn?learnCountdownTimer:discoverCountdownTimer;if(h)clearInterval(h);if(isLearn)learnCountdownTimer=null;else discoverCountdownTimer=null;const state=$(isLearn?"learnState":"discoverState"),next=$(isLearn?"learnNext":"discoverNext"),progress=$(isLearn?"learnProgress":"discoverProgress");state.textContent=isLearn?"● Learning idle":"● Hidden scan idle";state.className="status-dot idle";next.textContent=`Next ${isLearn?"sample":"check"}: —`;progress.style.width="0%"}

async function learnNow(){$("learnBtn").disabled=true;$("learnBtn").textContent="Sampling…";try{const d=await call("/api/learn",{method:"POST"});renderLiquidity(d.items||[]);processResearchCandidates(d.items||[]);$("learnUpdated").textContent=`Last sample ${new Date(d.learned_at*1000).toLocaleTimeString()}`;if(learnTimer)startCountdown("learn",120)}catch(e){msg(e.message,true)}finally{$("learnBtn").disabled=false;$("learnBtn").textContent="Sample Research Markets"}}
function stopLearnAuto(){if(learnTimer)clearInterval(learnTimer);learnTimer=null;$("learnAutoBtn").textContent="Start Quiet Learning";$("learnAutoBtn").classList.add("secondary");stopCountdown("learn")}
function toggleLearnAuto(){if(learnTimer)return stopLearnAuto();learnNow();learnTimer=setInterval(learnNow,120000);$("learnAutoBtn").textContent="Stop Quiet Learning (2m)";$("learnAutoBtn").classList.remove("secondary");startCountdown("learn",120)}

async function discoverNow(itemIds=null){
  const isAuto=Array.isArray(itemIds)&&itemIds.length>0;
  if(!isAuto){$("discoverBtn").disabled=true;$("discoverBtn").textContent="Scanning full pool…"}
  try{
    const suffix=isAuto?`?ids=${encodeURIComponent(itemIds.join(","))}`:"",d=await call(`/api/discover${suffix}`,{method:"POST"});
    for(const x of d.items||[]){const changed=recordHiddenSnapshot(x);recordHiddenOutcome(x,changed);hiddenResults.set(Number(x.id),x)}
    renderDiscovery([...hiddenResults.values()]);processHiddenAlerts(d.items||[]);
    $("discoverUpdated").textContent=`Last check ${new Date(d.scanned_at*1000).toLocaleTimeString()} · ${hiddenResults.size}/${d.pool_count||discoveryIds.length} items seen`;
  }catch(e){msg(e.message,true)}finally{if(!isAuto){$("discoverBtn").disabled=false;$("discoverBtn").textContent="Scan Full Hidden Pool"}}
}

function hiddenNextDue(id){
  const now=Date.now(),x=hiddenResults.get(Number(id)),s=hiddenStat(id),c=hiddenCacheState.get(Number(id)),p=hiddenPriorityInfo(id,x);
  if(!x||!c)return 0;
  const delay=Math.max(1,Number(x.cache_delay||30))*1000,cacheTs=Number(x.cache_timestamp||0)*1000;
  if(cacheTs&&now<cacheTs+delay)return cacheTs+delay+250;
  const pending=Number(c.requestsSinceChange||0)>0,hardFloor=!!x.hard_floor;
  const pendingIntervals={5:4000,4:5000,3:8000,2:11000,1:15000};
  const normalIntervals={5:6000,4:8000,3:14000,2:22000,1:35000};
  let gap=(pending?pendingIntervals:normalIntervals)[p.rank]||12000;
  if(hardFloor)gap=Math.min(gap,pending?7000:12000);
  const maxGap=hardFloor?20000:45000;
  return Math.min(Number(s.lastRequestAt||0)+gap,Number(s.lastRequestAt||0)+maxGap);
}
function chooseNextHiddenId(){
  if(!discoveryIds.length)return null;
  const now=Date.now();let best=null,bestScore=-Infinity;
  for(const rawId of discoveryIds){
    const id=Number(rawId),x=hiddenResults.get(id),s=hiddenStat(id),p=hiddenPriorityInfo(id,x),due=hiddenNextDue(id);
    const since=now-Number(s.lastRequestAt||0),hardFloor=!!x?.hard_floor,maxWait=hardFloor?20000:45000;
    if(!x){return id}
    if(now<due&&since<maxWait)continue;
    const overdue=Math.max(0,(now-due)/1000),starve=Math.max(0,(since-(hardFloor?12000:30000))/1000);
    const currentBonus=isStrongHiddenHit(x)?30:isHiddenHit(x)?18:0;
    const floorBonus=hardFloor?28:0;
    const score=p.rank*20+overdue+starve*2+currentBonus+floorBonus;
    if(score>bestScore){bestScore=score;best=id}
  }
  return best;
}
function updateHiddenSchedulerStatus(nextId=null){
  if(!discoverRunning)return;
  const hot=discoveryIds.filter(id=>hiddenPriorityInfo(id,hiddenResults.get(Number(id))).label==="HOT").length;
  $("discoverState").textContent=`● Adaptive Hidden scan · ${hot} hot item${hot===1?"":"s"}`;$("discoverState").className="status-dot live";
  if(nextId){const x=hiddenResults.get(Number(nextId)),p=hiddenPriorityInfo(nextId,x);$("discoverNext").textContent=`Next priority: ${x?.name||`Item ${nextId}`} · ${p.label}${x?.hard_floor?" · NPC floor protected":""}`}
  else $("discoverNext").textContent="Waiting for the next useful cache window…";
}
async function runDiscoveryScheduler(){
  if(!discoverRunning)return;
  if(discoverRequestRunning){discoverTimer=setTimeout(runDiscoveryScheduler,DISCOVER_ITEM_SECONDS*1000);return}
  const id=chooseNextHiddenId();updateHiddenSchedulerStatus(id);
  if(id!==null){
    discoverRequestRunning=true;
    try{await discoverNow([id])}finally{discoverRequestRunning=false}
  }
  if(discoverRunning)discoverTimer=setTimeout(runDiscoveryScheduler,DISCOVER_ITEM_SECONDS*1000);
}

function stopDiscoverAuto(){
  discoverRunning=false;discoverRequestRunning=false;if(discoverTimer)clearTimeout(discoverTimer);discoverTimer=null;
  $("discoverAutoBtn").textContent="Start Smart Hidden Scan";$("discoverAutoBtn").classList.add("secondary");stopCountdown("discover");
}
function toggleDiscoverAuto(){
  if(discoverRunning)return stopDiscoverAuto();
  discoverRunning=true;discoverBatchIndex=0;
  $("discoverAutoBtn").textContent="Stop Adaptive Hidden Scan";$("discoverAutoBtn").classList.remove("secondary");
  $("discoverState").textContent="● Adaptive Hidden scan · learning priorities";$("discoverState").className="status-dot live";
  $("discoverNext").textContent="NPC-floor items stay prioritized; other cold markets still have a 45s anti-starvation check.";
  $("discoverProgress").style.width="0%";
  runDiscoveryScheduler();
}

["interval","minProfit","minRoi","stockDiscount","sound","hiddenSound"].forEach(id=>document.addEventListener("change",e=>{if(e.target.id===id)saveSettings()}));
$("key").addEventListener("keydown",e=>{if(e.key==="Enter")loadKey()});
setInterval(()=>{if(hiddenResults.size)renderDiscovery([...hiddenResults.values()])},1000);
loadSavedSettings();loadHiddenPriorityStats();appStatus();
