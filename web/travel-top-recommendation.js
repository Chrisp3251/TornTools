/* Travel Intelligence: clearer top recommendation / action card */
(function(){
  function cashNeeded(t){
    if(Number.isFinite(Number(t?.spend)))return Number(t.spend);
    return (Array.isArray(t?.load)?t.load:[]).reduce((sum,x)=>sum+(Number(x.buy)||0)*(Number(x.cost)||0),0);
  }
  function topLoadMarkup(t){
    const load=Array.isArray(t?.load)?t.load:[];
    if(!load.length)return '<div class="top-pick-empty">No currently stocked profitable item is recommended.</div>';
    return `<div class="top-pick-items">${load.map((x,i)=>`<div class="top-pick-item ${i===0?'primary':''}"><span class="top-pick-rank">${i===0?'TOP PICK':`#${i+1}`}</span><div><strong>${tiEsc(x.name)}</strong><small>${tiNum.format(x.stock)} in stock · ${tiMoney.format(x.profit_each)} profit each · ${tiMoney.format((Number(x.buy)||0)*(Number(x.cost)||0))} needed</small></div><b>BUY ${tiNum.format(x.buy)}</b></div>`).join('')}</div>`;
  }
  function clock(ts){return Number(ts)>0?new Date(Number(ts)*1000).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'}):'—'}
  function durMinutes(mins){const m=Math.max(0,Math.round(Number(mins)||0));return m<60?`${m}m`:`${Math.floor(m/60)}h ${m%60}m`}
  function timeline(t,travel){
    if(!t)return '';
    const now=Date.now()/1000,state=travel?.state||'IN_TORN',oneWay=(Number(t.one_way_minutes)||0)*60;
    let backInTorn=null,depart=null,arrive=null,backAfterTrip=null,status='',source='';
    if(state==='IN_TORN'){
      backInTorn=now;depart=now;arrive=depart+oneWay;backAfterTrip=arrive+oneWay;status='LEAVE TORN NOW';source='You are currently in Torn.';
    }else if(state==='FLYING_HOME'){
      backInTorn=Number(travel.available_in_torn_at||travel.timestamp||0)||null;depart=backInTorn;arrive=depart?depart+oneWay:null;backAfterTrip=arrive?arrive+oneWay:null;
      status=depart?`LEAVE TORN AFTER ${clock(depart)}`:'WAIT FOR RETURN LANDING';
      source=travel.available_in_torn_exact?'Your exact current return-flight landing time is included.':'Your projected return-to-Torn time is included.';
    }else if(state==='FLYING_OUT'){
      const currentArrival=Number(travel.timestamp||0)||null;backInTorn=Number(travel.available_in_torn_at||0)||null;
      status=currentArrival?`CURRENT TRIP · LAND ${clock(currentArrival)}`:'CURRENT TRIP · ALREADY EN ROUTE';
      source=backInTorn?`Projected back in Torn ${clock(backInTorn)}; your next outbound recommendation begins after that.`:'This is your current destination load, not a new departure from Torn.';
    }else if(state==='ABROAD'){
      backInTorn=Number(travel.available_in_torn_at||0)||null;status='BUY / RETURN TO TORN FIRST';
      source=backInTorn?`Projected back in Torn ${clock(backInTorn)}; next-trip departures start after that.`:'You are abroad, so no outbound departure from Torn is possible yet.';
    }
    const mode=t.travel_mode_label||travel?.method||'selected travel method';
    return `<div class="best-trip-plan"><div class="best-plan-head"><span>TRIP TIMING</span><strong>${tiEsc(status)}</strong><small>${tiEsc(source)}</small></div><div class="best-plan-grid">${state==='FLYING_HOME'?`<div><span>Back in Torn</span><strong>${clock(backInTorn)}</strong></div>`:''}${state==='IN_TORN'||state==='FLYING_HOME'?`<div><span>Depart Torn</span><strong>${clock(depart)}</strong></div><div><span>Arrive ${tiEsc(t.country_name)}</span><strong>${clock(arrive)}</strong></div><div><span>Back in Torn again</span><strong>${clock(backAfterTrip)}</strong></div>`:`<div><span>Current state</span><strong>${tiEsc(state.replaceAll('_',' '))}</strong></div><div><span>Back in Torn</span><strong>${clock(backInTorn)}</strong></div>`}<div><span>Cash needed</span><strong>${tiMoney.format(cashNeeded(t))}</strong></div><div><span>Travel method</span><strong>${tiEsc(mode)}</strong></div><div><span>One way</span><strong>${durMinutes(t.one_way_minutes)}</strong></div></div></div>`;
  }
  function actionBanner(t,travel){
    if(!t)return '';
    const state=travel?.state||'IN_TORN';
    const first=t.load?.[0];
    const verb=state==='ABROAD'?'BUY NOW':state==='FLYING_OUT'?'BUY WHEN YOU LAND':state==='FLYING_HOME'?'NEXT TRIP AFTER RETURN':'RECOMMENDED TRIP';
    const item=first?`${tiNum.format(first.buy)}× ${tiEsc(first.name)}`:'No stocked item';
    return `<div class="top-action-banner"><div><span>${verb}</span><strong>${item}</strong><small>${tiEsc(t.country_name)} · Bring ${tiMoney.format(cashNeeded(t))} · ${tiMoney.format(t.expected_profit)} adjusted load profit · ${tiMoney.format(t.profit_per_hour)}/hr</small></div><div class="top-action-go">GO</div></div>`;
  }
  const oldTravel=window.tiTravelCard;
  window.tiTravelCard=function(travel,destinationTrip){
    oldTravel(travel,destinationTrip);
    const root=document.getElementById('travelState');
    if(!root||!travel?.available||!destinationTrip)return;
    if(travel.state==='FLYING_OUT'||travel.state==='ABROAD'){
      const banner=document.createElement('div');
      banner.className='live-item-focus';
      banner.innerHTML=`<span>${travel.state==='ABROAD'?'CURRENT BUY TARGET':'LANDING BUY TARGET'}</span>${topLoadMarkup(destinationTrip)}`;
      const head=root.querySelector('.summary-grid');
      if(head)head.insertAdjacentElement('afterend',banner);
    }
  };
  const oldBest=window.tiBest;
  window.tiBest=function(t,travel,destinationTrip){
    oldBest(t,travel,destinationTrip);
    let chosen=t;
    if(travel?.available&&(travel.state==='FLYING_OUT'||travel.state==='ABROAD')&&destinationTrip)chosen=destinationTrip;
    const root=document.getElementById('best');
    if(!root||!chosen)return;
    const head=root.querySelector('.section-head');
    if(head){
      const wrapper=document.createElement('div');wrapper.innerHTML=actionBanner(chosen,travel);head.insertAdjacentElement('afterend',wrapper.firstElementChild);
      const action=root.querySelector('.top-action-banner');if(action)action.insertAdjacentHTML('afterend',timeline(chosen,travel));
    }
    const existing=[...root.querySelectorAll('.research-note')].find(x=>x.textContent.includes('BUY PRIORITY'));
    if(existing){existing.classList.add('top-buy-priority');existing.innerHTML=`<div class="top-buy-label">WHAT TO BUY</div>${topLoadMarkup(chosen)}`;}
  };
  const css=document.createElement('style');css.textContent=`.best-trip-plan{margin:0 0 14px;padding:12px 14px;border:1px solid rgba(117,183,255,.32);border-radius:10px;background:rgba(117,183,255,.045)}.best-plan-head span,.best-plan-head strong,.best-plan-head small{display:block}.best-plan-head span{font-size:9px;font-weight:900;letter-spacing:.11em;color:#75b7ff}.best-plan-head strong{font-size:18px;margin-top:3px}.best-plan-head small{font-size:10px;color:var(--muted,#9ba1ad);margin-top:3px}.best-plan-grid{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px;margin-top:10px}.best-plan-grid>div{padding:8px 9px;border:1px solid var(--border,#343943);border-radius:8px;background:rgba(0,0,0,.12)}.best-plan-grid span,.best-plan-grid strong{display:block}.best-plan-grid span{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted,#9ba1ad)}.best-plan-grid strong{font-size:13px;margin-top:3px}@media(max-width:900px){.best-plan-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}`;
  document.head.appendChild(css);
  const ui=document.createElement('script');ui.src='/static/travel-ui-v2.js?v=4';document.head.appendChild(ui);
})();
