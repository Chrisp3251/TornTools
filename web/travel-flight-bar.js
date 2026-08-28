/* Live airplane progress bar for Travel Intelligence. */
(function(){
function esc(s){return typeof tiEsc==='function'?tiEsc(s):String(s??'')}
function secs(n){return typeof tiSeconds==='function'?tiSeconds(n):`${Math.max(0,Math.round(n||0))}s`}
function clock(ts){return Number(ts)>0?new Date(Number(ts)*1000).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'}):'—'}
function stateData(tr){
  const now=Date.now()/1000,state=tr?.state||'IN_TORN',landing=Number(tr?.timestamp||0),departed=Number(tr?.departed||0);
  const flying=(state==='FLYING_OUT'||state==='FLYING_HOME')&&landing>now;
  let pct=0,total=0,left=Math.max(0,landing-now);
  if(flying&&departed>0&&landing>departed){total=landing-departed;pct=Math.max(0,Math.min(100,((now-departed)/total)*100));}
  else if(flying&&Number(tr?.time_left)>0){left=Number(tr.time_left);pct=8;}
  return {state,flying,pct,left,landing,departed};
}
function render(){
  const root=document.getElementById('flightProgress');if(!root)return;
  const data=window.tiLastData||((typeof tiLastData!=='undefined')?tiLastData:null),tr=data?.travel||{},s=stateData(tr);
  if(!tr.available){root.innerHTML='<div class="flightbar-off">Flight status unavailable</div>';return;}
  if(!s.flying){
    const label=s.state==='ABROAD'?(tr.destination||'Abroad'):'Torn';
    root.innerHTML=`<div class="flightbar-idle"><span>✈ LIVE FLIGHT STATUS</span><strong>${esc(label)}</strong><small>${s.state==='ABROAD'?'Landed abroad':'Ready in Torn'}</small></div>`;return;
  }
  const home=s.state==='FLYING_HOME',origin=home?(tr.destination||'Abroad'):'Torn',dest=home?'Torn':(tr.destination||'Destination');
  root.innerHTML=`<div class="flightbar-head"><div><span>✈ LIVE FLIGHT STATUS</span><strong>${home?'RETURNING TO TORN':'FLYING TO '+esc(dest).toUpperCase()}</strong></div><div class="flightbar-eta"><span>${secs(s.left)} remaining</span><strong>LAND ${clock(s.landing)}</strong></div></div><div class="flightbar-route"><div class="flightbar-place"><b>${esc(origin)}</b><small>DEPARTED</small></div><div class="flightbar-track"><div class="flightbar-fill" style="width:${s.pct.toFixed(2)}%"></div><div class="flightbar-plane" style="left:${s.pct.toFixed(2)}%">✈</div></div><div class="flightbar-place right"><b>${esc(dest)}</b><small>LANDING</small></div></div><div class="flightbar-foot"><span>${Math.round(s.pct)}% complete</span><span>${tr.method?esc(tr.method):'Flight'}</span><span>${s.departed?'Departed '+clock(s.departed):''}</span></div>`;
}
const css=document.createElement('style');css.textContent=`#flightProgress{margin:0 0 12px}.flightbar-idle,.flightbar-off{padding:10px 13px;border:1px solid var(--border,#343943);border-radius:10px;background:rgba(255,255,255,.025)}.flightbar-idle span,.flightbar-idle strong,.flightbar-idle small{display:block}.flightbar-idle span,.flightbar-head span{font-size:9px;font-weight:900;letter-spacing:.11em;color:#75b7ff}.flightbar-idle strong{font-size:15px;margin-top:2px}.flightbar-idle small{color:var(--muted,#9ba1ad);font-size:10px}.flightbar-head{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;margin-bottom:9px}.flightbar-head strong,.flightbar-eta span,.flightbar-eta strong{display:block}.flightbar-head strong{font-size:14px;margin-top:2px}.flightbar-eta{text-align:right}.flightbar-eta span{font-size:12px;font-weight:800}.flightbar-eta strong{font-size:9px;color:var(--muted,#9ba1ad);margin-top:2px}.flightbar-route{display:grid;grid-template-columns:minmax(70px,auto) 1fr minmax(70px,auto);align-items:center;gap:10px}.flightbar-place b,.flightbar-place small{display:block}.flightbar-place b{font-size:12px}.flightbar-place small{font-size:8px;letter-spacing:.08em;color:var(--muted,#9ba1ad)}.flightbar-place.right{text-align:right}.flightbar-track{position:relative;height:9px;border-radius:999px;background:rgba(255,255,255,.08);border:1px solid rgba(117,183,255,.2);box-shadow:inset 0 1px 3px rgba(0,0,0,.3)}.flightbar-fill{position:absolute;left:0;top:0;bottom:0;border-radius:999px;background:linear-gradient(90deg,#4f94d4,#70d89a);transition:width 1s linear}.flightbar-plane{position:absolute;top:50%;font-size:20px;line-height:1;transform:translate(-50%,-53%);filter:drop-shadow(0 2px 3px rgba(0,0,0,.5));transition:left 1s linear}.flightbar-foot{display:flex;justify-content:space-between;gap:8px;margin-top:7px;font-size:9px;color:var(--muted,#9ba1ad)}@media(max-width:560px){.flightbar-route{grid-template-columns:58px 1fr 58px;gap:6px}.flightbar-head{align-items:flex-start}.flightbar-plane{font-size:18px}.flightbar-foot span:last-child{display:none}}`;document.head.appendChild(css);
setInterval(render,1000);setTimeout(render,100);
})();
