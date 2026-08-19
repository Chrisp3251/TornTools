(function () {
  'use strict';

  function statusText(text, isError) {
    const el = document.getElementById('sniperStatus');
    if (!el) return;
    if (isError) el.innerHTML = `<span class="bad">${text}</span>`;
    else el.textContent = text;
  }

  function intelligenceStatusText(text, isError) {
    const el = document.getElementById('intelligenceUpdated');
    if (!el) return;
    if (isError) el.innerHTML = `<span class="bad">${text}</span>`;
    else el.textContent = text;
  }

  function installBazaarWatchNav() {
    const nav = document.querySelector('.tool-tabs');
    if (!nav || document.getElementById('tabBazaarWatch')) return;
    const btn = document.createElement('button');
    btn.id = 'tabBazaarWatch';
    btn.className = 'tool-tab';
    btn.textContent = 'Bazaar Watch';
    btn.addEventListener('click', () => { location.href = '/static/bazaar-watch.html'; });
    nav.appendChild(btn);
  }

  function installMarketIntelligenceButton() {
    const btn = document.getElementById('intelligenceToggleBtn');
    if (!btn || btn.__ttMarketIntelBound) return;
    btn.__ttMarketIntelBound = true;

    btn.removeAttribute('onclick');
    btn.addEventListener('click', () => {
      try {
        if (typeof toggleMarketIntelligence !== 'function') {
          throw new Error('Market Intelligence control did not load');
        }
        toggleMarketIntelligence();
        intelligenceStatusText(
          (typeof intelligenceRunning !== 'undefined' && intelligenceRunning)
            ? 'Market Intelligence started.'
            : 'Market Intelligence stopped.'
        );
      } catch (e) {
        intelligenceStatusText(`Could not toggle Market Intelligence: ${e.message}`, true);
      }
    });
  }

  async function directBuyOne(itemId) {
    const id = Number(itemId);
    const target = sniperTargets.get(id);
    const result = hiddenResults.get(id);
    if (!target || !result || !sniperHit(target, result)) return;

    const qty = Number(result.qty_floor || 0);
    const price = Number(result.lowest || 0);
    const max = effectiveSniperMax(target);
    if (qty !== 1 || !price || price > max) {
      window.open(target.market_url, '_blank', 'noopener');
      return;
    }

    window.open(target.market_url, '_blank');
    statusText(`BUY 1 requested for ${target.name} at ${money.format(price)}. Torn will verify the live listing; normal confirmation remains manual.`);
    try {
      const d = await call('/api/sniper/manual-buy-intent', {
        method: 'POST',
        body: JSON.stringify({
          item_id: id,
          expected_price: price,
          effective_max: max,
          expected_qty: 1
        })
      });
      if (!d || !d.ok) throw new Error((d && d.error) || 'Could not create BUY 1 intent');
    } catch (e) {
      statusText(`BUY 1 relay failed: ${e.message}`, true);
    }
  }

  function augmentRows() {
    const rows = document.querySelectorAll('#sniperRows tr');
    rows.forEach(row => {
      const first = row.cells && row.cells[0];
      const marketCell = row.cells && row.cells[4];
      if (!first || !marketCell) return;
      const match = (first.innerText || '').match(/Item\s*#(\d+)/i);
      if (!match) return;
      const id = Number(match[1]);
      const target = sniperTargets.get(id);
      const result = hiddenResults.get(id);
      if (!target || !result || !sniperHit(target, result)) return;

      const qty = Number(result.qty_floor || 0);
      const price = Number(result.lowest || 0);
      const existingOpen = marketCell.querySelector('button');
      if (qty === 1 && price > 0) {
        if (marketCell.querySelector('.tt-dashboard-buy-one')) return;
        marketCell.innerHTML = '';
        const buy = document.createElement('button');
        buy.className = 'mini-btn tt-dashboard-buy-one';
        buy.textContent = `BUY 1 · ${money.format(price)}`;
        buy.title = 'One manual click. Torn page verifies the same single-unit listing and invokes one native Buy control.';
        buy.addEventListener('click', () => directBuyOne(id));
        marketCell.appendChild(buy);
      } else if (qty > 1 && existingOpen) {
        existingOpen.textContent = `Open Stack (${qty})`;
      }
    });
  }

  function installStyle() {
    if (document.getElementById('ttManualBuyStyles')) return;
    const style = document.createElement('style');
    style.id = 'ttManualBuyStyles';
    style.textContent = `
      .tt-dashboard-buy-one{background:#8e171b!important;border-color:#ff696d!important;font-weight:900!important;box-shadow:0 0 14px rgba(239,92,97,.25)}
      .tt-dashboard-buy-one:hover{background:#b52127!important}
    `;
    document.head.appendChild(style);
  }

  function install() {
    installStyle();
    installBazaarWatchNav();
    installMarketIntelligenceButton();
    if (typeof renderSniperTargets === 'function' && !renderSniperTargets.__ttManualBuyWrapped) {
      const original = renderSniperTargets;
      const wrapped = function () {
        original.apply(this, arguments);
        augmentRows();
      };
      wrapped.__ttManualBuyWrapped = true;
      renderSniperTargets = wrapped;
    }
    augmentRows();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install);
  else install();
})();
