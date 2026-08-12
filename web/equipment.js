const eqMoney = new Intl.NumberFormat("en-US", {style:"currency", currency:"USD", maximumFractionDigits:0});

function switchToolTab(tab) {
  const scanner = document.getElementById("scannerView");
  const equipment = document.getElementById("equipmentView");
  const scannerBtn = document.getElementById("tabScanner");
  const equipmentBtn = document.getElementById("tabEquipment");
  const showEquipment = tab === "equipment";
  scanner.hidden = showEquipment;
  equipment.hidden = !showEquipment;
  scannerBtn.classList.toggle("active", !showEquipment);
  equipmentBtn.classList.toggle("active", showEquipment);
  localStorage.setItem("torntools.activeTab", showEquipment ? "equipment" : "scanner");
}

function eqVal(id) {
  const el = document.getElementById(id);
  if (!el || el.value.trim() === "") return null;
  const n = Number(el.value);
  return Number.isFinite(n) ? n : null;
}

function compRow(c) {
  const stats = [];
  if (c.damage != null) stats.push(`DMG ${c.damage.toFixed(2)}`);
  if (c.accuracy != null) stats.push(`ACC ${c.accuracy.toFixed(2)}`);
  if (c.armor != null) stats.push(`ARM ${c.armor.toFixed(2)}`);
  return `<tr><td><strong>${Number(c.quality).toFixed(2)}%</strong></td><td>${stats.join(" · ") || "—"}</td><td><strong>${eqMoney.format(c.price)}</strong></td></tr>`;
}

function renderEquipmentResult(d) {
  const box = document.getElementById("equipmentResult");
  const verdictClass = d.verdict === "MARKET IT" ? "eq-market" : d.verdict === "VENDOR" ? "eq-vendor" : "eq-hold";
  const percentile = d.quality_percentile == null ? "—" : `${d.quality_percentile.toFixed(1)}th percentile`;
  const cache = d.cache || {};
  const cacheText = cache.cache_age_seconds == null ? "Unknown market-cache age" : `${cache.freshness} · ${cache.cache_age_seconds}s old`;
  box.innerHTML = `
    <article class="equipment-result-card ${verdictClass}">
      <div class="eq-result-head">
        <div><div class="eyebrow">${d.type || "EQUIPMENT"}</div><h2>${d.name}</h2><p class="muted">Item #${d.item_id} · ${cacheText}</p></div>
        <div class="eq-verdict">${d.verdict}</div>
      </div>
      <p class="eq-reason">${d.reason}</p>
      <div class="eq-metrics">
        <div><span>Your quality</span><strong>${Number(d.your_stats.quality).toFixed(2)}%</strong><small>${percentile} vs current plain listings</small></div>
        <div><span>Vendor value</span><strong>${eqMoney.format(d.vendor_sell || 0)}</strong><small>Guaranteed cash-out you entered</small></div>
        <div><span>Competitive ask</span><strong>${d.competitive_ask ? eqMoney.format(d.competitive_ask) : "—"}</strong><small>Based on closest current plain listings</small></div>
        <div><span>After 5% fee</span><strong>${d.net_after_fee ? eqMoney.format(d.net_after_fee) : "—"}</strong><small>${d.premium_over_vendor == null ? "No comparison" : `${d.premium_over_vendor >= 0 ? "+" : ""}${eqMoney.format(d.premium_over_vendor)} vs vendor`}</small></div>
      </div>
      <div class="research-note"><strong>Confidence: ${d.confidence}</strong> · ${d.close_comparables} close-quality comparables · ${d.plain_listings} plain listings checked. Asking prices are not confirmed sale prices.</div>
      <div class="table-wrap"><table class="research-table"><thead><tr><th>Comparable quality</th><th>Stats</th><th>Asking price</th></tr></thead><tbody>${(d.comps || []).map(compRow).join("") || '<tr><td colspan="3">No comparable listings.</td></tr>'}</tbody></table></div>
      <div class="toolbar eq-actions"><button onclick="window.open('${d.market_url}','_blank','noopener')">Open This Item Market</button><span class="muted">Market average: ${d.market_average ? eqMoney.format(d.market_average) : "—"} · Median closest ask: ${d.median_ask ? eqMoney.format(d.median_ask) : "—"}</span></div>
    </article>`;
}

async function checkEquipment() {
  const itemId = eqVal("eqItemId");
  const quality = eqVal("eqQuality");
  const damage = eqVal("eqDamage");
  const accuracy = eqVal("eqAccuracy");
  const armor = eqVal("eqArmor");
  const vendor = eqVal("eqVendor") ?? 0;
  const result = document.getElementById("equipmentResult");
  const btn = document.getElementById("eqCheckBtn");
  if (!itemId || quality == null) {
    result.innerHTML = '<div class="empty bad">Item ID and Quality are required.</div>';
    return;
  }
  const params = new URLSearchParams({item_id:String(Math.trunc(itemId)), quality:String(quality), vendor_sell:String(Math.max(0,Math.trunc(vendor)))});
  if (damage != null) params.set("damage", String(damage));
  if (accuracy != null) params.set("accuracy", String(accuracy));
  if (armor != null) params.set("armor", String(armor));
  btn.disabled = true; btn.textContent = "Checking market…";
  result.innerHTML = '<div class="empty">Checking current plain listings…</div>';
  try {
    const r = await fetch(`/api/equipment/check?${params.toString()}`);
    let d = {};
    try { d = await r.json(); } catch {}
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    renderEquipmentResult(d);
  } catch (e) {
    result.innerHTML = `<div class="empty bad">${e.message}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = "Check Equipment";
  }
}

function clearEquipment() {
  ["eqItemId","eqQuality","eqDamage","eqAccuracy","eqArmor","eqVendor"].forEach(id => { const el=document.getElementById(id); if(el) el.value=""; });
  document.getElementById("equipmentResult").innerHTML = '<div class="empty">Enter an item and its stats, then check the market.</div>';
}

window.addEventListener("DOMContentLoaded", () => {
  const saved = localStorage.getItem("torntools.activeTab") || "scanner";
  switchToolTab(saved === "equipment" ? "equipment" : "scanner");
  ["eqItemId","eqQuality","eqDamage","eqAccuracy","eqArmor","eqVendor"].forEach(id => {
    const el=document.getElementById(id);
    if (el) el.addEventListener("keydown", e => { if (e.key === "Enter") checkEquipment(); });
  });
});
