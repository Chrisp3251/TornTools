const bwMoney = new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:0});
let bwRunning=false;
let bwTimer=null;
let bwConfig=null;
const BW_POLL_MS=31000;
const $bw=id=>document.getElementById(id);

async function bwCall(path,opts={}){
  const r=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});
  let d={};try{d=await r.json()}catch{}
  if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);
  return d;
}
function bwMsg(text,bad=false){const el=$bw('message');if(!el)return;el.textContent=text;el.className=bad?'bad':'good'}
function bwBazaarUrl(){return bwConfig&&bwConfig.player_id?`https://www.torn.com/bazaar.php?userId=${bwConfig.player_id}#/`:null}

function bwSetRunning(running){
  bwRunning=!!running;
  localStorage.setItem('torntools.bazaarWatch.running',bwRunning?'1':'0');
  const btn=$bw('toggleBtn'),state=$bw('watchState');
  if(btn){btn.textContent=bwRunning?'Stop Monitoring':'Start Monitoring';btn.classList.toggle('secondary',!bwRunning)}
  if(state){state.textContent=bwRunning?'● Monitoring':'Idle';state.className=bwRunning?'pill good':'pill'}
}

async function bwLoadConfig(){
  try{
    const d=await bwCall('/api/bazaar-watch/config');
    bwConfig=d;
    if(d.player_id)$bw('playerId').value=d.player_id;
    if(d.min_value)$bw('minValue').value=d.min_value;
    const open=$bw('openBtn');if(open)open.disabled=!d.player_id;
    return d;
  }catch(e){bwMsg(`Could not load Bazaar Watch: ${e.message}`,true);return null}
}

async function bwSaveConfig(enabled=true,quiet=false){
  const playerId=Math.trunc(Number($bw('playerId').value||0));
  const minValue=Math.trunc(Number($bw('minValue').value||0));
  if(!playerId||minValue<=0)throw new Error('Enter a valid player ID and minimum value.');
  const d=await bwCall('/api/bazaar-watch/config',{method:'POST',body:JSON.stringify({player_id:playerId,min_value:minValue,enabled})});
  bwConfig=d;
  $bw('openBtn').disabled=false;
  if(!quiet)bwMsg(`Watching player #${playerId} for listings worth ${bwMoney.format(minValue)} or more.`);
  return d;
}

function bwNotify(event){
  const body=`${event.name} · ${event.quantity}x · ask ${bwMoney.format(event.price||0)} · market ${event.market_price?bwMoney.format(event.market_price):'—'}`;
  try{
    if('Notification' in window&&Notification.permission==='granted'){
      const n=new Notification(`Bazaar Watch · ${event.event_type}`,{body,tag:`bazaar-${event.player_id}-${event.listing_key}`,renotify:true});
      n.onclick=()=>{window.focus();window.open(event.bazaar_url||bwBazaarUrl(),'_blank','noopener');n.close()};
      setTimeout(()=>n.close(),20000);
    }
  }catch{}
}

function bwRenderCurrent(items){
  const root=$bw('currentRows');if(!root)return;
  if(!items||!items.length){
    const threshold=Number(bwConfig?.min_value||$bw('minValue')?.value||0);
    root.innerHTML=`<tr><td colspan="7" class="muted">Bazaar listings were found, but none currently meet the active ${bwMoney.format(threshold)} threshold.</td></tr>`;
    return;
  }
  root.innerHTML=items.map(x=>`<tr>
    <td><strong>${x.name}</strong><br><small class="muted">Item #${x.item_id||'—'}${x.uid?` · UID ${x.uid}`:''}</small></td>
    <td>${x.quantity}</td>
    <td><strong>${bwMoney.format(x.price||0)}</strong></td>
    <td>${x.market_price?bwMoney.format(x.market_price):'—'}</td>
    <td>${bwMoney.format(x.ask_stack_value||0)}</td>
    <td>${x.market_stack_value?bwMoney.format(x.market_stack_value):'—'}</td>
    <td>${x.reason||'value threshold'}</td>
  </tr>`).join('');
}

function bwRenderEvents(items){
  const root=$bw('eventRows');if(!root)return;
  if(!items||!items.length){root.innerHTML='<tr><td colspan="8" class="muted">No qualifying changes recorded yet.</td></tr>';return}
  root.innerHTML=items.map(x=>`<tr>
    <td>${new Date(Number(x.ts||0)*1000).toLocaleString()}</td>
    <td><strong>${x.event_type}</strong></td>
    <td><strong>${x.name}</strong><br><small class="muted">${x.reason||''}</small></td>
    <td>${x.quantity}${x.prior_quantity!=null?`<br><small class="muted">was ${x.prior_quantity}</small>`:''}</td>
    <td>${bwMoney.format(x.price||0)}${x.prior_price!=null&&x.prior_price!==x.price?`<br><small class="muted">was ${bwMoney.format(x.prior_price)}</small>`:''}</td>
    <td>${x.market_price?bwMoney.format(x.market_price):'—'}</td>
    <td><strong>${bwMoney.format(x.estimated_value||0)}</strong></td>
    <td><button class="mini-btn" onclick="window.open('${x.bazaar_url}','_blank','noopener')">Open Bazaar</button></td>
  </tr>`).join('');
}

async function bwLoadEvents(){
  try{const d=await bwCall('/api/bazaar-watch/events?limit=75');bwRenderEvents(d.items||[])}catch(e){bwMsg(`Could not load event history: ${e.message}`,true)}
}

async function bwCheckNow(){
  const btn=$bw('checkBtn');if(btn){btn.disabled=true;btn.textContent='Checking…'}
  try{
    // Always persist the values currently visible in the form before checking.
    // Previously a changed threshold was ignored after the first saved player.
    await bwSaveConfig(bwRunning,true);
    const d=await bwCall('/api/bazaar-watch/check',{method:'POST'});
    $bw('lastChecked').textContent=`Checked ${new Date(d.checked_at*1000).toLocaleTimeString()} · threshold ${bwMoney.format(d.min_value||0)}${d.bazaar_timestamp?` · Torn snapshot ${new Date(d.bazaar_timestamp*1000).toLocaleTimeString()}`:''}`;
    $bw('bazaarOpen').textContent=d.bazaar_is_open===true?'OPEN':d.bazaar_is_open===false?'CLOSED':'UNKNOWN';
    $bw('bazaarCount').textContent=`${d.listing_count||0} listing${Number(d.listing_count||0)===1?'':'s'}`;
    $bw('expensiveCount').textContent=Number(d.expensive_count||0).toLocaleString();
    $bw('newEventCount').textContent=Number(d.event_count||0).toLocaleString();
    bwRenderCurrent(d.expensive_items||[]);
    if(d.first_baseline)bwMsg(`Baseline saved at ${bwMoney.format(d.min_value||0)}. Existing listings will not trigger alerts; future additions/changes will.`);
    else if(d.event_count)bwMsg(`${d.event_count} qualifying bazaar change${d.event_count===1?'':'s'} detected at the ${bwMoney.format(d.min_value||0)} threshold.`);
    else bwMsg(`No new qualifying bazaar changes. Active threshold: ${bwMoney.format(d.min_value||0)}.`);
    for(const e of d.events||[])bwNotify(e);
    if(d.event_count)await bwLoadEvents();
    return d;
  }catch(e){bwMsg(`Bazaar check failed: ${e.message}`,true);throw e}
  finally{if(btn){btn.disabled=false;btn.textContent='Check Now'}}
}

async function bwStart(){
  try{
    await bwSaveConfig(true,true);
    if('Notification' in window&&Notification.permission==='default'){
      try{await Notification.requestPermission()}catch{}
    }
    bwSetRunning(true);
    await bwCheckNow();
    if(bwTimer)clearInterval(bwTimer);
    bwTimer=setInterval(()=>{if(bwRunning)bwCheckNow().catch(()=>{})},BW_POLL_MS);
  }catch(e){bwSetRunning(false);bwMsg(e.message,true)}
}
function bwStop(){
  if(bwTimer)clearInterval(bwTimer);bwTimer=null;
  bwSetRunning(false);
  bwMsg('Bazaar monitoring stopped.');
}
function bwToggle(){if(bwRunning)bwStop();else bwStart()}

window.addEventListener('DOMContentLoaded',async()=>{
  $bw('saveBtn').addEventListener('click',()=>bwSaveConfig(bwRunning).catch(e=>bwMsg(e.message,true)));
  $bw('toggleBtn').addEventListener('click',bwToggle);
  $bw('checkBtn').addEventListener('click',()=>bwCheckNow().catch(()=>{}));
  $bw('openBtn').addEventListener('click',()=>{const u=bwBazaarUrl();if(u)window.open(u,'_blank','noopener')});
  $bw('refreshEventsBtn').addEventListener('click',bwLoadEvents);
  await bwLoadConfig();
  await bwLoadEvents();
  if(localStorage.getItem('torntools.bazaarWatch.running')==='1'&&bwConfig&&bwConfig.player_id)bwStart();
});
