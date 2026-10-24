// ==UserScript==
// @name         TornTools Foreign People Sync
// @namespace    http://127.0.0.1:8765/
// @version      0.2.0
// @description  Syncs player IDs from Torn index.php?page=people&start=N into TornTools Foreign Monitor, with page-by-page navigation.
// @match        https://www.torn.com/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';

  const ENDPOINT = 'http://127.0.0.1:8765/api/foreign-monitor/seed';
  const PANEL_ID = 'tt-foreign-sync';
  const PAGE_SIZE = 25;
  const COUNTRY_MAP = [
    ['can', ['canada','canadian']],
    ['mex', ['mexico']],
    ['cay', ['cayman']],
    ['haw', ['hawaii']],
    ['uk', ['united kingdom','britain','british']],
    ['arg', ['argentina','argentinian']],
    ['swi', ['switzerland','swiss']],
    ['jap', ['japan','japanese']],
    ['chi', ['china','chinese']],
    ['uae', ['uae','united arab emirates']],
    ['saf', ['south africa','south african']]
  ];

  function peoplePageInfo() {
    const u = new URL(location.href);
    const isPeople = /\/index\.php$/i.test(u.pathname) && u.searchParams.get('page') === 'people';
    const start = Math.max(0, Number(u.searchParams.get('start') || 0) || 0);
    return {isPeople, start};
  }

  function peoplePageUrl(start) {
    const u = new URL(location.href);
    u.pathname = '/index.php';
    u.searchParams.set('page', 'people');
    u.searchParams.set('start', String(Math.max(0, Number(start) || 0)));
    return u.toString();
  }

  function detectCountry() {
    const hay = (document.title + ' ' + location.href + ' ' + (document.body?.innerText || '').slice(0,12000)).toLowerCase();
    for (const [code, aliases] of COUNTRY_MAP) {
      if (aliases.some(x => hay.includes(x))) return code;
    }
    return null;
  }

  function extractPlayers() {
    const map = new Map();
    const anchors = [...document.querySelectorAll('a[href]')];
    for (const a of anchors) {
      const href = a.getAttribute('href') || '';
      let m = href.match(/profiles\.php\?XID=(\d+)/i);
      if (!m) m = href.match(/[?&](?:user2ID|XID)=(\d+)/i);
      if (!m) continue;
      const id = Number(m[1]);
      if (!Number.isFinite(id) || id <= 0) continue;
      const text = (a.textContent || '').trim();
      let name = text.replace(/\s*\[\d+\]\s*$/, '').trim();
      if (!name || /^profile$/i.test(name) || /^attack$/i.test(name)) name = null;
      const row = a.closest('li,tr,[class*="row"],[class*="user"],[class*="player"]');
      const rowText = (row?.innerText || '').replace(/\s+/g,' ');
      const lm = rowText.match(/\b(?:level|lvl)\s*[:#]?\s*(\d{1,3})\b/i);
      const level = lm ? Number(lm[1]) : null;
      if (!map.has(id)) map.set(id, {player_id:id, name, level});
      else {
        const cur = map.get(id);
        if (!cur.name && name) cur.name = name;
        if (!cur.level && level) cur.level = level;
      }
    }
    return [...map.values()];
  }

  function post(country, players) {
    return new Promise((resolve,reject) => {
      GM_xmlhttpRequest({
        method:'POST',
        url:ENDPOINT,
        headers:{'Content-Type':'application/json'},
        data:JSON.stringify({country,players}),
        timeout:10000,
        onload:r=>{
          try {
            const d=JSON.parse(r.responseText||'{}');
            if (r.status<200 || r.status>=300) return reject(new Error(d.detail||('HTTP '+r.status)));
            resolve(d);
          } catch(e){ reject(e); }
        },
        onerror:()=>reject(new Error('Could not reach TornTools localhost')),
        ontimeout:()=>reject(new Error('TornTools request timed out'))
      });
    });
  }

  function ensurePanel() {
    if (document.getElementById(PANEL_ID)) return;
    const style=document.createElement('style');
    style.textContent=`
      #${PANEL_ID}{position:fixed;right:18px;top:118px;z-index:2147483646;background:#111820;color:#eef4ff;border:1px solid #44536a;border-radius:10px;padding:10px 12px;min-width:255px;max-width:350px;font:13px/1.35 Arial,sans-serif;box-shadow:0 8px 28px rgba(0,0,0,.45)}
      #${PANEL_ID} .tt-title{font-weight:800;margin-bottom:6px}
      #${PANEL_ID} button{cursor:pointer;border:1px solid #60738d;border-radius:6px;background:#1f2b3a;color:#fff;padding:7px 10px;font-weight:700;margin-top:8px}
      #${PANEL_ID} .tt-muted{opacity:.72;font-size:11px;margin-top:5px}
    `;
    document.documentElement.appendChild(style);
    const p=document.createElement('div');
    p.id=PANEL_ID;
    const page=peoplePageInfo();
    p.innerHTML='<div class="tt-title">TornTools Foreign Sync</div>'+
      '<div id="tt-foreign-state">'+(page.isPeople?'People page offset '+page.start+'. Ready to sync.':'Open Torn People: index.php?page=people&start=0')+'</div>'+
      '<button id="tt-foreign-btn">Sync visible people</button> '+
      '<button id="tt-foreign-next">Sync + next 25</button> '+
      '<button id="tt-foreign-prev">Previous 25</button>'+
      '<div class="tt-muted">Uses the visible Torn People page. Current offset: '+page.start+'.</div>';
    document.body.appendChild(p);

    async function syncCurrent(goNext){
      const state=p.querySelector('#tt-foreign-state');
      const pageNow=peoplePageInfo();
      if(!pageNow.isPeople){ state.textContent='Open https://www.torn.com/index.php?page=people&start=0 first.'; return; }
      const country=detectCountry();
      const players=extractPlayers();
      if(!country){ state.textContent='Could not detect your foreign country on this People page.'; return; }
      if(!players.length){ state.textContent='No player profile links were found at offset '+pageNow.start+'.'; return; }
      state.textContent='Syncing '+players.length+' players from offset '+pageNow.start+'…';
      try{
        const d=await post(country,players);
        state.textContent='Synced '+d.matched+' / '+d.received+' to '+(d.country_name||country.toUpperCase())+' from offset '+pageNow.start+'.';
        if(goNext){
          setTimeout(()=>{ location.href=peoplePageUrl(pageNow.start + PAGE_SIZE); },350);
        }
      }catch(e){ state.textContent='Sync failed: '+(e.message||e); }
    }

    p.querySelector('#tt-foreign-btn').addEventListener('click', ()=>syncCurrent(false));
    p.querySelector('#tt-foreign-next').addEventListener('click', ()=>syncCurrent(true));
    p.querySelector('#tt-foreign-prev').addEventListener('click', ()=>{
      const pageNow=peoplePageInfo();
      location.href=peoplePageUrl(Math.max(0,pageNow.start-PAGE_SIZE));
    });
  }

  ensurePanel();
})();