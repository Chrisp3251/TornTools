/* Flight-aware live buy target: actual stock overrides predictions. */
(function(){
let previousKey='', lastPoll=0;
const esc=s=>typeof tiEsc==='function'?tiEsc(s):String(s??'');
const money=n=>typeof tiMoney!=='undefined'?tiMoney.format(Number(n)||0):`$${Math.round(Number(n)||0).toLocaleString()}`;
function loadKey(t){return (t?.load||[]).map(x=>`${x.item_id}:${x.buy}:${x.stock}`).join('|')}
function predicted(t,travel){
 const arrival=Number(travel?.timestamp||0)||Date.now()/1000;
 return (t?.restocks||[]).filter(r=>r.estimated_at&&['MEDIUM','HIGH'].includes(String(r.confidence_label||'').toUpperCase())).map(r=>{
   const est=Number(r.estimated_at), delta=est-arrival;
   return {...r,delta};
 }).filter(r=>r.delta<=0 || r.delta<=15*60).sort((a,b)=>(Number(b.profit_each)||0)-(Number(a.profit_each)||0))[0]||null;
}
function ensure(){
 const state=document.getElementById('travelState');if(!state)return null;
 let box=document.getElementById('liveLandingTarget');
 if(!box){box=document.createElement('div');box.id='liveLandingTarget';box.className='live-target';state.appendChild(box)}return box;
}
function render(d){
 const tr=d?.travel||{},t=d?.destination_trip,box=ensure();if(!box)return;
 if(!tr.available||!['FLYING_OUT','ABROAD'].includes(tr.state)||!t){box.hidden=true;return}box.hidden=false;
 const actual=t.load?.[0], forecast=predicted(t,tr), key=loadKey(t), changed=previousKey&&key!==previousKey;previousKey=key;
 const actualHtml=actual?`<div class="live-target-main"><span>${tr.state==='ABROAD'?'BUY NOW · LIVE STOCK':'LANDING TARGET · LIVE STOCK'}</span><strong>${t.load.map(x=>`${x.buy}× ${esc(x.name)}`).join(' + ')}</strong><small>${money(t.expected_profit)} expected load profit · feed ${Math.max(0,Math.round(Number(t.age_seconds)||0))}s old</small></div>`:'<div class="live-target-main"><span>LIVE STOCK</span><strong>No profitable stocked load right now</strong></div>';
 let forecastHtml='';
 if(forecast){const when=forecast.delta<0?`${Math.max(1,Math.round(Math.abs(forecast.delta)/60))}m before landing`:`${Math.max(1,Math.round(forecast.delta/60))}m after landing`;const beats=actual&&(Number(forecast.profit_each)||0)>(Number(actual.profit_each)||0);forecastHtml=`<div class="live-target-forecast ${beats?'upgrade':''}"><span>${beats?'★ EXPECTED UPGRADE':'RESTOCK WATCH'}</span><strong>${esc(forecast.name)}</strong><small>${money(forecast.profit_each)}/ea · est. ${when} · ${esc(forecast.confidence_label)} confidence${beats?' · better than current top item if it appears':''}</small></div>`}
 box.innerHTML=`${changed?'<div class="live-change">🔥 LIVE STOCK CHANGED — recommendation recalculated</div>':''}<div class="live-target-grid">${actualHtml}${forecastHtml}</div><div class="live-target-foot"><i></i> Auto-checking destination stock every 10s · observed stock always overrides prediction</div>`;
}
async function poll(){
 const d=window.tiLastData||((typeof tiLastData!=='undefined')?tiLastData:null),tr=d?.travel||{};
 if(!tr.available||!['FLYING_OUT','ABROAD'].includes(tr.state))return;
 if(Date.now()-lastPoll<9000)return;lastPoll=Date.now();
 try{const cap=Math.max(1,Number(document.getElementById('capacity')?.value)||17),speed=Math.max(.25,Number(document.getElementById('speed')?.value)||1),fee=Math.max(0,Number(document.getElementById('fee')?.value)||5)/100;const r=await fetch(`/api/travel-intelligence?capacity=${encodeURIComponent(cap)}&speed=${encodeURIComponent(speed)}&sale_fee=${encodeURIComponent(fee)}&live=1`,{cache:'no-store'});const n=await r.json();if(!r.ok)return;tiLastData=n;window.tiLastData=n;if(typeof tiTravelCard==='function')tiTravelCard(n.travel,n.destination_trip);if(typeof tiBest==='function')tiBest(n.best,n.travel,n.destination_trip);setTimeout(()=>render(n),0)}catch(e){}
}
const css=document.createElement('style');css.textContent=`.live-target{margin-top:12px;border:1px solid rgba(85,201,134,.38);border-radius:11px;padding:11px 12px;background:linear-gradient(135deg,rgba(85,201,134,.08),rgba(117,183,255,.035))}.live-target-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:9px}.live-target-main,.live-target-forecast{padding:9px 10px;border-radius:8px;background:rgba(0,0,0,.14);border:1px solid var(--border,#343943)}.live-target-main span,.live-target-forecast span{display:block;font-size:9px;letter-spacing:.1em;font-weight:900;color:#70d89a}.live-target-main strong,.live-target-forecast strong{display:block;font-size:16px;margin-top:3px}.live-target-main small,.live-target-forecast small{display:block;color:var(--muted,#9ba1ad);font-size:10px;margin-top:3px}.live-target-forecast.upgrade{border-color:rgba(244,199,109,.5);background:rgba(244,199,109,.06)}.live-target-forecast.upgrade span{color:#f4c76d}.live-change{font-size:11px;font-weight:900;color:#f4c76d;margin-bottom:8px}.live-target-foot{font-size:9px;color:var(--muted,#9ba1ad);margin-top:8px}.live-target-foot i{display:inline-block;width:6px;height:6px;border-radius:50%;background:#55c986;margin-right:5px;box-shadow:0 0 0 3px rgba(85,201,134,.12)}`;document.head.appendChild(css);
setInterval(()=>{const d=window.tiLastData||((typeof tiLastData!=='undefined')?tiLastData:null);if(d)render(d);poll()},10000);setTimeout(()=>{const d=window.tiLastData||((typeof tiLastData!=='undefined')?tiLastData:null);if(d)render(d);poll()},1200);
})();
