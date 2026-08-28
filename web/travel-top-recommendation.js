/* Travel Intelligence: clearer top recommendation / action card */
(function(){
  function topLoadMarkup(t){
    const load=Array.isArray(t?.load)?t.load:[];
    if(!load.length)return '<div class="top-pick-empty">No currently stocked profitable item is recommended.</div>';
    return `<div class="top-pick-items">${load.map((x,i)=>`<div class="top-pick-item ${i===0?'primary':''}"><span class="top-pick-rank">${i===0?'TOP PICK':`#${i+1}`}</span><div><strong>${tiEsc(x.name)}</strong><small>${tiNum.format(x.stock)} in stock · ${tiMoney.format(x.profit_each)} profit each</small></div><b>BUY ${tiNum.format(x.buy)}</b></div>`).join('')}</div>`;
  }
  function actionBanner(t,travel){
    if(!t)return '';
    const state=travel?.state||'IN_TORN';
    const first=t.load?.[0];
    const verb=state==='ABROAD'?'BUY NOW':state==='FLYING_OUT'?'BUY WHEN YOU LAND':'RECOMMENDED TRIP';
    const item=first?`${tiNum.format(first.buy)}× ${tiEsc(first.name)}`:'No stocked item';
    return `<div class="top-action-banner"><div><span>${verb}</span><strong>${item}</strong><small>${tiEsc(t.country_name)} · ${tiMoney.format(t.expected_profit)} adjusted load profit · ${tiMoney.format(t.profit_per_hour)}/hr</small></div><div class="top-action-go">GO</div></div>`;
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
    const banner=document.createElement('div');
    banner.innerHTML=actionBanner(chosen,travel);
    const head=root.querySelector('.section-head');
    if(head)head.insertAdjacentElement('afterend',banner.firstElementChild);
    const existing=[...root.querySelectorAll('.research-note')].find(x=>x.textContent.includes('BUY PRIORITY'));
    if(existing){existing.classList.add('top-buy-priority');existing.innerHTML=`<div class="top-buy-label">WHAT TO BUY</div>${topLoadMarkup(chosen)}`;}
  };
})();
