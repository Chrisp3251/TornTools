/* Partial Travel Intelligence refresher: update only boxes whose data changed. */
(function(){
function data(){return window.tiLastData||((typeof tiLastData!=='undefined')?tiLastData:null)}
function stable(obj){try{return JSON.stringify(obj)}catch{return ''}}
function tripKey(t){if(!t)return '';return stable({c:t.country,p:t.expected_profit,s:t.spend,ph:t.profit_per_hour,conf:t.confidence_label,age:t.age_seconds,load:(t.load||[]).map(x=>[x.item_id,x.buy,x.stock,x.profit_each,x.confidence_label]),restocks:(t.restocks||[]).map(r=>[r.item_id,r.status,r.estimated_at,r.confidence_label,r.sample_cycles,r.anchor_quality])})}
function travelKey(tr){return stable({state:tr?.state,destination:tr?.destination,destination_code:tr?.destination_code,method:tr?.method,timestamp:tr?.timestamp,departed:tr?.departed,available_in_torn_at:tr?.available_in_torn_at,available_in_torn_exact:tr?.available_in_torn_exact,description:tr?.description})}
function tripsKey(d){return stable((d?.trips||[]).map(t=>tripKey(t)))}
function renderRows(d){const rows=document.getElementById('rows');if(!rows||typeof tiLoad!=='function')return;rows.innerHTML=(d.trips||[]).map((t,i)=>{const current=d.travel?.destination_code===t.country&&(d.travel.state==='FLYING_OUT'||d.travel.state==='ABROAD');return `<tr class="${current?'priority-hit':i===0?'research-hit':''}"><td><strong>#${i+1}</strong></td><td><strong>${tiEsc(t.country_name)}</strong>${current?'<br><small class="good">YOUR DESTINATION</small>':''}<br><small class="muted">${t.filled}/${t.capacity} slots</small></td><td>${tiLoad(t)}</td><td>${typeof tiRestockCell==='function'?tiRestockCell(t):'—'}</td><td>${tiMoney.format(t.spend)}</td><td><strong>${tiMoney.format(t.expected_profit)}</strong></td><td>${tiMoney.format(t.headline_profit)}</td><td>${tiMinutes(t.round_trip_minutes)}</td><td><strong>${tiMoney.format(t.profit_per_hour)}</strong></td><td>${t.confidence_label}<br><small class="muted">${Math.round((Number(t.confidence)||0)*100)}%</small></td></tr>`}).join('')||'<tr><td colspan="10" class="muted">No profitable stocked destinations.</td></tr>'}
let busy=false;
async function partialRefresh(){if(busy)return;busy=true;try{
 const old=data();if(!old)return;
 const cap=Math.max(1,Number(document.getElementById('capacity')?.value)||17),speed=Math.max(.25,Number(document.getElementById('speed')?.value)||1),fee=Math.max(0,Number(document.getElementById('fee')?.value)||5)/100;
 const r=await fetch(`/api/travel-intelligence?capacity=${encodeURIComponent(cap)}&speed=${encodeURIComponent(speed)}&sale_fee=${encodeURIComponent(fee)}`,{cache:'no-store'});const d=await r.json();if(!r.ok)return;
 const oldTravel=travelKey(old.travel),newTravel=travelKey(d.travel),oldBest=tripKey(old.best),newBest=tripKey(d.best),oldDest=tripKey(old.destination_trip),newDest=tripKey(d.destination_trip),oldTrips=tripsKey(old),newTrips=tripsKey(d);
 tiLastData=d;window.tiLastData=d;
 const fresh=document.getElementById('fresh');if(fresh)fresh.textContent=d.generated_at?`Feed ${new Date(d.generated_at).toLocaleTimeString()}`:'Feed loaded';
 if(oldTravel!==newTravel||oldDest!==newDest){if(typeof tiTravelCard==='function')tiTravelCard(d.travel,d.destination_trip)}
 if(oldBest!==newBest||oldTravel!==newTravel||oldDest!==newDest){if(typeof tiBest==='function')tiBest(d.best,d.travel,d.destination_trip)}
 if(oldTrips!==newTrips)renderRows(d);
 if(typeof window.tiRenderFuturePlanner==='function'&&(oldTrips!==newTrips||oldTravel!==newTravel))window.tiRenderFuturePlanner();
 if(typeof tiTickCountdown==='function')tiTickCountdown();
}catch(e){}finally{busy=false}}
window.addEventListener('DOMContentLoaded',()=>{
 setTimeout(()=>{try{if(typeof tiTimer!=='undefined'&&tiTimer){clearInterval(tiTimer);tiTimer=null}}catch(e){}},500);
 setInterval(partialRefresh,15000);
});
})();
