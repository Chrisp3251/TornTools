const money = new Intl.NumberFormat("en-US", {style:"currency", currency:"USD", maximumFractionDigits:0});

let metadata=[];
let discoveryIds=[];
let hiddenResults=new Map();
let hiddenAlertState=new Map();
let hiddenCacheState=new Map();
let hiddenPriorityStats=new Map();
let intelligenceRunning=false;
let intelligenceTimer=null;
let intelligenceResearchTimer=null;
let intelligenceResearchKickoff=null;
let intelligenceRequestRunning=false;
let brokerTimer=null;

const INTELLIGENCE_TICK_MS=1000;
const BACKGROUND_RESEARCH_MS=10*60*1000;
const HIDDEN_STATS_KEY="torntools.hiddenPriority.v1";
const INTELLIGENCE_AUTOSTART_KEY="torntools.intelligence.autostart";
const $=id=>document.getElementById(id);

async function call(path,opts={}) {
  const r=await fetch(path,{headers:{"Content-Type":"application/json"},...opts});
  let d={}; try{d=await r.json()}catch{}
  if(!r.ok) throw new Error(d.detail||`HTTP ${r.status}`);
  return d;
}

function msg(text,bad=false){const e=$("msg");if(!e)return;e.textContent=text;e.className=bad?"bad":"good"}

async function appStatus(){
  try{
    const d=await call("/api/status");
    metadata=d.items||[];
    discoveryIds=(d.discovery_ids||[]).map(Number);
    const e=$("status");
    if(e){e.textContent=d.key_loaded?`● V${d.version} · key loaded`:`● V${d.version} · key needed`;e.className=d.key_loaded?"pill good":"pill warn"}
    const p=$("apiPanel");if(p)p.hidden=!!d.key_loaded;
    await loadLiquidity();
    updateNotificationButton();
    await refreshBrokerStatus();
  }catch{
    const e=$("status");if(e){e.textContent="Backend unavailable";e.className="pill bad"}
  }
}

async function loadKey(){
  const input=$("key"),btn=$("loadKeyBtn");
  const k=input?input.value.trim():"";
  if(!k)return msg("Paste a key first.",true);
  try{
    if(btn)btn.disabled=true;
    const r=await call("/api/key",{method:"POST",body:JSON.stringify({api_key:k})});
    if(input)input.value="";
    msg(r.message||"Key loaded.");
    await appStatus();
  }catch(e){msg(e.message,true)}finally{if(btn)btn.disabled=false}
}

async function forgetKey(){
  try{
    await call("/api/key",{method:"DELETE"});
    stopMarketIntelligence();
    await appStatus();
  }catch(e){msg(e.message,true)}
}

function loadHiddenPriorityStats(){
  try{
    const raw=JSON.parse(localStorage.getItem(HIDDEN_STATS_KEY)||"{}");
    hiddenPriorityStats=new Map(Object.entries(raw).map(([id,v])=>[Number(id),v]));
  }catch{hiddenPriorityStats=new Map()}
}
function saveHiddenPriorityStats(){try{localStorage.setItem(HIDDEN_STATS_KEY,JSON.stringify(Object.fromEntries(hiddenPriorityStats)))}catch{}}
function hiddenStat(id){
  id=Number(id);
  if(!hiddenPriorityStats.has(id))hiddenPriorityStats.set(id,{snapshots:0,hits:0,strongHits:0,npcHits:0,lastSnapshotTs:0,lastHitAt:0,lastRequestAt:0,consecutiveMisses:0});
  return hiddenPriorityStats.get(id);
}
function isHiddenHit(x){return x&&!x.error&&(x.kind==="NPC FLOOR"||(x.kind==="UNDER MARKET"&&Number(x.discount_pct||0)>=8))}
function isStrongHiddenHit(x){return x&&!x.error&&(x.kind==="NPC FLOOR"||(x.kind==="UNDER MARKET"&&Number(x.discount_pct||0)>=15))}
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

function beep(priority="normal"){
  try{const c=new(window.AudioContext||window.webkitAudioContext)(),o=c.createOscillator(),g=c.createGain();o.connect(g);g.connect(c.destination);o.frequency.value=priority==="high"?1150:880;g.gain.value=priority==="high"?.09:.05;o.start();o.stop(c.currentTime+(priority==="high"?.28:.16))}catch{}
}
function notify(title,body,url=null,priority="normal"){
  if(priority&&$("hiddenSound")?.checked)beep(priority);
  if(!("Notification" in window)||Notification.permission!=="granted")return;
  try{const n=new Notification(title,{body,tag:`torntools-${title}`,renotify:true});if(url)n.onclick=()=>{window.focus();window.open(url,"_blank","noopener");n.close()};setTimeout(()=>n.close(),15000)}catch{}
}
function updateNotificationButton(){
  const b=$("notifyBtn");if(!b)return;
  if(!("Notification" in window)){b.textContent="Desktop Alerts Unsupported";b.disabled=true;return}
  if(Notification.permission==="granted"){b.textContent="Desktop Alerts Enabled";b.classList.add("enabled-alerts")}
  else if(Notification.permission==="denied"){b.textContent="Desktop Alerts Blocked";b.disabled=true}
  else b.textContent="Enable Desktop Alerts";
}
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
  if(!state)return{label:"TRACKING",className:"fresh-unknown",age,delay};
  const justChanged=state.changedAt&&Date.now()-state.changedAt<12000&&state.requestsSinceChange===0;
  if(justChanged)return{label:"NEW SNAPSHOT",className:"fresh-good",age,delay};
  if(state.requestsSinceChange>0)return{label:"REFRESH PENDING",className:"fresh-recent",age,delay};
  return{label:"TRACKING",className:"fresh-unknown",age,delay};
}
function renderDiscovery(items){
  const root=$("discoverRows");if(!root)return;
  if(!items.length){root.innerHTML='<tr><td colspan="9" class="muted">No market intelligence results yet.</td></tr>';return}
  const sorted=[...items].sort((a,b)=>{const pa=hiddenPriorityInfo(a.id,a),pb=hiddenPriorityInfo(b.id,b);return pb.rank-pa.rank||(b.deal_score||-1)-(a.deal_score||-1)});
  root.innerHTML=sorted.map(x=>{
    if(x.error)return`<tr><td><strong>${x.name}</strong></td><td colspan="7" class="bad">${x.error}</td><td></td></tr>`;
    const f=freshnessInfo(x),p=hiddenPriorityInfo(x.id,x),high=x.kind==="NPC FLOOR",normal=x.kind==="UNDER MARKET"&&Number(x.discount_pct||0)>=15,mild=x.kind==="UNDER MARKET"&&!normal;
    const rowClass=high?"priority-hit":normal?"research-hit":mild?"mild-hit":"";
    const potential=x.floor_profit>0?`${money.format(x.floor_profit)} direct cash-out spread`:(x.discount_pct>=8?`${Number(x.discount_pct).toFixed(1)}% under market`:"—");
    const ageText=f.age===null?"cache timestamp unavailable":`${f.age}s old · ${f.delay}s cache delay`;
    const hitText=p.n?`${p.hits}/${p.n} hit snapshots · ${(p.hitRate*100).toFixed(0)}%`:`learning hit rate`;
    return`<tr class="${rowClass}"><td><strong>${x.name}</strong><br><small class="muted"><strong>${p.label}</strong> · ${hitText}</small></td><td><strong>${x.kind}</strong></td><td><span class="freshness ${f.className}">${f.label}</span><br><small class="muted">${ageText}</small></td><td>${money.format(x.lowest||0)}${x.qty_floor>1?` × ${x.qty_floor}`:""}</td><td>${x.market_value?money.format(x.market_value):"—"}</td><td>${Number(x.discount_pct||0).toFixed(1)}%</td><td>${x.hard_floor?money.format(x.hard_floor):"—"}</td><td>${potential}</td><td><button class="mini-btn" onclick="window.open('${x.market_url}','_blank','noopener')">Open</button></td></tr>`;
  }).join("");
}
function processHiddenAlerts(items){
  for(const x of items||[]){
    if(x.error)continue;
    let priority=null;
    if(x.kind==="NPC FLOOR")priority="high";
    else if(x.kind==="UNDER MARKET"&&Number(x.discount_pct||0)>=15)priority="normal";
    const prior=hiddenAlertState.get(Number(x.id))||null;
    if(!priority){hiddenAlertState.set(Number(x.id),null);continue}
    const signature=`${x.cache_timestamp||0}:${x.lowest}:${x.qty_floor}:${x.kind}`;
    hiddenAlertState.set(Number(x.id),signature);if(prior===signature)continue;
    const age=cacheAge(x),ageText=age===null?"cache age unknown":`${age}s-old Torn snapshot`;
    const body=x.kind==="NPC FLOOR"?`${money.format(x.lowest)} buy · ${money.format(x.hard_floor)} NPC floor · ${money.format(x.floor_profit||0)} spread · ${ageText}`:`${money.format(x.lowest)} buy · ${Number(x.discount_pct||0).toFixed(1)}% below market · ${ageText}`;
    notify(`${priority==="high"?"HIGH PRIORITY · ":""}${x.name}`,body,x.market_url,priority);
  }
}

async function discoverNow(itemIds=null){
  const targeted=Array.isArray(itemIds)&&itemIds.length>0;
  const suffix=targeted?`?ids=${encodeURIComponent(itemIds.join(","))}`:"";
  const d=await call(`/api/discover${suffix}`,{method:"POST"});
  for(const x of d.items||[]){const changed=recordHiddenSnapshot(x);recordHiddenOutcome(x,changed);hiddenResults.set(Number(x.id),x)}
  renderDiscovery([...hiddenResults.values()]);
  processHiddenAlerts(d.items||[]);
  const updated=$("discoverUpdated");if(updated)updated.textContent=`Last check ${new Date(d.scanned_at*1000).toLocaleTimeString()} · ${hiddenResults.size}/${d.pool_count||discoveryIds.length} items seen`;
  const overall=$("intelligenceUpdated");if(overall)overall.textContent=`Market snapshot ${new Date(d.scanned_at*1000).toLocaleTimeString()}`;
  return d;
}

function researchStageClass(x){return x.sniper_candidate?"priority-hit":x.graduated?"research-hit":x.stage==="PROVEN MARKET"?"research-candidate":x.stage==="BUILDING CASE"?"mild-hit":""}
function renderLiquidity(items,requirements=""){
  const note=$("researchRequirements"),root=$("liquidityRows");
  if(note&&requirements)note.innerHTML=`<strong>Evidence bar:</strong> ${requirements}. Items move through one shared learning/watch pipeline; Sniper promotion remains manual.`;
  if(!root)return;
  if(!items.length){root.innerHTML='<tr><td colspan="12" class="muted">No evidence yet.</td></tr>';return}
  root.innerHTML=items.map(x=>{
    if(x.error)return`<tr><td><strong>${x.name}</strong></td><td colspan="10" class="bad">${x.error}</td><td></td></tr>`;
    const stage=x.sniper_candidate?"SNIPER CANDIDATE":x.stage||"LEARNING";
    const trusted=x.trusted_events==null?x.bargain_events:x.trusted_events;
    const quarantine=x.quarantined_events?` · ${x.quarantined_events} quarantined`:"";
    return`<tr class="${researchStageClass(x)}">
      <td><strong>${x.name}</strong><br><small class="muted">Score ${Number(x.sniper_score??x.promotion_score??0).toFixed(1)}/100 · ${x.data_quality||"learning"}</small></td>
      <td><strong>${stage}</strong></td>
      <td>${x.lowest?money.format(x.lowest):"—"}<br><small class="muted">baseline ${x.rolling_baseline?money.format(x.rolling_baseline):"—"}</small></td>
      <td>${x.observations||0}</td>
      <td><strong>${trusted||0}</strong><br><small class="muted">${x.recovered_events||0} recovered${quarantine}</small></td>
      <td>${x.strong_events||0}</td>
      <td><strong>${Number(x.best_edge_pct??x.best_discount_pct??0).toFixed(1)}%</strong><br><small class="muted">median ${Number(x.median_edge_pct??x.median_discount_pct??0).toFixed(1)}%</small></td>
      <td>${x.activity||"Learning"}<br><small class="muted">${Number(x.activity_score||0).toFixed(0)}/100</small></td>
      <td>${Number(x.listing_churn_rate||0).toFixed(0)}%</td>
      <td>${x.gap_events||0}<br><small class="muted">max ${Number(x.largest_gap_pct||0).toFixed(1)}%</small></td>
      <td>${Number(x.floor_change_rate||0).toFixed(0)}%</td>
      <td><button class="mini-btn" onclick="window.open('${x.market_url}','_blank','noopener')">Open</button></td>
    </tr>`;
  }).join("");
}

async function loadLiquidity(){
  try{
    const [candidateData,researchData]=await Promise.all([
      call("/api/sniper/candidates").catch(()=>({items:[]})),
      call("/api/research/status").catch(()=>({items:[]}))
    ]);
    if(Array.isArray(candidateData.discovery_ids))discoveryIds=candidateData.discovery_ids.map(Number);
    if(Array.isArray(researchData.discovery_ids))discoveryIds=researchData.discovery_ids.map(Number);
    const merged=new Map();
    for(const x of researchData.items||[])merged.set(Number(x.id),x);
    for(const x of candidateData.items||[])merged.set(Number(x.id),{...(merged.get(Number(x.id))||{}),...x});
    const items=[...merged.values()].sort((a,b)=>Number(b.sniper_candidate)-Number(a.sniper_candidate)||Number(b.sniper_score??b.promotion_score??0)-Number(a.sniper_score??a.promotion_score??0));
    renderLiquidity(items,candidateData.requirements||researchData.requirements||"");
    const updated=$("learnUpdated");if(updated)updated.textContent=`Evidence refreshed ${new Date().toLocaleTimeString()} · ${items.length} markets tracked`;
  }catch(e){msg(`Evidence refresh failed: ${e.message}`,true)}
}

async function runBackgroundResearch(){
  if(!intelligenceRunning)return;
  try{
    const d=await call("/api/research/sample",{method:"POST"});
    if(Array.isArray(d.discovery_ids))discoveryIds=d.discovery_ids.map(Number);
    if((d.newly_graduated||[]).length){
      const names=d.newly_graduated.map(x=>x.name).join(", ");
      notify("Market Intelligence promotion",`${names} proved enough evidence to join the active watch pool.`,null,"normal");
    }
    const updated=$("learnUpdated");if(updated)updated.textContent=`Background research ${new Date(d.sampled_at*1000).toLocaleTimeString()} · next in 10m`;
    await loadLiquidity();
  }catch(e){msg(`Background research: ${e.message}`,true)}
}

function hiddenNextDue(id){
  const now=Date.now(),x=hiddenResults.get(Number(id)),s=hiddenStat(id),c=hiddenCacheState.get(Number(id)),p=hiddenPriorityInfo(id,x);
  if(!x||!c)return 0;
  const delay=Math.max(1,Number(x.cache_delay||30))*1000,cacheTs=Number(x.cache_timestamp||0)*1000;
  if(cacheTs&&now<cacheTs+delay)return cacheTs+delay+250;
  const pending=Number(c.requestsSinceChange||0)>0,hardFloor=!!x.hard_floor;
  const pendingIntervals={5:4000,4:5000,3:8000,2:11000,1:15000},normalIntervals={5:6000,4:8000,3:14000,2:22000,1:35000};
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
    if(!x)return id;
    if(now<due&&since<maxWait)continue;
    const overdue=Math.max(0,(now-due)/1000),starve=Math.max(0,(since-(hardFloor?12000:30000))/1000),currentBonus=isStrongHiddenHit(x)?30:isHiddenHit(x)?18:0,floorBonus=hardFloor?28:0;
    const rankScore=p.rank*20+overdue+starve*2+currentBonus+floorBonus;
    if(rankScore>bestScore){bestScore=rankScore;best=id}
  }
  return best;
}
function updateIntelligenceStatus(nextId=null){
  const state=$("intelligenceState"),next=$("intelligenceNext");if(!state||!next)return;
  if(!intelligenceRunning){state.textContent="● Intelligence idle";state.className="status-dot idle";next.textContent="Next check: —";return}
  const hot=discoveryIds.filter(id=>hiddenPriorityInfo(id,hiddenResults.get(Number(id))).label==="HOT").length;
  state.textContent=`● Market Intelligence active · ${hot} hot item${hot===1?"":"s"}`;state.className="status-dot live";
  if(nextId){const x=hiddenResults.get(Number(nextId)),p=hiddenPriorityInfo(nextId,x);next.textContent=`Next priority: ${x?.name||`Item ${nextId}`} · ${p.label}${x?.hard_floor?" · NPC floor protected":""}`}
  else next.textContent="Waiting for the next useful Torn cache window…";
}
async function runIntelligenceScheduler(){
  if(!intelligenceRunning)return;
  if(intelligenceRequestRunning){intelligenceTimer=setTimeout(runIntelligenceScheduler,INTELLIGENCE_TICK_MS);return}
  const id=chooseNextHiddenId();updateIntelligenceStatus(id);
  if(id!==null){
    intelligenceRequestRunning=true;
    try{await discoverNow([id])}catch(e){msg(`Market Intelligence: ${e.message}`,true)}finally{intelligenceRequestRunning=false}
  }
  if(intelligenceRunning)intelligenceTimer=setTimeout(runIntelligenceScheduler,INTELLIGENCE_TICK_MS);
}
function startMarketIntelligence(){
  if(intelligenceRunning)return;
  intelligenceRunning=true;
  localStorage.setItem(INTELLIGENCE_AUTOSTART_KEY,"1");
  const btn=$("intelligenceToggleBtn");if(btn){btn.textContent="Stop Market Intelligence";btn.classList.remove("secondary")}
  const progress=$("intelligenceProgress");if(progress)progress.style.width="100%";
  updateIntelligenceStatus();
  runIntelligenceScheduler();
  intelligenceResearchKickoff=setTimeout(()=>{if(intelligenceRunning)runBackgroundResearch()},20000);
  intelligenceResearchTimer=setInterval(()=>{if(intelligenceRunning)runBackgroundResearch()},BACKGROUND_RESEARCH_MS);
}
function stopMarketIntelligence(){
  intelligenceRunning=false;
  intelligenceRequestRunning=false;
  localStorage.setItem(INTELLIGENCE_AUTOSTART_KEY,"0");
  if(intelligenceTimer)clearTimeout(intelligenceTimer);intelligenceTimer=null;
  if(intelligenceResearchKickoff)clearTimeout(intelligenceResearchKickoff);intelligenceResearchKickoff=null;
  if(intelligenceResearchTimer)clearInterval(intelligenceResearchTimer);intelligenceResearchTimer=null;
  const btn=$("intelligenceToggleBtn");if(btn){btn.textContent="Start Market Intelligence";btn.classList.add("secondary")}
  const progress=$("intelligenceProgress");if(progress)progress.style.width="0%";
  updateIntelligenceStatus();
}
function toggleMarketIntelligence(){if(intelligenceRunning)stopMarketIntelligence();else startMarketIntelligence()}

async function refreshBrokerStatus(){
  try{
    const d=await call("/api/request-broker/status");
    const reused=Number(d.estimated_requests_avoided||0);
    if($("brokerUpstream"))$("brokerUpstream").textContent=Number(d.upstream_requests||0).toLocaleString();
    if($("brokerReused"))$("brokerReused").textContent=reused.toLocaleString();
    if($("brokerReusePct"))$("brokerReusePct").textContent=`${Number(d.reuse_pct||0).toFixed(1)}%`;
  }catch{}
}

window.addEventListener("DOMContentLoaded",async()=>{
  loadHiddenPriorityStats();
  const key=$("key");if(key)key.addEventListener("keydown",e=>{if(e.key==="Enter")loadKey()});
  const sound=$("hiddenSound");if(sound){const saved=localStorage.getItem("torntools.hiddenSound");if(saved!==null)sound.checked=saved==="1";sound.addEventListener("change",()=>localStorage.setItem("torntools.hiddenSound",sound.checked?"1":"0"))}
  setInterval(()=>{if(hiddenResults.size)renderDiscovery([...hiddenResults.values()])},1000);
  brokerTimer=setInterval(refreshBrokerStatus,15000);
  await appStatus();
  if(localStorage.getItem(INTELLIGENCE_AUTOSTART_KEY)==="1")startMarketIntelligence();
});
