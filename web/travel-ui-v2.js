/* Travel Intelligence UI v2: discrete Torn travel modes, collapsible options, and explicit departure timing. */
(function(){
const MODE_OPTIONS=[
  ['0.5','Standard','Regular paid travel'],
  ['1','Airstrip','Private Island + pilot'],
  ['2','WLT Private','Wind Lines Travel private jet'],
  ['3','Business Class','Business Class Ticket / eligible job perk']
];
function esc(s){return typeof tiEsc==='function'?tiEsc(s):String(s??'')}
function clock(ts){return Number(ts)>0?new Date(Number(ts)*1000).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'}):'—'}
function duration(sec){sec=Math.max(0,Number(sec)||0);const m=Math.round(sec/60);return m<60?`${m}m`:`${Math.floor(m/60)}h ${m%60}m`}
function data(){return window.tiLastData||((typeof tiLastData!=='undefined')?tiLastData:null)}
function modeFromMethod(method){const s=String(method||'').toLowerCase();if(s.includes('business'))return '3';if(s.includes('private')||s.includes('wlt'))return '2';if(s.includes('airstrip'))return '1';if(s.includes('standard'))return '0.5';return null}
function installOptions(){
  const old=document.getElementById('speed');if(!old||old.tagName==='SELECT')return;
  const select=document.createElement('select');select.id='speed';select.className=old.className||'';
  for(const [value,label,detail] of MODE_OPTIONS){const o=document.createElement('option');o.value=value;o.textContent=`${label} — ${detail}`;select.appendChild(o)}
  const savedMode=localStorage.getItem('torntools.travel.mode');
  const inferred=modeFromMethod(data()?.travel?.method);
  select.value=MODE_OPTIONS.some(x=>x[0]===savedMode)?savedMode:(inferred||'1');
  old.replaceWith(select);
  select.closest('label')?.childNodes?.forEach?.(()=>{});
  const label=select.closest('label');if(label){for(const n of [...label.childNodes]){if(n.nodeType===3&&n.textContent.trim()){n.textContent='Travel method';break}}}
  select.addEventListener('change',()=>{localStorage.setItem('torntools.travel.mode',select.value);localStorage.setItem('torntools.travel.speed',select.value);document.getElementById('refresh')?.click()});

  const heading=[...document.querySelectorAll('h2')].find(h=>h.textContent.trim()==='Your trip profile');
  const panel=heading?.closest('section.panel');if(!panel)return;
  heading.textContent='Travel options';
  const sub=heading.parentElement?.querySelector('.muted');if(sub)sub.textContent='Set your carry capacity, actual Torn travel method, and expected market fee.';
  panel.id='travelOptionsPanel';panel.hidden=true;
  const top=document.querySelector('.topbar');if(top&&!document.getElementById('travelOptionsButton')){
    const controls=document.createElement('div');controls.className='ti-top-controls';
    const btn=document.createElement('button');btn.id='travelOptionsButton';btn.className='secondary';btn.type='button';btn.textContent='Options';
    btn.addEventListener('click',()=>{panel.hidden=!panel.hidden;btn.classList.toggle('active',!panel.hidden);btn.textContent=panel.hidden?'Options':'Close Options'});
    const home=[...top.querySelectorAll('button')].find(b=>b.textContent.trim()==='Home');if(home){home.parentNode?.insertBefore(controls,home);controls.append(btn,home)}else{controls.append(btn);top.appendChild(controls)}
  }
  const note=panel.querySelector('.toolbar .muted');if(note)note.textContent='Travel times now use Torn’s actual Standard, Airstrip, WLT Private, or Business Class times — no manual speed multiplier.';
}
function chosenTrip(d){const tr=d?.travel||{};if(['FLYING_OUT','ABROAD'].includes(tr.state)&&d?.destination_trip)return d.destination_trip;return d?.best||null}
function timingMarkup(d){
  const tr=d?.travel||{},trip=chosenTrip(d);if(!trip)return '';
  const now=Date.now()/1000,flight=(Number(trip.one_way_minutes)||0)*60;
  let title='',main='',detail='',depart=null,arrive=null,cls='';
  if(tr.state==='FLYING_HOME'){
    depart=Number(tr.available_in_torn_at||tr.timestamp||0);arrive=depart?depart+flight:null;
    title=tr.available_in_torn_exact?'NEXT DEPARTURE · EXACT RETURN ACCOUNTED FOR':'NEXT DEPARTURE · RETURN ESTIMATE';
    main=depart?`LEAVE TORN AFTER ${clock(depart)}`:'WAITING FOR RETURN TIME';
    detail=depart?`Land in Torn ${clock(depart)} → ${esc(trip.country_name)} arrival about ${clock(arrive)} · ${duration(flight)} one way.`:'TornTools is waiting for your return landing timestamp.';
    cls='return';
  }else if(tr.state==='FLYING_OUT'){
    arrive=Number(tr.timestamp||0);const back=Number(tr.available_in_torn_at||0);
    title='CURRENT TRIP TIMING';main=arrive?`ARRIVE ${esc(trip.country_name)} AT ${clock(arrive)}`:'ALREADY EN ROUTE';
    detail=`You already left Torn.${back?` Earliest projected back in Torn: ${clock(back)}.`:''} This card is for what to buy when you land.`;cls='flight';
  }else if(tr.state==='ABROAD'){
    const back=Number(tr.available_in_torn_at||0);title='CURRENT TRIP TIMING';main='BUY / RETURN TO TORN FIRST';detail=back?`Earliest projected back in Torn: ${clock(back)}. Future departures are calculated after that.`:'You are abroad; next outbound timing starts after your return to Torn.';cls='abroad';
  }else{
    depart=now;arrive=now+flight;title='DEPARTURE PLAN';main='LEAVE TORN NOW';detail=`${esc(trip.country_name)} arrival about ${clock(arrive)} · ${duration(flight)} one way using ${esc(trip.travel_mode_label||'selected travel method')}.`;cls='now';
  }
  return `<div id="bestDeparturePlan" class="best-departure ${cls}"><span>${title}</span><strong>${main}</strong><small>${detail}</small></div>`;
}
function renderTiming(){const d=data(),root=document.getElementById('best');if(!d||!root)return;const html=timingMarkup(d);if(!html)return;let el=document.getElementById('bestDeparturePlan');if(el){const temp=document.createElement('div');temp.innerHTML=html;el.replaceWith(temp.firstElementChild);return}const anchor=root.querySelector('.top-action-banner')||root.querySelector('.section-head');if(anchor)anchor.insertAdjacentHTML('afterend',html);else root.insertAdjacentHTML('afterbegin',html)}
const css=document.createElement('style');css.textContent=`.ti-top-controls{display:flex;gap:8px;align-items:center}.ti-top-controls .secondary.active{border-color:#75b7ff;background:rgba(117,183,255,.12)}#travelOptionsPanel[hidden]{display:none!important}#travelOptionsPanel select{width:100%;min-height:38px}.best-departure{margin:0 0 12px;padding:11px 13px;border-radius:9px;border:1px solid rgba(117,183,255,.28);background:rgba(117,183,255,.045)}.best-departure span,.best-departure strong,.best-departure small{display:block}.best-departure span{font-size:9px;letter-spacing:.11em;font-weight:900;color:#75b7ff}.best-departure strong{font-size:17px;margin-top:3px}.best-departure small{font-size:10px;color:var(--muted,#9ba1ad);margin-top:3px}.best-departure.return{border-color:rgba(85,201,134,.4);background:rgba(85,201,134,.055)}.best-departure.return span{color:#70d89a}.best-departure.flight{border-color:rgba(244,199,109,.35);background:rgba(244,199,109,.045)}.best-departure.flight span{color:#f4c76d}@media(max-width:700px){.topbar{gap:10px}.ti-top-controls{width:100%;justify-content:flex-end;flex-wrap:wrap}}`;document.head.appendChild(css);
setTimeout(()=>{installOptions();renderTiming()},300);setInterval(()=>{installOptions();renderTiming()},1000);
})();
