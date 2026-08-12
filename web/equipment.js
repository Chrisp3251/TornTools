const eqMoney = new Intl.NumberFormat("en-US", {style:"currency", currency:"USD", maximumFractionDigits:0});
let equipmentInventoryLoaded = false;
let equipmentInventory = [];

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
  if (showEquipment && !equipmentInventoryLoaded) loadEquipmentInventory(false);
}

function eqVal(id) {
  const el = document.getElementById(id);
  if (!el || el.value.trim() === "") return null;
  const n = Number(el.value);
  return Number.isFinite(n) ? n : null;
}

function statText(i) {
  const parts=[];
  if (i.damage != null) parts.push(`DMG ${Number(i.damage).toFixed(2)}`);
  if (i.accuracy != null) parts.push(`ACC ${Number(i.accuracy).toFixed(2)}`);
  if (i.armor != null) parts.push(`ARM ${Number(i.armor).toFixed(2)}`);
  return parts.join(" · ") || "—";
}

function inventoryCategory(i) {
  return String(i.category || i.type || i.sub_type || "Other").trim() || "Other";
}

function populateCategoryFilter() {
  const select=document.getElementById("eqCategoryFilter");
  if (!select) return;
  const current=select.value || "all";
  const categories=[...new Set(equipmentInventory.map(inventoryCategory))].sort((a,b)=>a.localeCompare(b));
  select.innerHTML='<option value="all">All categories</option>'+categories.map(c=>`<option value="${c.replace(/"/g,'&quot;')}">${c}</option>`).join("");
  select.value=categories.includes(current) ? current : "all";
}

function filteredInventory() {
  const category=document.getElementById("eqCategoryFilter")?.value || "all";
  const status=document.getElementById("eqStatusFilter")?.value || "all";
  const sort=document.getElementById("eqSort")?.value || "category";
  const search=(document.getElementById("eqSearch")?.value || "").trim().toLowerCase();
  let items=equipmentInventory.filter(i=>{
    if (category!=="all" && inventoryCategory(i)!==category) return false;
    if (status==="plain" && !i.plain) return false;
    if (status==="rw" && i.plain) return false;
    if (status==="equipped" && !i.equipped) return false;
    if (search) {
      const hay=[i.name,i.item_id,i.uid,inventoryCategory(i),i.type,i.sub_type,i.rarity,statText(i)].join(" ").toLowerCase();
      if (!hay.includes(search)) return false;
    }
    return true;
  });
  items=[...items];
  if (sort==="name") items.sort((a,b)=>String(a.name).localeCompare(String(b.name)) || Number(b.quality||0)-Number(a.quality||0));
  else if (sort==="quality_desc") items.sort((a,b)=>Number(b.quality||-1)-Number(a.quality||-1) || String(a.name).localeCompare(String(b.name)));
  else if (sort==="quality_asc") items.sort((a,b)=>Number(a.quality??999)-Number(b.quality??999) || String(a.name).localeCompare(String(b.name)));
  else items.sort((a,b)=>inventoryCategory(a).localeCompare(inventoryCategory(b)) || String(a.name).localeCompare(String(b.name)) || Number(b.quality||0)-Number(a.quality||0));
  return items;
}

function renderInventory(items) {
  const rows=document.getElementById("eqInventoryRows");
  const visible=document.getElementById("eqVisibleCount");
  if (visible) visible.textContent=`${items.length} shown / ${equipmentInventory.length} loaded`;
  if (!items.length) {
    rows.innerHTML='<tr><td colspan="7" class="muted">No equipment matches those filters.</td></tr>';
    return;
  }
  rows.innerHTML=items.map(i=>{
    const status=i.plain ? (i.equipped ? "PLAIN · EQUIPPED" : "PLAIN") : (i.rarity ? String(i.rarity).toUpperCase() : "BONUSED/RW");
    const rowClass=i.plain ? "" : "mild-hit";
    return `<tr class="${rowClass}">
      <td><strong>${i.name}</strong><br><small class="muted">Item #${i.item_id}${i.uid?` · ${i.uid}`:""}</small></td>
      <td><strong>${inventoryCategory(i)}</strong></td>
      <td>${i.quality==null?"—":`${Number(i.quality).toFixed(2)}%`}</td>
      <td>${statText(i)}</td>
      <td>${i.type||i.sub_type||"—"}</td>
      <td>${status}</td>
      <td><button class="mini-btn" onclick="useInventoryItem(${i._sourceIndex})">Use</button></td>
    </tr>`;
  }).join("");
}

function applyInventoryFilters() { renderInventory(filteredInventory()); }

async function loadEquipmentInventory(force=false) {
  if (equipmentInventoryLoaded && !force) return;
  const btn=document.getElementById("eqInventoryBtn");
  const status=document.getElementById("eqInventoryStatus");
  const rows=document.getElementById("eqInventoryRows");
  if (btn) { btn.disabled=true; btn.textContent="Loading…"; }
  if (status) status.textContent="Loading your Torn inventory…";
  if (rows) rows.innerHTML='<tr><td colspan="7" class="muted">Loading equipment…</td></tr>';
  try {
    const r=await fetch("/api/equipment/inventory");
    let d={}; try { d=await r.json(); } catch {}
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    equipmentInventory=(d.items||[]).map((i,index)=>({...i,_sourceIndex:index}));
    equipmentInventoryLoaded=true;
    populateCategoryFilter();
    applyInventoryFilters();
    if (status) {
      const extras=[];
      if (d.truncated) extras.push("inventory detail scan was capped");
      if (d.detail_errors) extras.push(`${d.detail_errors} detail lookup error(s)`);
      status.innerHTML=`Loaded <strong>${equipmentInventory.length}</strong> equipment item(s) from your Torn inventory. Use the category, status, quality sort, or search controls to narrow the list.${extras.length?` <span class="warn">${extras.join(" · ")}</span>`:""}`;
    }
  } catch(e) {
    equipmentInventoryLoaded=false;
    if (rows) rows.innerHTML='<tr><td colspan="7" class="muted">Inventory unavailable. Manual equipment checking still works below.</td></tr>';
    if (status) status.innerHTML=`<span class="bad">Could not load inventory: ${e.message}</span>`;
  } finally {
    if (btn) { btn.disabled=false; btn.textContent="Refresh Inventory"; }
  }
}

async function loadSelectedItemValues(i) {
  const el=document.getElementById("eqSelectedMarket");
  const vendorInput=document.getElementById("eqVendor");
  if (el) el.textContent="Torn values: checking…";
  try {
    const r=await fetch(`/api/equipment/values?item_id=${encodeURIComponent(i.item_id)}`);
    let d={}; try{d=await r.json()}catch{}
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    i.vendor_sell=d.sell_price;
    i.torn_market_price=d.market_price;
    if (vendorInput && d.sell_price != null) vendorInput.value=String(d.sell_price);
    if (el) el.innerHTML=`Torn market value: <strong>${d.market_price != null ? eqMoney.format(d.market_price) : "—"}</strong> · Vendor/NPC sell: <strong>${d.sell_price != null ? eqMoney.format(d.sell_price) : "—"}</strong><span class="muted"> · Torn base item values</span>`;
    return d;
  } catch(e) {
    if (el) el.innerHTML=`Torn values: <span class="muted">unavailable (${e.message})</span>`;
    return null;
  }
}

async function useInventoryItem(index) {
  const i=equipmentInventory[index];
  if (!i) return;
  document.getElementById("eqItemId").value=i.item_id ?? "";
  document.getElementById("eqQuality").value=i.quality ?? "";
  document.getElementById("eqDamage").value=i.damage ?? "";
  document.getElementById("eqAccuracy").value=i.accuracy ?? "";
  document.getElementById("eqArmor").value=i.armor ?? "";
  document.getElementById("eqVendor").value=i.vendor_sell ?? "";
  const selected=document.getElementById("eqSelected");
  if (selected) selected.textContent=`Selected: ${i.name} · ${inventoryCategory(i)} · ${i.quality==null?"quality unknown":`${Number(i.quality).toFixed(2)}% quality`}${i.plain?"":" · RW/bonused"}`;
  document.getElementById("equipmentResult").innerHTML='<div class="empty">Stats loaded. Fetching Torn vendor and market values…</div>';
  const meta=await loadSelectedItemValues(i);
  document.getElementById("equipmentResult").innerHTML=meta
    ? '<div class="empty">Stats and Torn values loaded. Click Check Equipment.</div>'
    : '<div class="empty">Stats loaded. Vendor value could not be fetched automatically, but Check Equipment will still try Torn values on the backend.</div>';
}

function compRow(c) {
  const stats=[];
  if (c.damage != null) stats.push(`DMG ${c.damage.toFixed(2)}`);
  if (c.accuracy != null) stats.push(`ACC ${c.accuracy.toFixed(2)}`);
  if (c.armor != null) stats.push(`ARM ${c.armor.toFixed(2)}`);
  return `<tr><td><strong>${Number(c.quality).toFixed(2)}%</strong></td><td>${stats.join(" · ") || "—"}</td><td><strong>${eqMoney.format(c.price)}</strong></td></tr>`;
}

function renderEquipmentResult(d) {
  const box=document.getElementById("equipmentResult");
  const verdictClass=d.verdict==="MARKET IT"?"eq-market":d.verdict==="VENDOR"?"eq-vendor":"eq-hold";
  const percentile=d.quality_percentile==null?"—":`${d.quality_percentile.toFixed(1)}th percentile`;
  const cache=d.cache||{};
  const cacheText=cache.cache_age_seconds==null?"Unknown market-cache age":`${cache.freshness} · ${cache.cache_age_seconds}s old`;
  const selectedMarket=document.getElementById("eqSelectedMarket");
  if (selectedMarket) selectedMarket.innerHTML=`Torn market value: <strong>${d.torn_market_price != null ? eqMoney.format(d.torn_market_price) : "—"}</strong> · Vendor/NPC sell: <strong>${d.vendor_sell != null ? eqMoney.format(d.vendor_sell) : "—"}</strong>`;
  box.innerHTML=`
    <article class="equipment-result-card ${verdictClass}">
      <div class="eq-result-head"><div><div class="eyebrow">${d.type||"EQUIPMENT"}</div><h2>${d.name}</h2><p class="muted">Item #${d.item_id} · ${cacheText}</p></div><div class="eq-verdict">${d.verdict}</div></div>
      <p class="eq-reason">${d.reason}</p>
      <div class="eq-metrics">
        <div><span>Your quality</span><strong>${Number(d.your_stats.quality).toFixed(2)}%</strong><small>${percentile} vs current plain listings</small></div>
        <div><span>Vendor value</span><strong>${eqMoney.format(d.vendor_sell||0)}</strong><small>${d.vendor_sell_source==="torn"?"Auto-filled from Torn":"Manual override"}</small></div>
        <div><span>Competitive ask</span><strong>${d.competitive_ask?eqMoney.format(d.competitive_ask):"—"}</strong><small>Based on closest current plain listings</small></div>
        <div><span>After 5% fee</span><strong>${d.net_after_fee?eqMoney.format(d.net_after_fee):"—"}</strong><small>${d.premium_over_vendor==null?"No comparison":`${d.premium_over_vendor>=0?"+":""}${eqMoney.format(d.premium_over_vendor)} vs vendor`}</small></div>
      </div>
      <div class="research-note"><strong>Confidence: ${d.confidence}</strong> · ${d.close_comparables} close-quality comparables · ${d.plain_listings} plain listings checked. Asking prices are not confirmed sale prices.</div>
      <div class="table-wrap"><table class="research-table"><thead><tr><th>Comparable quality</th><th>Stats</th><th>Asking price</th></tr></thead><tbody>${(d.comps||[]).map(compRow).join("")||'<tr><td colspan="3">No comparable listings.</td></tr>'}</tbody></table></div>
      <div class="toolbar eq-actions"><button onclick="window.open('${d.market_url}','_blank','noopener')">Open This Item Market</button><span class="muted">Torn market value: ${d.torn_market_price!=null?eqMoney.format(d.torn_market_price):"—"} · Live Item Market average: ${d.market_average?eqMoney.format(d.market_average):"—"} · Median closest ask: ${d.median_ask?eqMoney.format(d.median_ask):"—"}</span></div>
    </article>`;
}

async function checkEquipment() {
  const itemId=eqVal("eqItemId");
  const quality=eqVal("eqQuality");
  const damage=eqVal("eqDamage");
  const accuracy=eqVal("eqAccuracy");
  const armor=eqVal("eqArmor");
  const vendor=eqVal("eqVendor");
  const result=document.getElementById("equipmentResult");
  const btn=document.getElementById("eqCheckBtn");
  if (!itemId || quality==null) { result.innerHTML='<div class="empty bad">Item ID and Quality are required.</div>'; return; }
  const params=new URLSearchParams({item_id:String(Math.trunc(itemId)),quality:String(quality)});
  if (vendor!=null) params.set("vendor_sell",String(Math.max(0,Math.trunc(vendor))));
  if (damage!=null) params.set("damage",String(damage));
  if (accuracy!=null) params.set("accuracy",String(accuracy));
  if (armor!=null) params.set("armor",String(armor));
  btn.disabled=true; btn.textContent="Checking market…";
  result.innerHTML='<div class="empty">Checking current plain listings…</div>';
  try {
    const r=await fetch(`/api/equipment/check?${params.toString()}`);
    let d={}; try{d=await r.json()}catch{}
    if (!r.ok) throw new Error(d.detail||`HTTP ${r.status}`);
    if (document.getElementById("eqVendor") && d.vendor_sell != null) document.getElementById("eqVendor").value=String(d.vendor_sell);
    renderEquipmentResult(d);
  } catch(e) { result.innerHTML=`<div class="empty bad">${e.message}</div>`; }
  finally { btn.disabled=false; btn.textContent="Check Equipment"; }
}

function clearEquipment() {
  ["eqItemId","eqQuality","eqDamage","eqAccuracy","eqArmor","eqVendor"].forEach(id=>{const el=document.getElementById(id);if(el)el.value="";});
  const selected=document.getElementById("eqSelected"); if(selected)selected.textContent="No inventory item selected.";
  const market=document.getElementById("eqSelectedMarket"); if(market)market.textContent="Torn market value: —";
  document.getElementById("equipmentResult").innerHTML='<div class="empty">Choose an inventory item above or enter an item and its stats, then check the market.</div>';
}

window.addEventListener("DOMContentLoaded",()=>{
  const saved=localStorage.getItem("torntools.activeTab")||"scanner";
  switchToolTab(saved==="equipment"?"equipment":"scanner");
  ["eqItemId","eqQuality","eqDamage","eqAccuracy","eqArmor","eqVendor"].forEach(id=>{const el=document.getElementById(id);if(el)el.addEventListener("keydown",e=>{if(e.key==="Enter")checkEquipment();});});
});
