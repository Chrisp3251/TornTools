/* Clarify Travel Intelligence confidence labels and show explicit restock times. */
(function(){
  const clock=ts=>Number(ts)>0?new Date(Number(ts)*1000).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'}):'—';
  function enhanceRestocks(){
    document.querySelectorAll('[data-restock-card]').forEach(card=>{
      const est=Number(card.dataset.est||0);
      let info=card.querySelector('.restock-confidence-explain');
      if(!info){
        info=document.createElement('div');
        info.className='restock-confidence-explain';
        const meta=card.querySelector('.restock-meta, small.muted');
        (meta||card).insertAdjacentElement('afterend',info);
      }
      const metaText=(card.querySelector('.restock-meta, small.muted')?.textContent||'').trim();
      const conf=((metaText.match(/\b(HIGH|MEDIUM|LOW)\b/i)||[])[1]||'LOW').toUpperCase();
      info.innerHTML=est>0
        ? `<span>Estimated restock</span><strong>${clock(est)}</strong><small>${conf} restock confidence · based on observed stockout → restock cycles</small>`
        : '<span>Estimated restock</span><strong>Learning</strong><small>Not enough completed stockout → restock cycles for a useful time yet</small>';
    });
  }
  function relabel(){
    document.querySelectorAll('th').forEach(th=>{
      if(th.textContent.trim()==='Confidence'){
        th.textContent='Stock / price confidence';
        th.title='Freshness/reliability of the current foreign-stock and resale-price snapshot. This is not restock confidence.';
      }
    });
    document.querySelectorAll('#best .section-head p.muted,#travelState .summary-card small').forEach(el=>{
      el.innerHTML=el.innerHTML.replace(/\b(HIGH|MEDIUM|LOW) confidence\b/ig,'$1 stock/price confidence');
    });
  }
  function legend(){
    const details=document.getElementById('rows')?.closest('details');
    if(!details||details.querySelector('.confidence-legend'))return;
    const box=document.createElement('div');
    box.className='confidence-legend';
    box.innerHTML='<strong>Confidence key</strong><span><b>Stock / price confidence</b> = how fresh/reliable the current foreign stock + resale-price snapshot is.</span><span><b>Restock confidence</b> = how reliable the predicted restock time is, based on observed completed stockout → restock cycles.</span>';
    const wrap=details.querySelector('.table-wrap');
    if(wrap)details.insertBefore(box,wrap);
  }
  function run(){relabel();enhanceRestocks();legend();}
  const css=document.createElement('style');
  css.textContent='.restock-confidence-explain{margin-top:6px;padding:7px 8px;border-radius:7px;background:rgba(117,183,255,.055);border:1px solid rgba(117,183,255,.18)}.restock-confidence-explain span,.restock-confidence-explain strong,.restock-confidence-explain small{display:block}.restock-confidence-explain span{font-size:8px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted,#9ba1ad)}.restock-confidence-explain strong{font-size:13px;margin-top:2px}.restock-confidence-explain small{font-size:9px;color:var(--muted,#9ba1ad);margin-top:2px}.confidence-legend{margin:10px 0 0;padding:10px 12px;border:1px solid var(--border,#343943);border-radius:8px;background:rgba(255,255,255,.025);font-size:10px;color:var(--muted,#9ba1ad);line-height:1.45}.confidence-legend strong,.confidence-legend span{display:block}.confidence-legend strong{margin-bottom:3px}.confidence-legend b{color:inherit}';
  document.head.appendChild(css);
  setInterval(run,1000);
  setTimeout(run,250);
})();