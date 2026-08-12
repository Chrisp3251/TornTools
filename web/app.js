const money = new Intl.NumberFormat("en-US",{style:"currency",currency:"USD",maximumFractionDigits:0});
let marketUrl = "https://www.torn.com/page.php?sid=ItemMarket#/market/view=search&itemID=206&sortField=price&sortOrder=ASC";
let timer = null;

async function call(path, opts={}) {
  const r = await fetch(path,{headers:{"Content-Type":"application/json"},...opts});
  let data={}; try{data=await r.json()}catch{}
  if(!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}
function msg(t, bad=false){ const e=document.getElementById("msg"); e.textContent=t; e.className=bad?"bad":"good"; }
async function status(){
  try{
    const d=await call("/api/status");
    const e=document.getElementById("status");
    e.textContent=d.key_loaded?"● Backend ready · key loaded":"● Backend ready · key needed";
    e.className=d.key_loaded?"good":"warn";
  }catch{ document.getElementById("status").textContent="Backend unavailable"; }
}
async function loadKey(){
  const key=document.getElementById("key").value.trim();
  if(!key) return msg("Paste a key first.",true);
  try{
    await call("/api/key",{method:"POST",body:JSON.stringify({api_key:key})});
    document.getElementById("key").value="";
    msg("Key loaded into backend memory.");
    await status();
  }catch(e){msg(e.message,true);}
}
async function forgetKey(){
  try{ await call("/api/key",{method:"DELETE"}); msg("Key forgotten."); await status(); }
  catch(e){msg(e.message,true);}
}
async function refreshMarket(){
  try{
    const d=await call("/api/xanax");
    marketUrl=d.market_url || marketUrl;
    document.getElementById("low").textContent=money.format(d.lowest);
    document.getElementById("qty").textContent=`${d.qty_floor} item(s) at floor`;
    document.getElementById("next").textContent=d.next_price?money.format(d.next_price):"—";
    document.getElementById("profit").textContent=d.net_profit_after_5pct===null?"—":money.format(d.net_profit_after_5pct);
    document.getElementById("roi").textContent=d.net_roi_after_5pct===null?"—":`${d.net_roi_after_5pct.toFixed(2)}% theoretical ROI`;
    const rows=document.getElementById("rows"); rows.innerHTML="";
    d.top.forEach((x,i)=>{ rows.innerHTML += `<tr><td>${i+1}</td><td>${money.format(x.price)}</td><td>${x.amount}</td></tr>`;});
    msg("Market refreshed.");
  }catch(e){msg(e.message,true);}
}
function toggleAuto(){
  if(timer){clearInterval(timer); timer=null;}
  if(document.getElementById("auto").checked){ refreshMarket(); timer=setInterval(refreshMarket,30000); }
}
status();
