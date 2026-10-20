"use strict";

/* =====================================================================
   AXIOM Pricing -- customer-facing demo.
   Every number comes from the engine run on the server; this file only
   draws it. Charts are hand-made SVG so nothing loads from the internet.
   ===================================================================== */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (n, d = 0) => (n == null || !isFinite(n) ? "–" : Number(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }));
const pct = (n, d = 1) => (n == null || !isFinite(n) ? "–" : `${num(n, d)}%`);
let CUR = "SAR";
const money = (n, d = 2) => (n == null || !isFinite(n) ? "–" : `${num(n, d)} ${CUR}`);
const big = (n, withCur = true) => {
  if (n == null || !isFinite(n)) return "–";
  const a = Math.abs(n);
  const s = a >= 1e9 ? `${num(n / 1e9, 2)} B` : a >= 1e6 ? `${num(n / 1e6, 1)} M` : a >= 1e3 ? `${num(n / 1e3, 0)} k` : num(n, 0);
  return withCur ? `${s} ${CUR}` : s;
};
const signed = (n, f) => (n > 0 ? "+" : "") + f(n);
const monthName = (m) => { const [y, mo] = String(m).split("-"); return `${["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][+mo - 1]} ${y.slice(2)}`; };

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) { let d = r.statusText; try { d = (await r.json()).detail || d; } catch (_) {} throw new Error(d); }
  return r.json();
}
const post = (p, b) => api(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) });

/* --------------------------------------------------------------- state */
let OPT = null, VIEW = null, OV = null, OPP = null, PAIRS = [];
let TAB = "how";
const F = { channel: [], region: [], customer_type: [], segment: [], industry: [], account_class: [], customer: [], start: null, end: null };
const UI = { dim: "channel", metric: "margin_pct", custSort: "revenue", custDesc: true, custQuery: "", oppMode: "gain" };
const ADV = { customer: null, product: null, res: null, tryPrice: null, rules: null };

const FILTER_LABELS = { channel: "Channel", region: "Region", customer_type: "Customer type", segment: "Buying behaviour", industry: "Industry", account_class: "Account class", customer: "Customer" };
const SEG_ORDER = ["Loyal payers", "Smart negotiators", "Big-volume hagglers", "Premium anchors", "Small, heavily discounted", "Thin margin, low discount", "Too new to tell"];
const SEG_COLOR = { "Loyal payers": "var(--s1)", "Smart negotiators": "var(--s4)", "Big-volume hagglers": "var(--s3)", "Premium anchors": "var(--s2)", "Small, heavily discounted": "var(--s5)", "Thin margin, low discount": "var(--s6)", "Too new to tell": "var(--s6)" };
const segColor = (s) => SEG_COLOR[s] || "var(--s6)";

/* --------------------------------------------------------------- tooltip + delegation */
const tip = $("#tip");
document.addEventListener("mousemove", (e) => {
  const t = e.target.closest && e.target.closest("[data-tip]");
  if (!t) { tip.hidden = true; return; }
  tip.innerHTML = t.getAttribute("data-tip");
  tip.hidden = false;
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let x = e.clientX + 14, y = e.clientY + 14;
  if (x + w > innerWidth - 8) x = e.clientX - w - 14;
  if (y + h > innerHeight - 8) y = e.clientY - h - 14;
  tip.style.left = x + "px"; tip.style.top = y + "px";
});
document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-filter],[data-customer],[data-go],[data-advise],[data-dim],[data-metric]");
  if (!el) return;
  if (el.dataset.filter) { if (el.closest("#drawer")) closeDrawer(); const f = JSON.parse(el.dataset.filter); Object.entries(f).forEach(([k, v]) => v.forEach((x) => addFilter(k, x))); refresh(); }
  else if (el.dataset.customer) openCustomer(el.dataset.customer);
  else if (el.dataset.go) showTab(el.dataset.go);
  else if (el.dataset.advise) { const [c, p] = el.dataset.advise.split("|"); closeDrawer(); ADV.customer = c; ADV.product = p; ADV.tryPrice = null; showTab("advisor"); }
  else if (el.dataset.dim) { UI.dim = el.dataset.dim; renderExplore(); }
  else if (el.dataset.metric) { UI.metric = el.dataset.metric; renderExplore(); }
});

/* --------------------------------------------------------------- tabs */
const FILTERED_TABS = new Set(["overview", "explore", "customers"]);
const TABS = ["how", "overview", "explore", "customers", "advisor", "opportunity"];
function showTab(name) {
  TAB = name;
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".page").forEach((p) => { p.hidden = p.id !== "page-" + name; });
  $("#filterbar").hidden = !FILTERED_TABS.has(name);
  render();
  try { localStorage.setItem("axiom-tab3", name); } catch (_) {}
  history.replaceState(null, "", "#" + name);
  window.scrollTo({ top: 0 });
}
$$(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));
function render() {
  if (!VIEW) return;
  ({ overview: renderOverview, explore: renderExplore, customers: renderCustomers, advisor: renderAdvisor, opportunity: renderOpportunity, how: renderHow })[TAB]();
}

/* --------------------------------------------------------------- boot */
async function poll() {
  let s;
  try { s = await api("/api/status"); } catch (_) { setTimeout(poll, 2000); return; }
  $("#status").innerHTML = s.ready ? `<span class="pill ok">live data <b>${esc(s.profile)}</b></span>` : `<span class="pill wait">loading</span>`;
  if (s.error) { $("#loading").innerHTML = `<p><b>The engine did not start</b></p><div class="error">${esc(s.error)}</div>`; return; }
  if (!s.ready) { $("#loading-step").textContent = `${s.step} · ${s.seconds} s`; setTimeout(poll, 1500); return; }
  [OPT, OV, OPP, PAIRS] = await Promise.all([api("/api/options"), api("/api/overview"), api("/api/opportunity"), api("/api/pairs")]);
  CUR = OPT.currency || "SAR";
  PAIRS.forEach((p) => { if (p.name) NAMES[p.customer_id] = p.name; });
  buildFilterBar();
  await refresh(false);
  $("#loading").hidden = true;
  // A link like /#advisor or /#customers/CUST0251 opens that page directly.
  const [hashTab, hashCustomer] = location.hash.slice(1).split("/");
  let tab = "how"; try { tab = localStorage.getItem("axiom-tab3") || "how"; } catch (_) {}
  if (TABS.includes(hashTab)) tab = hashTab;
  showTab(tab);
  if (hashCustomer) openCustomer(decodeURIComponent(hashCustomer));
}

/* --------------------------------------------------------------- filters */
function addFilter(k, v) { if (k === "start" || k === "end") { F[k] = v; return; } if (!F[k].includes(v)) F[k].push(v); }
function removeFilter(k, v) { if (k === "start" || k === "end") F[k] = null; else F[k] = F[k].filter((x) => x !== v); }
function anyFilter() { return Object.values(F).some((v) => (Array.isArray(v) ? v.length : v)); }

function buildFilterBar() {
  const dds = ["channel", "region", "customer_type", "segment", "industry", "account_class"].map((k) => `<div class="dd" data-k="${k}"><button type="button">${esc(FILTER_LABELS[k])}</button></div>`).join("");
  const months = OPT.months.map((m) => `<option value="${m}">${monthName(m)}</option>`).join("");
  $("#fb-controls").innerHTML = `<span class="lbl">Show me</span>${dds}<span class="lbl" style="margin-left:6px">from</span><select class="period" id="f-start"><option value="">start</option>${months}</select><span class="lbl">to</span><select class="period" id="f-end"><option value="">end</option>${months}</select>`;
  $$("#fb-controls .dd > button").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); toggleMenu(b.parentElement); }));
  $("#f-start").addEventListener("change", (e) => { F.start = e.target.value || null; refresh(); });
  $("#f-end").addEventListener("change", (e) => { F.end = e.target.value || null; refresh(); });
  document.addEventListener("click", (e) => { if (!e.target.closest(".dd")) $$(".dd-menu").forEach((m) => m.remove()); });
}
function toggleMenu(dd) {
  const open = $(".dd-menu", dd);
  $$(".dd-menu").forEach((m) => m.remove());
  if (open) return;
  const k = dd.dataset.k;
  const menu = document.createElement("div");
  menu.className = "dd-menu";
  menu.innerHTML = OPT.filters[k].map((v) => `<label><input type="checkbox" value="${esc(v)}" ${F[k].includes(v) ? "checked" : ""}>${k === "segment" ? `<span class="dot" style="background:${segColor(v)}"></span>` : ""}${esc(v)}</label>`).join("");
  menu.addEventListener("click", (e) => e.stopPropagation());
  menu.addEventListener("change", (e) => { if (e.target.checked) addFilter(k, e.target.value); else removeFilter(k, e.target.value); refresh(); });
  dd.appendChild(menu);
}
function renderActiveChips() {
  const chips = [];
  Object.entries(F).forEach(([k, v]) => {
    if (Array.isArray(v)) v.forEach((x) => chips.push(`<span class="chip">${esc(FILTER_LABELS[k])}: <b>${esc(k === "customer" ? customerName(x) : x)}</b><button data-rm="${k}|${esc(x)}" aria-label="remove">×</button></span>`));
    else if (v) chips.push(`<span class="chip">${k === "start" ? "From" : "To"}: <b>${monthName(v)}</b><button data-rm="${k}|" aria-label="remove">×</button></span>`);
  });
  $("#fb-active").innerHTML = chips.length ? chips.join("") + `<button class="linkbtn" id="clear-all">Clear all</button>` : "";
  $$("#fb-active [data-rm]").forEach((b) => b.addEventListener("click", () => { const [k, v] = b.dataset.rm.split("|"); removeFilter(k, v); refresh(); }));
  const c = $("#clear-all"); if (c) c.addEventListener("click", () => { Object.keys(F).forEach((k) => { F[k] = Array.isArray(F[k]) ? [] : null; }); refresh(); });
  $$("#fb-controls .dd").forEach((dd) => { const b = $("button", dd), n = F[dd.dataset.k].length; b.classList.toggle("on", n > 0); b.textContent = FILTER_LABELS[dd.dataset.k] + (n ? ` (${n})` : ""); });
  $("#f-start").value = F.start || ""; $("#f-end").value = F.end || "";
}
const NAMES = {};
function customerName(id) { return NAMES[id] || id; }

let REQ = 0;
async function refresh(doRender = true) {
  const my = ++REQ;
  const v = await post("/api/explore", F);
  if (my !== REQ) return;
  VIEW = v;
  v.customers.forEach((c) => { if (c.name) NAMES[c.customer_id] = c.name; });
  renderActiveChips();
  if (doRender) render();
}

/* =====================================================================
   charts
   ===================================================================== */
function niceTicks(lo, hi, n = 5) {
  if (!(hi > lo)) return [lo];
  const span = hi - lo, raw = span / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= n + 0.5) || mag * 10;
  const out = []; for (let v = Math.ceil(lo / step - 1e-9) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}

/** The classic bridge: list price stepping down to margin. */
function bridgeSvg(s) {
  const items = [
    { k: "List price", v: s.list_value, type: "total", c: "var(--s4)", t: "What the orders would have been worth at your published list prices." },
    { k: "Negotiated away", v: -s.negotiated_off_list, type: "step", c: "var(--neg)", t: "Price agreed below list in negotiation, before the invoice." },
    { k: "Invoice discounts", v: -s.on_invoice_discount, type: "step", c: "var(--inv)", t: "Discounts printed on the invoice: payment terms, promotions." },
    { k: "Rebates afterwards", v: -s.off_invoice_leakage, type: "step", c: "var(--off)", t: "Money paid back after the invoice: rebates, bonuses, allowances. Easy to forget, often large." },
    { k: "Pocket revenue", v: s.pocket_price, type: "total", c: "var(--accent)", t: "What actually reaches your pocket after every discount." },
    { k: "Cost of goods", v: -s.cogs, type: "step", c: "var(--cogs)", t: "What the goods cost you." },
    { k: "Your margin", v: s.pocket_margin, type: "total", c: "var(--good)", t: "What you keep." },
  ];
  const W = 700, H = 300, l = 54, r = 10, t = 22, b = 46;
  const max = s.list_value || 1;
  const y = (v) => t + (1 - v / max) * (H - t - b);
  const bw = (W - l - r) / items.length;
  let level = 0;
  const ticks = niceTicks(0, max, 4).map((v) => `<line class="grid" x1="${l}" x2="${W - r}" y1="${y(v)}" y2="${y(v)}"/><text x="${l - 6}" y="${y(v) + 4}" text-anchor="end">${big(v, false)}</text>`).join("");
  const bars = items.map((it, i) => {
    let top, bot;
    if (it.type === "total") { top = it.v; bot = 0; level = it.v; } else { top = level; bot = level + it.v; level = bot; }
    const x = l + i * bw + bw * 0.16, w = bw * 0.68;
    const share = 100 * Math.abs(it.v) / max;
    const tipHtml = `<b>${esc(it.k)}</b>: ${big(Math.abs(it.v))} (${num(share, 1)} of every 100 at list)<br>${esc(it.t)}`;
    const conn = i < items.length - 1 ? `<line x1="${x + w}" x2="${x + w + bw * 0.32}" y1="${y(level)}" y2="${y(level)}" stroke="var(--muted)" stroke-dasharray="2 2"/>` : "";
    const [w1, ...rest] = it.k.split(" ");
    return `<g data-tip="${esc(tipHtml)}"><rect x="${x}" y="${y(Math.max(top, bot))}" width="${w}" height="${Math.max(1.5, Math.abs(y(bot) - y(top)))}" rx="4" fill="${it.c}"/>
      <text x="${x + w / 2}" y="${y(Math.max(top, bot)) - 6}" text-anchor="middle" class="ink bold">${it.type === "step" ? "−" : ""}${big(Math.abs(it.v), false)}</text>
      <text x="${x + w / 2}" y="${H - b + 16}" text-anchor="middle" class="ink">${esc(w1)}</text><text x="${x + w / 2}" y="${H - b + 30}" text-anchor="middle">${esc(rest.join(" "))}</text></g>${conn}`;
  }).join("");
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Price waterfall">${ticks}${bars}</svg>`;
}

/** Of every 100 at list price, where it goes. */
function stripHtml(s) {
  const L = s.list_value || 1;
  const parts = [
    ["Your margin", s.pocket_margin, "var(--good)", "What you keep."],
    ["Cost of goods", s.cogs, "var(--cogs)", "What the goods cost you."],
    ["Negotiated away", s.negotiated_off_list, "var(--neg)", "Agreed below list price."],
    ["Invoice discounts", s.on_invoice_discount, "var(--inv)", "Discounts on the invoice."],
    ["Rebates afterwards", s.off_invoice_leakage, "var(--off)", "Paid back after the invoice."],
  ].map(([k, v, c, t]) => ({ k, v: 100 * v / L, c, t }));
  return `<div class="strip">${parts.map((p) => `<div style="flex-grow:${Math.max(p.v, 0.0001)};background:${p.c}" data-tip="<b>${esc(p.k)}</b>: ${num(p.v, 1)} of every 100 ${CUR}<br>${esc(p.t)}">${p.v >= 5 ? num(p.v, 0) : ""}</div>`).join("")}</div>
    <div class="strip-legend">${parts.map((p) => `<div><i style="background:${p.c}"></i><span><b>${num(p.v, 1)}</b> ${esc(p.k.toLowerCase())}</span></div>`).join("")}</div>`;
}

/** Monthly bars (pocket revenue) with a margin % line. */
function trendSvg(trend, height = 240) {
  if (!trend.length) return `<div class="empty">No orders in this selection.</div>`;
  const W = 760, H = height, l = 54, r = 44, t = 14, b = 34;
  const maxR = Math.max(...trend.map((d) => d.pocket_price)) * 1.1 || 1;
  const ms = trend.map((d) => d.margin_pct).filter((v) => v != null);
  const mLo = Math.floor(Math.min(...ms) - 2), mHi = Math.ceil(Math.max(...ms) + 2);
  const bw = (W - l - r) / trend.length;
  const x = (i) => l + i * bw + bw / 2;
  const y = (v) => t + (1 - v / maxR) * (H - t - b);
  const ym = (v) => t + (1 - (v - mLo) / (mHi - mLo)) * (H - t - b);
  const grid = niceTicks(0, maxR, 4).map((v) => `<line class="grid" x1="${l}" x2="${W - r}" y1="${y(v)}" y2="${y(v)}"/><text x="${l - 6}" y="${y(v) + 4}" text-anchor="end">${big(v, false)}</text>`).join("");
  const mt = niceTicks(mLo, mHi, 4).map((v) => `<text x="${W - r + 6}" y="${ym(v) + 4}" style="fill:var(--good)">${num(v, 0)}%</text>`).join("");
  const bars = trend.map((d, i) => `<rect x="${x(i) - bw * 0.36}" y="${y(d.pocket_price)}" width="${bw * 0.72}" height="${H - b - y(d.pocket_price)}" rx="2" fill="var(--accent)" opacity=".75" data-tip="<b>${monthName(d.key)}</b><br>Collected ${big(d.pocket_price)}<br>Margin ${pct(d.margin_pct)} · ${num(d.orders)} orders<br>Given away ${pct(d.given_away_pct)} of list"/>`).join("");
  const line = `<polyline fill="none" stroke="var(--good)" stroke-width="2.5" points="${trend.map((d, i) => `${x(i)},${ym(d.margin_pct)}`).join(" ")}"/>` + trend.map((d, i) => `<circle cx="${x(i)}" cy="${ym(d.margin_pct)}" r="3" fill="var(--good)"/>`).join("");
  const every = Math.ceil(trend.length / 10);
  const xl = trend.map((d, i) => (i % every === 0 ? `<text x="${x(i)}" y="${H - b + 16}" text-anchor="middle">${monthName(d.key)}</text>` : "")).join("");
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Monthly trend">${grid}${bars}${line}${mt}${xl}</svg>
    <div class="legend"><span><i style="background:var(--accent);opacity:.75"></i>collected per month (left)</span><span><i class="line" style="background:var(--good)"></i>margin % (right)</span></div>`;
}

const METRICS = {
  margin_pct: { label: "Margin %", f: (r) => r.margin_pct, fmt: (v) => pct(v), good: "high", book: (s) => s.margin_pct, explain: "Share of the money collected that you keep after the cost of goods." },
  kept_pct: { label: "Price kept %", f: (r) => r.kept_pct, fmt: (v) => pct(v), good: "high", book: (s) => s.kept_pct, explain: "Share of the list price that actually reaches your pocket." },
  given_away_pct: { label: "Given away %", f: (r) => r.given_away_pct, fmt: (v) => pct(v), good: "low", book: (s) => s.given_away_pct, explain: "Share of the list price lost to negotiation, discounts and rebates." },
  pocket_price: { label: "Revenue", f: (r) => r.pocket_price, fmt: (v) => big(v), good: "high", book: null, explain: "What was actually collected." },
  pocket_margin: { label: "Margin amount", f: (r) => r.pocket_margin, fmt: (v) => big(v), good: "high", book: null, explain: "Margin in money terms." },
};
const PRODUCT_METRICS = {
  margin_pct: { label: "Margin %", f: (r) => r.margin_pct, fmt: (v) => pct(v), good: "high", explain: "Product margin after negotiation, before order-level discounts and rebates." },
  given_away_pct: { label: "Negotiated %", f: (r) => r.negotiated_pct, fmt: (v) => pct(v), good: "low", explain: "Share of list price negotiated away on this product." },
  pocket_price: { label: "Revenue", f: (r) => r.revenue, fmt: (v) => big(v), good: "high", explain: "Revenue after negotiation." },
  pocket_margin: { label: "Margin amount", f: (r) => r.margin, fmt: (v) => big(v), good: "high", explain: "Margin in money terms." },
};

/** Horizontal bars, sorted, clickable to filter. */
function barsSvg(rows, metric, filterKey, bookValue, colorFn) {
  const sorted = rows.filter((r) => metric.f(r) != null).sort((a, b) => metric.f(b) - metric.f(a));
  if (!sorted.length) return `<div class="empty">Nothing to show for this selection.</div>`;
  const vals = sorted.map((r) => metric.f(r));
  const list = sorted.slice(0, 18);
  const rowH = 30, l = 170, rr = 80, W = 700, H = list.length * rowH + 16;
  const isPct = metric.label.includes("%");
  const lo = isPct ? Math.max(0, Math.min(...vals, bookValue ?? Infinity) - 5) : 0;
  const hi = Math.max(...vals, bookValue ?? -Infinity) * (isPct ? 1.03 : 1.05);
  const x = (v) => l + ((v - lo) / (hi - lo || 1)) * (W - l - rr);
  const avg = bookValue != null ? `<line x1="${x(bookValue)}" x2="${x(bookValue)}" y1="0" y2="${H - 8}" stroke="var(--ink)" stroke-dasharray="4 3" opacity=".6"/><text x="${x(bookValue) + 4}" y="${H + 4}" class="ink">selection average ${metric.fmt(bookValue)}</text>` : "";
  const bars = list.map((r, i) => {
    const v = metric.f(r), yy = 6 + i * rowH;
    const better = bookValue == null ? null : metric.good === "high" ? v >= bookValue : v <= bookValue;
    const c = colorFn ? colorFn(r) : better == null || better ? "var(--accent)" : "var(--neg)";
    const label = r.name && r.name !== r.key ? r.name : r.key;
    const tipH = `<b>${esc(label)}</b><br>${esc(metric.label)}: ${metric.fmt(v)}<br>Revenue ${big(r.pocket_price ?? r.revenue)}${r.orders != null ? ` · ${num(r.orders)} orders` : ""}${r.customers != null ? ` · ${num(r.customers)} customers` : ""}${filterKey ? "<br><i>Click to focus on this</i>" : ""}`;
    const attrs = filterKey ? `class="hit" data-filter='${esc(JSON.stringify({ [filterKey]: [r.key] }))}'` : "";
    return `<g ${attrs} data-tip="${esc(tipH)}"><text x="${l - 10}" y="${yy + 18}" text-anchor="end" class="ink">${esc(label.length > 26 ? label.slice(0, 25) + "…" : label)}</text>
      <rect x="${l}" y="${yy + 5}" width="${Math.max(2, x(v) - l)}" height="${rowH - 11}" rx="4" fill="${c}" opacity=".85"/>
      <text x="${x(v) + 6}" y="${yy + 18}" class="ink bold">${metric.fmt(v)}</text></g>`;
  }).join("");
  return `<svg viewBox="0 0 ${W} ${H + 10}" role="img">${avg}${bars}</svg>${sorted.length > list.length ? `<p class="tiny muted">Top ${list.length} of ${sorted.length}; all of them are in the table below.</p>` : ""}`;
}

/** Channel x region heatmap of margin %. */
function heatmapSvg(hm, book) {
  if (!hm.cells.length) return `<div class="empty">No orders in this selection.</div>`;
  const cw = 64, ch = 34, l = 120, t = 78;
  const W = l + hm.regions.length * cw + 10, H = t + hm.channels.length * ch + 10;
  const vals = hm.cells.map((c) => c.margin_pct);
  const spread = Math.max(3, ...vals.map((v) => Math.abs(v - book)));
  const color = (v) => {
    const d = Math.max(-1, Math.min(1, (v - book) / spread));
    return d >= 0 ? `color-mix(in srgb, var(--accent) ${Math.round(15 + 70 * d)}%, var(--card))` : `color-mix(in srgb, var(--neg) ${Math.round(15 + 70 * -d)}%, var(--card))`;
  };
  const cellMap = Object.fromEntries(hm.cells.map((c) => [c.channel + "|" + c.region, c]));
  const heads = hm.regions.map((r, j) => `<text transform="translate(${l + j * cw + cw / 2},${t - 8}) rotate(-35)" class="ink">${esc(r.length > 14 ? r.slice(0, 13) + "…" : r)}</text>`).join("");
  const rows = hm.channels.map((c, i) => `<text x="${l - 10}" y="${t + i * ch + ch / 2 + 4}" text-anchor="end" class="ink">${esc(c)}</text>` + hm.regions.map((r, j) => {
    const cell = cellMap[c + "|" + r];
    if (!cell) return `<rect x="${l + j * cw + 2}" y="${t + i * ch + 2}" width="${cw - 4}" height="${ch - 4}" rx="5" fill="var(--soft)"/>`;
    return `<g class="hit" data-filter='${esc(JSON.stringify({ channel: [c], region: [r] }))}' data-tip="<b>${esc(c)} · ${esc(r)}</b><br>Margin ${pct(cell.margin_pct)} (selection ${pct(book)})<br>Revenue ${big(cell.revenue)} · ${num(cell.orders)} orders<br><i>Click to focus</i>">
      <rect x="${l + j * cw + 2}" y="${t + i * ch + 2}" width="${cw - 4}" height="${ch - 4}" rx="5" fill="${color(cell.margin_pct)}"/>
      <text x="${l + j * cw + cw / 2}" y="${t + i * ch + ch / 2 + 4}" text-anchor="middle" class="ink bold">${num(cell.margin_pct, 1)}</text></g>`;
  }).join("")).join("");
  return `<div class="tablewrap"><svg viewBox="0 0 ${W} ${H}" style="min-width:${Math.min(W, 640)}px" role="img">${heads}${rows}</svg></div>
    <div class="legend"><span><i style="background:color-mix(in srgb, var(--accent) 80%, var(--card))"></i>above the ${pct(book)} average</span><span><i style="background:color-mix(in srgb, var(--neg) 80%, var(--card))"></i>below it</span><span><i style="background:var(--soft)"></i>no orders</span></div>`;
}

/** Customer value map: revenue (log) against margin %. */
function scatterSvg(cust) {
  const pts = cust.filter((c) => c.revenue > 0 && c.margin_pct != null);
  if (!pts.length) return { svg: `<div class="empty">No customers in this selection.</div>`, mx: 0, my: 0 };
  const W = 760, H = 380, l = 50, r = 16, t = 16, b = 40;
  const lx = pts.map((p) => Math.log10(p.revenue));
  let xLo = Math.floor(Math.min(...lx) * 2) / 2, xHi = Math.ceil(Math.max(...lx) * 2) / 2; if (xHi === xLo) xHi += 0.5;
  const ys = pts.map((p) => p.margin_pct);
  const yLo = Math.floor(Math.min(...ys) - 2), yHi = Math.ceil(Math.max(...ys) + 2);
  const x = (v) => l + ((Math.log10(v) - xLo) / (xHi - xLo)) * (W - l - r);
  const y = (v) => t + (1 - (v - yLo) / (yHi - yLo || 1)) * (H - t - b);
  const med = (a) => { const s = [...a].sort((p, q) => p - q); return s[Math.floor(s.length / 2)]; };
  const mx = med(pts.map((p) => p.revenue)), my = med(ys);
  const maxO = Math.max(...pts.map((p) => p.orders));
  const xt = []; for (let e = Math.ceil(xLo); e <= xHi; e++) xt.push(Math.pow(10, e));
  const grid = xt.map((v) => `<line class="grid" x1="${x(v)}" x2="${x(v)}" y1="${t}" y2="${H - b}"/><text x="${x(v)}" y="${H - b + 15}" text-anchor="middle">${big(v, false)}</text>`).join("")
    + niceTicks(yLo, yHi, 5).map((v) => `<line class="grid" x1="${l}" x2="${W - r}" y1="${y(v)}" y2="${y(v)}"/><text x="${l - 6}" y="${y(v) + 4}" text-anchor="end">${num(v, 0)}%</text>`).join("");
  const quad = `<line x1="${x(mx)}" x2="${x(mx)}" y1="${t}" y2="${H - b}" stroke="var(--muted)" stroke-dasharray="5 4"/><line x1="${l}" x2="${W - r}" y1="${y(my)}" y2="${y(my)}" stroke="var(--muted)" stroke-dasharray="5 4"/>
    <text x="${W - r - 6}" y="${t + 14}" text-anchor="end" class="bold" style="fill:var(--good)">PROTECT · big and profitable</text>
    <text x="${W - r - 6}" y="${H - b - 8}" text-anchor="end" class="bold" style="fill:var(--neg)">FIX PRICING · big, thin margin</text>
    <text x="${l + 6}" y="${t + 14}" class="bold" style="fill:var(--accent)">GROW · small, profitable</text>
    <text x="${l + 6}" y="${H - b - 8}" class="bold">REVIEW · small, thin margin</text>`;
  const dots = pts.map((p) => `<circle class="hit" cx="${x(p.revenue)}" cy="${y(p.margin_pct)}" r="${2.2 + 4.5 * Math.sqrt(p.orders / maxO)}" fill="${segColor(p.segment)}" fill-opacity=".5" stroke="${segColor(p.segment)}" data-customer="${esc(p.customer_id)}" data-tip="<b>${esc(p.name || p.customer_id)}</b><br>${esc(p.segment)} · ${esc(p.customer_type)}<br>Revenue ${big(p.revenue)} · margin ${pct(p.margin_pct)}<br>${num(p.orders)} orders · gives away ${pct(p.given_away_pct)} of list<br><i>Click for the full profile</i>"/>`).join("");
  return { svg: `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Customer value map">${grid}${quad}${dots}<text x="${(W + l) / 2}" y="${H - 4}" text-anchor="middle">revenue collected per customer (log scale)</text></svg>`, mx, my };
}

function sparkSvg(trend, key = "pocket_price") {
  if (!trend.length) return "";
  const W = 560, H = 90, vals = trend.map((d) => d[key]), max = Math.max(...vals) || 1;
  const x = (i) => 6 + (i / Math.max(1, trend.length - 1)) * (W - 12), y = (v) => H - 14 - (v / max) * (H - 24);
  return `<svg viewBox="0 0 ${W} ${H}">${trend.map((d, i) => `<rect x="${x(i) - 4}" y="${y(d[key])}" width="8" height="${H - 14 - y(d[key])}" rx="2" fill="var(--accent)" opacity=".7" data-tip="<b>${monthName(d.key)}</b>: ${big(d[key])} · margin ${pct(d.margin_pct)}"/>`).join("")}<text x="4" y="${H - 1}">${monthName(trend[0].key)}</text><text x="${W - 4}" y="${H - 1}" text-anchor="end">${monthName(trend[trend.length - 1].key)}</text></svg>`;
}

/* =====================================================================
   pages
   ===================================================================== */
function kpi(label, value, sub = "", cls = "") { return `<div class="kpi ${cls}"><div class="l">${label}</div><div class="v">${value}</div><div class="s">${sub}</div></div>`; }
function vsBook(v, b, good = "high", d = 1) {
  if (!anyFilter() || v == null || b == null) return "";
  const diff = v - b; if (Math.abs(diff) < 0.05) return ` · same as all sales`;
  const better = good === "high" ? diff > 0 : diff < 0;
  return ` · <span class="${better ? "up" : "down"}">${signed(diff, (z) => num(z, d))} pts</span> vs all sales`;
}
function scopeLine() {
  const s = VIEW.summary;
  return anyFilter() ? `Showing <b>${num(s.orders)}</b> orders from <b>${num(s.customers)}</b> customers in your selection.` : `All <b>${num(s.orders)}</b> orders from <b>${num(s.customers)}</b> customers, ${monthName(OPT.months[0])} to ${monthName(OPT.months[OPT.months.length - 1])}.`;
}

/* --------------------------------------------------------------- overview */
function renderOverview() {
  const s = VIEW.summary, B = VIEW.book;
  if (!s.orders) { $("#page-overview").innerHTML = `<div class="card empty">No orders match this selection. Remove a filter above.</div>`; return; }
  const keep = s.list_value ? 100 * s.pocket_margin / s.list_value : null;
  $("#page-overview").innerHTML = `
    <section class="hero"><h1>Of every <b>100 ${CUR}</b> on your price list, you keep <b>${num(keep, 0)} ${CUR}</b> as margin.</h1>
      <p>${scopeLine()} Use the filters above, or click any bar, cell or dot, to focus on a channel, region, customer group or period. Every page follows.</p></section>
    <div class="kpis">
      ${kpi("Worth at list price", big(s.list_value), `${num(s.orders)} orders`)}
      ${kpi("Actually collected", big(s.pocket_price), `${pct(s.kept_pct)} of list${vsBook(s.kept_pct, B.kept_pct)}`, "accent")}
      ${kpi("Given away", big(s.given_away), `${pct(s.given_away_pct)} of list${vsBook(s.given_away_pct, B.given_away_pct, "low")}`, "bad")}
      ${kpi("Margin", big(s.pocket_margin), `${pct(s.margin_pct)} of what was collected${vsBook(s.margin_pct, B.margin_pct)}`, "good")}
    </div>
    <div class="card"><div class="card-head"><div><h2>Where every 100 ${CUR} goes</h2><p>Hover a block for what it means.</p></div></div>${stripHtml(s)}</div>
    <div class="row c21">
      <div class="card"><div class="card-head"><div><h2>From list price to margin</h2><p>Each step is money that left between the price list and your pocket. Hover for details.</p></div></div>${bridgeSvg(s)}</div>
      <div class="card"><div class="card-head"><div><h2>What stands out</h2><p>Found automatically in the numbers.</p></div></div>
        <div class="insights" style="grid-template-columns:1fr">${VIEW.insights.map((i) => `<div class="insight ${i.tone}"><b>${esc(i.title)}</b><p>${esc(i.text)}</p>${i.filter ? `<button data-filter='${esc(JSON.stringify(i.filter))}'>Focus on this →</button>` : ""}${i.tab ? `<button data-go="${i.tab}">See the opportunity →</button>` : ""}</div>`).join("")}</div></div>
    </div>
    <div class="card"><div class="card-head"><div><h2>Month by month</h2><p>Money collected and the margin kept on it.</p></div></div>${trendSvg(VIEW.trend)}</div>`;
}

/* --------------------------------------------------------------- explorer */
const DIM_TABS = [["channel", "Channel"], ["region", "Region"], ["customer_type", "Customer type"], ["segment", "Buying behaviour"], ["industry", "Industry"], ["account_class", "Account class"], ["families", "Product family"], ["products", "Product"]];
function renderExplore() {
  const isProduct = UI.dim === "families" || UI.dim === "products";
  const metrics = isProduct ? PRODUCT_METRICS : METRICS;
  if (!metrics[UI.metric]) UI.metric = "margin_pct";
  const m = metrics[UI.metric];
  const rows = isProduct ? VIEW.products[UI.dim] : VIEW.breakdowns[UI.dim];
  const book = isProduct ? null : m.book ? m.book(VIEW.summary) : null;
  const filterKey = isProduct ? null : UI.dim;
  const colorFn = UI.dim === "segment" ? (r) => segColor(r.key) : null;
  const dimLabel = DIM_TABS.find((d) => d[0] === UI.dim)[1];
  const valid = rows.filter((r) => m.f(r) != null);
  const best = [...valid].sort((a, b) => (m.good === "high" ? m.f(b) - m.f(a) : m.f(a) - m.f(b)));
  let gapCard = "";
  if (!isProduct && UI.metric.endsWith("pct") && best.length >= 2) {
    const top = best[0], w = best[best.length - 1], gap = Math.abs(m.f(top) - m.f(w));
    const base = UI.metric === "margin_pct" ? w.pocket_price : w.list_value;
    gapCard = `<div class="insight info mt"><b>What closing the gap is worth</b><p>Bringing ${esc(w.key)} level with ${esc(top.key)} on ${esc(m.label.toLowerCase())} is worth about ${big(base * gap / 100)} on these sales.</p></div>`;
  }
  const table = isProduct
    ? `<table><thead><tr><th>${dimLabel}</th><th class="num">Revenue</th><th class="num">Margin</th><th class="num">Margin %</th><th class="num">Negotiated %</th><th class="num">Units</th><th class="num">Customers</th></tr></thead><tbody>
        ${rows.map((r) => `<tr><td>${esc(r.name)}</td><td class="num">${big(r.revenue)}</td><td class="num">${big(r.margin)}</td><td class="num">${pct(r.margin_pct)}</td><td class="num">${pct(r.negotiated_pct)}</td><td class="num">${num(r.quantity)}</td><td class="num">${num(r.customers)}</td></tr>`).join("")}</tbody></table>`
    : `<table><thead><tr><th>${dimLabel}</th><th class="num">Orders</th><th class="num">Customers</th><th class="num">At list</th><th class="num">Collected</th><th class="num">Negotiated</th><th class="num">Invoice disc.</th><th class="num">Rebates</th><th class="num">Price kept</th><th class="num">Margin</th><th class="num">Margin %</th></tr></thead><tbody>
        ${rows.map((r) => `<tr class="click" data-filter='${esc(JSON.stringify({ [UI.dim]: [r.key] }))}'><td>${UI.dim === "segment" ? `<span class="dot" style="background:${segColor(r.key)}"></span>` : ""}${esc(r.key)}</td><td class="num">${num(r.orders)}</td><td class="num">${num(r.customers)}</td><td class="num">${big(r.list_value)}</td><td class="num">${big(r.pocket_price)}</td><td class="num">${big(r.negotiated_off_list)}</td><td class="num">${big(r.on_invoice_discount)}</td><td class="num">${big(r.off_invoice_leakage)}</td><td class="num">${pct(r.kept_pct)}</td><td class="num">${big(r.pocket_margin)}</td><td class="num"><b>${pct(r.margin_pct)}</b></td></tr>`).join("")}</tbody></table>`;

  $("#page-explore").innerHTML = `
    <section class="hero"><h1>Margin explorer</h1><p>${scopeLine()} Pick how to slice it and what to measure. Click a bar or a row to drill in: every page follows your selection.</p></section>
    <div class="card">
      <div class="card-head"><div class="seg">${DIM_TABS.map(([k, l]) => `<button class="${UI.dim === k ? "on" : ""}" data-dim="${k}">${l}</button>`).join("")}</div>
        <div class="seg">${Object.entries(metrics).map(([k, v]) => `<button class="${UI.metric === k ? "on" : ""}" data-metric="${k}">${v.label}</button>`).join("")}</div></div>
      <p class="small muted">${esc(m.explain)}${isProduct ? " Discounts and rebates are recorded per order, not per product, so product figures stop at the negotiated price rather than split order-level money across products by guesswork." : ""}</p>
      <div class="row c21" style="margin-top:8px">
        <div>${barsSvg(rows, m, filterKey, book, colorFn)}</div>
        <div>${best.length >= 2 ? `<div class="insight good"><b>Best ${esc(dimLabel.toLowerCase())}</b><p>${esc(best[0].name || best[0].key)}: ${m.fmt(m.f(best[0]))}</p></div>
            <div class="insight warn mt"><b>Weakest</b><p>${esc(best[best.length - 1].name || best[best.length - 1].key)}: ${m.fmt(m.f(best[best.length - 1]))}</p></div>${gapCard}`
          : `<p class="muted small">Only one ${esc(dimLabel.toLowerCase())} in this selection.</p>`}</div>
      </div>
    </div>
    ${!isProduct ? `<div class="card"><div class="card-head"><div><h2>Margin by channel and region</h2><p>Each cell is the margin % for one channel in one region. Click a cell to focus on it.</p></div></div>${heatmapSvg(VIEW.heatmap, VIEW.summary.margin_pct)}</div>` : ""}
    <div class="card"><div class="card-head"><div><h2>All ${esc(dimLabel.toLowerCase())} figures</h2><p>${isProduct ? "" : "Click a row to focus on it."}</p></div></div><div class="tablewrap tall">${table}</div></div>`;
}

/* --------------------------------------------------------------- customers */
function renderCustomers() {
  const cust = VIEW.customers;
  const segs = {};
  cust.forEach((c) => { const s = segs[c.segment] || (segs[c.segment] = { n: 0, revenue: 0 }); s.n++; s.revenue += c.revenue; });
  const total = cust.reduce((a, c) => a + c.revenue, 0) || 1;
  const segBreak = Object.fromEntries((VIEW.breakdowns.segment || []).map((r) => [r.key, r]));
  const cards = SEG_ORDER.filter((s) => segs[s]).map((s) => {
    const info = OPT.segments[s] || {}, b = segBreak[s] || {};
    return `<div class="segcard ${F.segment.includes(s) ? "on" : ""}" style="border-top-color:${segColor(s)}" data-filter='${esc(JSON.stringify({ segment: [s] }))}'>
      <h2>${esc(s)}</h2><p>${esc(info.story || "")}</p>
      <div class="nums"><div><b>${num(segs[s].n)}</b>customers</div><div><b>${num(100 * segs[s].revenue / total, 0)}%</b>of revenue</div><div><b>${pct(b.margin_pct)}</b>margin</div></div>
      <p class="tiny mt mb0 muted">Gives away ${pct(b.given_away_pct)} of list price</p></div>`;
  }).join("");
  const sc = scatterSvg(cust);
  const q = { protect: 0, fix: 0, grow: 0, review: 0 };
  cust.forEach((c) => { if (c.margin_pct == null) return; const hiR = c.revenue >= sc.mx, hiM = c.margin_pct >= sc.my; q[hiR ? (hiM ? "protect" : "fix") : (hiM ? "grow" : "review")]++; });
  const fixList = cust.filter((c) => c.revenue >= sc.mx && c.margin_pct < sc.my).sort((a, b) => b.revenue - a.revenue).slice(0, 6);

  $("#page-customers").innerHTML = `
    <section class="hero"><h1>Your customers, grouped by how they buy</h1><p>${scopeLine()} The groups are learned from behaviour (how much of list they pay, how hard they negotiate, how often and how broadly they buy), not from size labels. Click a group to focus on it.</p></section>
    <div class="segcards">${cards}</div>
    <div class="row c21 mt">
      <div class="card"><div class="card-head"><div><h2>Customer value map</h2><p>Every dot is a customer: further right = more revenue, higher = better margin, bigger = more orders. Click a dot to open the profile.</p></div></div>${sc.svg}
        <div class="legend">${SEG_ORDER.filter((s) => segs[s]).map((s) => `<span><i style="background:${segColor(s)}"></i>${esc(s)}</span>`).join("")}</div></div>
      <div class="card"><h2>What to do with each corner</h2>
        <ul class="checks mt">
          <li><span class="badge good">${q.protect}</span><span><b>Protect</b>: large and profitable. Keep them happy, hold price.</span></li>
          <li><span class="badge bad">${q.fix}</span><span><b>Fix pricing</b>: large but thin margin. This is where price discipline pays most.</span></li>
          <li><span class="badge info">${q.grow}</span><span><b>Grow</b>: profitable but small. Sell them more.</span></li>
          <li><span class="badge warn">${q.review}</span><span><b>Review</b>: small and thin. Check terms and cost to serve.</span></li>
        </ul>
        <h3 class="mt">Largest "fix pricing" customers</h3>
        ${fixList.map((c) => `<div class="small" style="display:flex;justify-content:space-between;gap:8px;padding:5px 0;border-bottom:1px solid var(--line);cursor:pointer" data-customer="${esc(c.customer_id)}"><span>${esc(c.name || c.customer_id)}</span><span class="muted">${big(c.revenue)} · ${pct(c.margin_pct)}</span></div>`).join("") || `<p class="small muted">None in this selection.</p>`}
      </div>
    </div>
    <div class="card"><div class="card-head"><div><h2>All customers</h2><p>Click a customer for the full profile and recommended prices.</p></div><input class="search" id="cust-q" placeholder="Search name, ID, group…" value="${esc(UI.custQuery)}"></div>
      <div class="tablewrap tall" id="cust-table"></div></div>`;
  renderCustomerTable();
  $("#cust-q").addEventListener("input", (e) => { UI.custQuery = e.target.value; renderCustomerTable(); });
}
function renderCustomerTable() {
  const q = UI.custQuery.toLowerCase();
  const cols = [["name", "Customer"], ["segment", "Buying behaviour"], ["customer_type", "Type"], ["channel", "Main channel"], ["region", "Region"], ["orders", "Orders", 1], ["revenue", "Revenue", 1], ["given_away_pct", "Given away", 1], ["margin_pct", "Margin %", 1]];
  let rows = VIEW.customers.filter((c) => !q || `${c.name} ${c.customer_id} ${c.segment} ${c.customer_type} ${c.region}`.toLowerCase().includes(q));
  rows = [...rows].sort((a, b) => { const x = a[UI.custSort], y = b[UI.custSort]; const r = typeof x === "string" ? String(x).localeCompare(y) : (x ?? -1e18) - (y ?? -1e18); return UI.custDesc ? -r : r; });
  const max = Math.max(...rows.map((r) => r.revenue), 1);
  $("#cust-table").innerHTML = `<table><thead><tr>${cols.map(([k, l, n]) => `<th class="sort ${n ? "num" : ""}" data-sort="${k}">${l}${UI.custSort === k ? (UI.custDesc ? " ▾" : " ▴") : ""}</th>`).join("")}</tr></thead><tbody>
    ${rows.slice(0, 300).map((c) => `<tr class="click" data-customer="${esc(c.customer_id)}"><td><b>${esc(c.name || c.customer_id)}</b><div class="tiny muted">${esc(c.customer_id)}</div></td><td><span class="dot" style="background:${segColor(c.segment)}"></span>${esc(c.segment)}</td><td>${esc(c.customer_type)}</td><td>${esc(c.channel)}</td><td>${esc(c.region)}</td><td class="num">${num(c.orders)}</td><td class="num"><span class="minibar" style="width:${Math.round(50 * c.revenue / max)}px"></span>${big(c.revenue)}</td><td class="num">${pct(c.given_away_pct)}</td><td class="num"><b>${pct(c.margin_pct)}</b></td></tr>`).join("")}</tbody></table>
    ${rows.length > 300 ? `<p class="tiny muted">Showing 300 of ${rows.length}. Search or filter to narrow down.</p>` : ""}`;
  $$("#cust-table th.sort").forEach((th) => th.addEventListener("click", () => { const k = th.dataset.sort; if (UI.custSort === k) UI.custDesc = !UI.custDesc; else { UI.custSort = k; UI.custDesc = true; } renderCustomerTable(); }));
}

/* --------------------------------------------------------------- customer drawer */
async function openCustomer(id) {
  $("#drawer-body").innerHTML = `<p class="muted">Loading…</p>`;
  $("#drawer").classList.add("open"); $("#scrim").hidden = false;
  let c; try { c = await api("/api/customer/" + encodeURIComponent(id)); } catch (e) { $("#drawer-body").innerHTML = `<div class="error">${esc(e.message)}</div>`; return; }
  const s = c.summary;
  const recs = c.recommendations.filter((r) => r.status === "complete");
  const maxP = Math.max(...c.products.map((p) => p.revenue), 1);
  $("#drawer-body").innerHTML = `
    <p class="tiny muted mb0">${esc(c.customer_id)} · ${esc(c.customer_type)} · ${esc(c.industry)} · ${esc(c.city ? c.city + ", " : "")}${esc(c.region)}</p>
    <h1>${esc(c.name || c.customer_id)}</h1>
    <p><span class="badge" style="background:color-mix(in srgb, ${segColor(c.segment)} 18%, transparent);color:${segColor(c.segment)}">${esc(c.segment)}</span> <span class="small muted">${esc(c.segment_story)}</span></p>
    <div class="kpis mt">
      ${kpi("Revenue collected", big(s.pocket_price), `${num(s.orders)} orders`)}
      ${kpi("Margin", pct(s.margin_pct), big(s.pocket_margin), s.margin_pct >= VIEW.book.margin_pct ? "good" : "bad")}
      ${kpi("Given away", pct(s.given_away_pct), `${big(s.given_away)} of list value`)}
      ${kpi("Quotes won", c.quotes && c.quotes.quotes ? `${c.quotes.won} / ${c.quotes.quotes}` : "–", c.quotes && c.quotes.quotes ? `${pct(100 * c.quotes.won / c.quotes.quotes, 0)} win rate` : "no quotes on record")}
    </div>
    <div class="card"><h3>Where their 100 ${CUR} goes</h3>${stripHtml(s)}</div>
    <div class="card"><h3>Monthly revenue</h3>${sparkSvg(c.trend)}</div>
    <div class="card"><h3>Recommended prices</h3>
      ${recs.length ? `<table><thead><tr><th>Product</th><th class="num">Today</th><th class="num">Recommended</th><th class="num">Change</th><th></th></tr></thead><tbody>
        ${recs.map((r) => `<tr><td>${esc(r.product_id)}</td><td class="num">${money(r.current_price)}</td><td class="num"><b>${money(r.mid)}</b></td><td class="num"><span class="badge ${r.uplift_pct >= 0 ? "good" : "info"}">${signed(r.uplift_pct, (z) => num(z, 1))}%</span></td><td><button class="linkbtn" data-advise="${esc(c.customer_id)}|${esc(r.product_id)}">Open →</button></td></tr>`).join("")}</tbody></table>`
        : `<p class="small muted">No price recommendation for this customer${c.recommendations.length ? ": " + esc(c.recommendations[0].note) : " yet: this demo prices the most-traded customer-product pairs"}.</p>`}</div>
    <div class="card"><h3>What they buy</h3><div class="tablewrap tall"><table><thead><tr><th>Product</th><th class="num">Revenue</th><th class="num">Units</th><th class="num">Typical price</th><th class="num">Margin %</th></tr></thead><tbody>
      ${c.products.map((p) => `<tr><td>${esc(p.name)}<div class="tiny muted">${esc(p.product_id)}</div></td><td class="num"><span class="minibar" style="width:${Math.round(40 * p.revenue / maxP)}px"></span>${big(p.revenue)}</td><td class="num">${num(p.quantity)}</td><td class="num">${money(p.avg_price)}</td><td class="num">${pct(p.margin_pct)}</td></tr>`).join("")}</tbody></table></div>
      <p class="tiny muted">Product margin is after negotiation, before order-level discounts and rebates.</p></div>
    <p><button class="linkbtn" data-filter='${esc(JSON.stringify({ customer: [c.customer_id] }))}'>Show only this customer on every page →</button></p>`;
}
function closeDrawer() { $("#drawer").classList.remove("open"); $("#scrim").hidden = true; }
$("#drawer-close").addEventListener("click", closeDrawer);
$("#scrim").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

/* --------------------------------------------------------------- price advisor */
const segName = (label) => { for (const [n, v] of Object.entries(OPT.segments)) if (v.label === label) return n; return label; };
async function renderAdvisor() {
  const by = {};
  PAIRS.forEach((p) => { (by[p.customer_id] = by[p.customer_id] || []).push(p); });
  const ids = Object.keys(by).sort((a, b) => customerName(a).localeCompare(customerName(b)));
  if (!ADV.customer || !by[ADV.customer]) {
    const first = PAIRS.find((p) => p.status === "complete" && p.uplift_pct > 0) || PAIRS[0];
    ADV.customer = first.customer_id; ADV.product = first.product_id;
  }
  if (!by[ADV.customer].some((p) => p.product_id === ADV.product)) ADV.product = (by[ADV.customer].find((p) => p.status === "complete") || by[ADV.customer][0]).product_id;
  if (!ADV.rules) ADV.rules = { ...OV.optimiser.rules };
  $("#page-advisor").innerHTML = `
    <section class="hero"><h1>Price advisor</h1><p>Pick a customer and a product. AXIOM recommends a price range from the customer's buying behaviour, how customers like them react to price, and thousands of past quotes that were won or lost. Then try your own price and see what would happen.</p></section>
    <div class="card"><div class="advisor-pick">
      <div><label for="adv-c">Customer</label><select id="adv-c">${ids.map((id) => `<option value="${esc(id)}" ${id === ADV.customer ? "selected" : ""}>${esc(customerName(id))}</option>`).join("")}</select></div>
      <div><label for="adv-p">Product</label><select id="adv-p">${by[ADV.customer].map((p) => `<option value="${esc(p.product_id)}" ${p.product_id === ADV.product ? "selected" : ""}>${esc(p.product_id)}${p.status === "complete" ? "" : " (no price yet)"}</option>`).join("")}</select></div>
      <div><button class="linkbtn" data-customer="${esc(ADV.customer)}">Customer profile →</button></div>
    </div></div>
    <div id="adv-body"><div class="card"><p class="muted">Working out the best price…</p></div></div>`;
  $("#adv-c").addEventListener("change", (e) => { ADV.customer = e.target.value; ADV.product = null; ADV.tryPrice = null; renderAdvisor(); });
  $("#adv-p").addEventListener("change", (e) => { ADV.product = e.target.value; ADV.tryPrice = null; renderAdvisor(); });
  await priceAdvisor();
}
async function priceAdvisor() {
  let res;
  try { res = await post("/api/price", { customer_id: ADV.customer, product_id: ADV.product, margin_floor: ADV.rules.margin_floor, min_win_probability: ADV.rules.min_win_probability, min_volume_share: ADV.rules.min_volume_share }); }
  catch (e) { $("#adv-body").innerHTML = `<div class="card"><div class="error">${esc(e.message)}</div></div>`; return; }
  ADV.res = res;
  const r = res.recommendation;
  if (ADV.tryPrice == null) ADV.tryPrice = r.status === "complete" ? r.mid : (r.current_price || (res.curve[Math.floor(res.curve.length / 2)] || {}).price);
  drawAdvisor();
}
function plainReason(t) {
  return String(t)
    .replace(/^the best price was limited by: /, "Held back by: ")
    .replace(/at least cost plus a (\d+)% margin/, "never below cost plus $1%")
    .replace(/chance of winning at least (\d+)%/, "keep at least a $1% chance of winning")
    .replace(/within the range of prices tested in the past/, "stay within prices tested before")
    .replace(/expected volume at least (\d+)% of today's \(segment (.*?) elasticity, most elastic end [-\d.]+\)/g, (m0, p1, p2) => `keep at least ${p1}% of today's volume (based on how ${segName(p2)} react to price)`)
    .replace(/by the segment (.*) elasticity (-?[\d.]+) \[[^\]]*\]/, (m0, p1) => `based on how ${segName(p1)} react to price`)
    .replace(/^no price meets every rule at once: /, "No price meets every guardrail: ");
}
function tryState(price) {
  const res = ADV.res, curve = res.curve, p = (res.trace && res.trace.product) || {};
  if (!curve.length) return null;
  let best = curve[0]; curve.forEach((c) => { if (Math.abs(c.price - price) < Math.abs(best.price - price)) best = c; });
  const broken = [];
  const floor = p.unit_cost != null ? p.unit_cost / (1 - ADV.rules.margin_floor) : null;
  if (floor != null && price < floor - 1e-9) broken.push(`below cost plus ${num(100 * ADV.rules.margin_floor, 0)}%`);
  if (p.list_price != null && price > p.list_price + 1e-9) broken.push("above list price");
  if (best.win_probability < ADV.rules.min_win_probability) broken.push(`wins less than ${num(100 * ADV.rules.min_win_probability, 0)}% of the time`);
  if (best.volume_share_worst_case != null && best.volume_share_worst_case < ADV.rules.min_volume_share) broken.push(`could lose more than ${num(100 * (1 - ADV.rules.min_volume_share), 0)}% of volume`);
  return { c: best, broken, profit: price - (p.unit_cost || 0) };
}
function verdictHtml(st) {
  const r = ADV.res.recommendation;
  if (st.broken.length) return `<span class="verdict bad">Breaks a guardrail: ${esc(st.broken.join(", "))}</span>`;
  if (r.status === "complete" && ADV.tryPrice >= r.low - 1e-6 && ADV.tryPrice <= r.high + 1e-6) return `<span class="verdict good">Inside the recommended range</span>`;
  return `<span class="verdict warn">Allowed, but less profit expected than the recommended range</span>`;
}
function rule(id, label, v, lo, hi, help) {
  return `<div class="rule"><label for="rule-${id}">${label} <b id="rule-${id}-v">${pct(100 * v, 0)}</b></label><input type="range" id="rule-${id}" min="${lo}" max="${hi}" step="0.01" value="${v}"><p>${help}</p></div>`;
}
function drawAdvisor() {
  const res = ADV.res, r = res.recommendation, t = res.trace || {}, curve = res.curve, p = t.product || {};
  const done = r.status === "complete";
  const rec = res.contract_record;
  const lo = curve.length ? curve[0].price : 0, hi = curve.length ? curve[curve.length - 1].price : 1;
  const step = curve.length > 1 ? (hi - lo) / (curve.length - 1) : 1;
  const reasons = (done ? r.confidence_reasons : [r.note]).map(plainReason);
  const seg = t.segment, el = t.elasticity, win = t.win;
  const sName = seg ? segName(seg.label) : null;
  $("#adv-body").innerHTML = `
    <div class="card"><div class="reco">
      <div>${done ? `<h3>Recommended price</h3><div class="big">${num(r.mid, 2)} <small>${CUR} per unit</small></div>
          <div class="range">Quote between <b>${money(r.low)}</b> and <b>${money(r.high)}</b>. Today they pay <b>${money(r.current_price)}</b>${r.uplift_pct != null ? ` <span class="badge ${r.uplift_pct >= 0 ? "good" : "info"}">${signed(r.uplift_pct, (z) => num(z, 1))}%</span>` : ""}.</div>
          <p class="small mt mb0">Expect to win about <b>${pct(100 * r.acceptance, 0)}</b> of quotes at this price, earning <b>${money(r.expected_unit_margin)}</b> per unit quoted on average${r.volume_share_at_mid != null ? `, while keeping about <b>${pct(100 * r.volume_share_at_mid, 0)}</b> of today's volume` : ""}.</p>
          <p class="mt mb0"><span class="verdict ${rec.confidence.band === "high" ? "good" : rec.confidence.band === "medium" ? "warn" : "bad"}">${esc(rec.confidence.band)} confidence</span></p>`
        : `<h3>No price recommended</h3><p>${esc(plainReason(r.note))}</p><p class="small muted">AXIOM says so rather than guess. Try relaxing the guardrails below, or pick another product.</p>`}
        <ul class="why">${(done ? reasons : []).map((z) => `<li>${esc(z)}</li>`).join("")}</ul></div>
      <div class="tryit">${curve.length ? `<h3>Try your own price</h3>
          <div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px"><b style="font-size:1.4rem" id="try-v">${money(ADV.tryPrice)}</b><span class="tiny muted">drag, or click the chart</span></div>
          <input type="range" id="try" min="${lo}" max="${hi}" step="${step}" value="${ADV.tryPrice}" aria-label="Your price">
          <div class="out" id="try-out"></div><div class="mt" id="try-verdict"></div>` : `<p class="small muted">No price curve for this pair.</p>`}</div>
    </div></div>
    ${curve.length ? `<div class="card"><div class="card-head"><div><h2>Higher price, fewer wins: finding the sweet spot</h2><p>Expected profit per unit quoted = profit per unit × chance of winning. It peaks between "too cheap" and "too dear".</p></div></div><div id="adv-chart"></div>
      <div class="legend"><span><i class="line" style="background:var(--accent)"></i>chance of winning</span><span><i class="line" style="background:var(--s3)"></i>expected profit per unit quoted</span><span><i class="line" style="background:var(--purple)"></i>volume kept (cautious estimate)</span><span><i style="background:var(--good);opacity:.3"></i>recommended range</span><span><i style="background:var(--neg);opacity:.15"></i>outside the guardrails</span></div></div>` : ""}
    <div class="card"><h2>Why this price, step by step</h2><div class="steps-why mt">
      <div class="stepwhy"><h4>1 · Who the customer is</h4><b style="color:${segColor(sName)}">${sName ? esc(sName) : "Not grouped yet"}</b><p>${seg ? `${num(seg.n_deals)} past orders. ${esc((OPT.segments[sName] || {}).story || "")}` : "Too few orders to read a habit."}</p></div>
      <div class="stepwhy"><h4>2 · How customers like them react to price</h4><b>${el && el.applies ? `1% dearer → about ${num(Math.abs(el.beta), 1)}% less volume` : "Not used"}</b><p>${el && el.applies ? `Likely between ${num(Math.abs(el.high), 1)}% and ${num(Math.abs(el.low), 1)}%. AXIOM plans on the cautious end.` : esc(el ? el.skipped : "")}</p></div>
      <div class="stepwhy"><h4>3 · What past quotes say</h4><b>${win && win.effect ? `5% dearer → ${num(Math.abs(win.effect.win_change_per_5pct), 1)} pts fewer wins` : "Average behaviour"}</b><p>${win ? `${num(win.quotes_to_customer)} quotes to this customer on record, ${pct(100 * win.won_share, 0)} won.` : "No quotes to this customer on record."}</p></div>
      <div class="stepwhy"><h4>4 · The guardrails</h4><b>${p.unit_cost != null ? `Cost ${money(p.unit_cost)} · list ${money(p.list_price)}` : "–"}</b><p>Never below cost plus ${num(100 * ADV.rules.margin_floor, 0)}%, never above list, keep at least ${num(100 * ADV.rules.min_win_probability, 0)}% wins and ${num(100 * ADV.rules.min_volume_share, 0)}% of volume.</p></div>
    </div>
    <details class="guard"><summary>Adjust the guardrails</summary>
      ${rule("floor", "Minimum margin over cost", ADV.rules.margin_floor, 0, 0.3, "Never quote below cost plus this share.")}
      ${rule("win", "Minimum chance of winning", ADV.rules.min_win_probability, 0, 0.7, "Never quote a price that wins less often than this.")}
      ${rule("vol", "Minimum volume kept", ADV.rules.min_volume_share, 0, 1, "Never quote a price expected to lose more volume than this allows. 0% switches it off.")}
      <button class="linkbtn" id="rules-reset">Back to the standard guardrails</button>
    </details></div>`;
  if (curve.length) { drawAdvisorChart(); updateTry(); }
  const tr = $("#try"); if (tr) tr.addEventListener("input", (e) => { ADV.tryPrice = +e.target.value; updateTry(); drawAdvisorChart(); });
  [["floor", "margin_floor"], ["win", "min_win_probability"], ["vol", "min_volume_share"]].forEach(([id, k]) => {
    const el2 = $("#rule-" + id); if (!el2) return;
    el2.addEventListener("input", (e) => { ADV.rules[k] = +e.target.value; $("#rule-" + id + "-v").textContent = pct(100 * ADV.rules[k], 0); });
    el2.addEventListener("change", () => { ADV.tryPrice = null; priceAdvisor().then(() => { const d = $(".guard"); if (d) d.open = true; }); });
  });
  const rr = $("#rules-reset"); if (rr) rr.addEventListener("click", () => { ADV.rules = { ...OV.optimiser.rules }; ADV.tryPrice = null; priceAdvisor(); });
}
function updateTry() {
  const st = tryState(ADV.tryPrice); if (!st) return;
  $("#try-v").textContent = money(ADV.tryPrice);
  $("#try-out").innerHTML = `<div>Chance of winning<b>${pct(100 * st.c.win_probability, 0)}</b></div><div>Profit per unit<b>${money(st.profit)}</b></div><div>Expected per unit quoted<b>${money(st.c.expected_profit_per_unit)}</b></div><div>Volume kept<b>${st.c.volume_share_worst_case != null ? pct(100 * Math.min(1, st.c.volume_share_worst_case), 0) : "–"}</b></div>`;
  $("#try-verdict").innerHTML = verdictHtml(st);
}
function drawAdvisorChart() {
  const res = ADV.res, curve = res.curve, r = res.recommendation;
  const W = 820, H = 320, l = 52, rr = 56, t = 22, b = 40;
  const x0 = curve[0].price, x1 = curve[curve.length - 1].price;
  const x = (v) => l + ((v - x0) / (x1 - x0 || 1)) * (W - l - rr);
  const y = (v) => t + (1 - v) * (H - t - b);
  const prof = curve.map((c) => c.expected_profit_per_unit);
  const pMax = Math.max(...prof, 0) * 1.12 || 1, pMin = Math.min(0, ...prof);
  const yp = (v) => t + (1 - (v - pMin) / (pMax - pMin)) * (H - t - b);
  let shade = "", s0 = null;
  curve.forEach((c, i) => { const last = i === curve.length - 1; if (!c.allowed && s0 === null) s0 = i; if ((c.allowed || last) && s0 !== null) { const e = c.allowed ? i - 1 : i; shade += `<rect x="${x(curve[s0].price)}" y="${t}" width="${Math.max(2, x(curve[e].price) - x(curve[s0].price))}" height="${H - t - b}" fill="var(--neg)" opacity=".1"/>`; s0 = null; } });
  const band = r.status === "complete" ? `<rect x="${x(r.low)}" y="${t}" width="${Math.max(3, x(r.high) - x(r.low))}" height="${H - t - b}" fill="var(--good)" opacity=".18"/><text x="${x(r.mid)}" y="${t - 7}" text-anchor="middle" class="bold" style="fill:var(--good)">recommended ${num(r.mid, 2)}</text>` : "";
  const today = r.current_price != null && r.current_price >= x0 && r.current_price <= x1 ? `<line x1="${x(r.current_price)}" x2="${x(r.current_price)}" y1="${t}" y2="${H - b}" stroke="var(--ink)" stroke-dasharray="3 3" opacity=".7"/><text x="${x(r.current_price) + 4}" y="${H - b - 6}" class="ink">today</text>` : "";
  const you = `<line x1="${x(ADV.tryPrice)}" x2="${x(ADV.tryPrice)}" y1="${t}" y2="${H - b}" stroke="var(--purple)" stroke-width="2"/><text x="${x(ADV.tryPrice) + 6}" y="${t + 14}" style="fill:var(--purple)" class="bold">you: ${num(ADV.tryPrice, 2)}</text>`;
  const line = (f, c, w = 2.5, dash = "") => `<polyline fill="none" stroke="${c}" stroke-width="${w}" ${dash ? `stroke-dasharray="${dash}"` : ""} points="${curve.map((d) => `${x(d.price)},${f(d)}`).join(" ")}"/>`;
  const hasVol = curve.some((d) => d.volume_share_worst_case != null);
  const xt = niceTicks(x0, x1, 7).map((v) => `<line class="grid" x1="${x(v)}" x2="${x(v)}" y1="${t}" y2="${H - b}"/><text x="${x(v)}" y="${H - b + 16}" text-anchor="middle">${num(v, v < 100 ? 1 : 0)}</text>`).join("");
  const yt = [0, 0.25, 0.5, 0.75, 1].map((v) => `<text x="${l - 6}" y="${y(v) + 4}" text-anchor="end">${v * 100}%</text>`).join("");
  const ypt = niceTicks(pMin, pMax, 4).map((v) => `<text x="${W - rr + 6}" y="${yp(v) + 4}" style="fill:var(--s3)">${num(v, 0)}</text>`).join("");
  $("#adv-chart").innerHTML = `<svg viewBox="0 0 ${W} ${H}" id="adv-svg" style="cursor:crosshair" role="img" aria-label="Price trade-off">${shade}${band}${xt}${yt}${ypt}
    ${line((d) => y(d.win_probability), "var(--accent)")}${hasVol ? line((d) => y(Math.min(1, d.volume_share_worst_case ?? 1)), "var(--purple)", 1.8, "5 4") : ""}${line((d) => yp(d.expected_profit_per_unit), "var(--s3)")}
    ${today}${you}<text x="${(W + l - rr) / 2}" y="${H - 4}" text-anchor="middle">price per unit (${CUR})</text></svg>`;
  $("#adv-svg").addEventListener("click", (e) => {
    const svg = e.currentTarget, pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY;
    const loc = pt.matrixTransform(svg.getScreenCTM().inverse());
    ADV.tryPrice = Math.max(x0, Math.min(x1, x0 + ((loc.x - l) / (W - l - rr)) * (x1 - x0)));
    const tr = $("#try"); if (tr) tr.value = ADV.tryPrice;
    updateTry(); drawAdvisorChart();
  });
}

/* --------------------------------------------------------------- opportunity */
function renderOpportunity() {
  const o = OPP;
  if (!o.rows.length) { $("#page-opportunity").innerHTML = `<div class="card empty">No priced pairs in this run.</div>`; return; }
  const winAvg = o.rows.reduce((a, r) => a + (r.win_chance || 0), 0) / o.rows.length;
  const barList = (rows, colorFn) => {
    const max = Math.max(...rows.map((r) => Math.abs(r.extra_margin)), 1);
    return rows.map((r) => `<div style="display:grid;grid-template-columns:minmax(0,160px) 1fr 90px;gap:10px;align-items:center;padding:6px 0" data-tip="<b>${esc(r.key)}</b><br>${signed(r.extra_margin, (z) => big(z))} a year over ${r.pairs} customer-product pairs">
      <span class="small">${colorFn ? `<span class="dot" style="background:${colorFn(r.key)}"></span>` : ""}${esc(r.key)}</span>
      <div style="height:14px;background:var(--soft);border-radius:7px;position:relative"><div style="position:absolute;${r.extra_margin >= 0 ? "left:0" : "right:0"};height:100%;width:${100 * Math.abs(r.extra_margin) / max}%;background:${r.extra_margin >= 0 ? "var(--good)" : "var(--neg)"};border-radius:7px"></div></div>
      <b class="small" style="text-align:right">${signed(r.extra_margin, (z) => big(z))}</b></div>`).join("");
  };
  const rows = o.rows.filter((r) => (UI.oppMode === "gain" ? (r.change_pct || 0) > 0 : (r.change_pct || 0) < 0)).sort((a, b) => (UI.oppMode === "gain" ? b.extra_margin - a.extra_margin : a.change_pct - b.change_pct));
  const windowText = o.window.replace(/\d{4}-\d{2}/g, (m) => monthName(m));
  $("#page-opportunity").innerHTML = `
    <section class="hero"><h1>Better prices are worth <b>${signed(o.total_extra_margin, (z) => big(z))}</b> a year</h1>
      <p>If the recommended prices had been used on last year's orders (${esc(windowText)}), this is the extra margin, <b>after</b> the volume each price change is expected to cost or win back. It covers the ${num(o.pairs_considered)} most-traded customer-product pairs; ${num(o.pairs_priced)} of them had a recommendation and sales last year.</p></section>
    <div class="kpis">
      ${kpi("Extra margin a year", signed(o.total_extra_margin, (z) => big(z)), "after expected volume changes", "good")}
      ${kpi("Prices to raise", num(o.raise), "paying less than the market supports")}
      ${kpi("Prices to lower", num(o.lower), "a small cut wins back volume", "accent")}
      ${kpi("Average chance of winning", pct(100 * winAvg, 0), "at the recommended prices")}
    </div>
    <div class="row c2">
      <div class="card"><h2>By buying behaviour</h2><p class="small muted">Where the money is, by customer group.</p>${barList(o.by_segment, segColor)}</div>
      <div class="card"><h2>By sales channel</h2><p class="small muted">Where the money is, by channel.</p>${barList(o.by_channel)}</div>
    </div>
    <div class="card"><div class="card-head"><div><h2>Price by price</h2><p>Click a row to open it in the price advisor.</p></div>
      <div class="seg"><button class="${UI.oppMode === "gain" ? "on" : ""}" id="opp-gain">Prices to raise (${o.raise})</button><button class="${UI.oppMode === "cut" ? "on" : ""}" id="opp-cut">Prices to lower (${o.lower})</button></div></div>
      <div class="tablewrap tall"><table><thead><tr><th>Customer</th><th>Product</th><th>Buying behaviour</th><th class="num">Today</th><th class="num">Recommended</th><th class="num">Change</th><th class="num">Volume now → after</th><th class="num">Chance of winning</th><th class="num">Extra margin / year</th></tr></thead><tbody>
      ${rows.map((r) => `<tr class="click" data-advise="${esc(r.customer_id)}|${esc(r.product_id)}"><td><b>${esc(r.name || r.customer_id)}</b></td><td>${esc(r.product_id)}</td><td><span class="dot" style="background:${segColor(r.segment)}"></span>${esc(r.segment)}</td><td class="num">${money(r.today)}</td><td class="num"><b>${money(r.recommended)}</b></td><td class="num"><span class="badge ${r.change_pct >= 0 ? "good" : "info"}">${signed(r.change_pct, (z) => num(z, 1))}%</span></td><td class="num">${num(r.volume_now)} → ${num(r.volume_after)}</td><td class="num">${pct(100 * r.win_chance, 0)}</td><td class="num"><b style="color:${r.extra_margin >= 0 ? "var(--good)" : "var(--neg)"}">${signed(r.extra_margin, (z) => big(z))}</b></td></tr>`).join("")}</tbody></table></div>
      <p class="tiny muted">A price cut can show negative extra margin on last year's volume. It is recommended because it raises the chance of winning future quotes, which last year's volume does not capture.</p></div>`;
  $("#opp-gain").addEventListener("click", () => { UI.oppMode = "gain"; renderOpportunity(); });
  $("#opp-cut").addEventListener("click", () => { UI.oppMode = "cut"; renderOpportunity(); });
}

/* --------------------------------------------------------------- how it works */
let MLOPS = null;
async function renderHow() {
  const g = OV.graph, w = OV.win || {}, m = w.metrics || {}, e = OV.elasticity || {}, sgm = OV.segmentation || {}, c = OV.contract;
  const bad = (v) => /^not linked|not applied|failed/.test(v);
  $("#page-how").innerHTML = `
    <section class="hero"><h1>How AXIOM turns your sales data into prices</h1><p>No black box: five steps, every number traceable to the orders and quotes it came from, and an honest "not enough data" when that is the truth.</p></section>
    <div class="card"><div class="flow">
      <div class="fstep"><div class="ico">1</div><h2>Read your data</h2><p>Orders, invoices, discounts, rebates, quotes won and lost, straight from your systems. Here: ${num(g.documents)} orders and ${num(g.lines)} order lines.</p></div>
      <div class="fstep"><div class="ico">2</div><h2>Check it</h2><p>Every record is checked against the others before anything is calculated. Problems are named, not hidden.</p></div>
      <div class="fstep"><div class="ico">3</div><h2>Follow the money</h2><p>Every step from list price to margin, by customer, product, channel, region and month.</p></div>
      <div class="fstep"><div class="ico">4</div><h2>Learn your customers</h2><p>Groups customers by how they buy, measures how each group reacts to price, and learns from ${num((w.n_train || 0) + (w.n_test || 0))} quotes won and lost.</p></div>
      <div class="fstep"><div class="ico">5</div><h2>Recommend prices</h2><p>The price range with the best expected profit, inside guardrails you set: margin floor, list price, win rate, volume.</p></div>
    </div></div>
    <div class="trust mt">
      <div class="t"><div class="n">100%</div><b>traceable</b><p>Every figure carries the IDs of the orders and quotes behind it, so it can be checked and explained.</p></div>
      <div class="t"><div class="n">${pct(100 * (m.auc || 0), 0)}</div><b>of the time</b><p>it ranks a won quote above a lost one, tested on ${num(w.n_test)} later quotes it never saw while learning.</p></div>
      <div class="t"><div class="n">±${num(100 * (m.calibration_error || 0), 0)} pts</div><b>win-rate accuracy</b><p>Predicted and actual win rates on unseen quotes differ by this much on average: within what chance alone explains.</p></div>
      <div class="t"><div class="n">0</div><b>data sent outside</b><p>Runs on your own servers. No cloud service, no outside model, no data leaves the building.</p></div>
    </div>
    <div class="card mt"><h2>What makes it trustworthy</h2><ul class="checks mt">
      <li><span class="ok">✓</span><span><b>It says "I don't know".</b> When the data is too thin, it gives no price and explains why, instead of a confident guess.</span></li>
      <li><span class="ok">✓</span><span><b>Ranges, not false precision.</b> Prices come as a range, and every estimate carries its uncertainty.</span></li>
      <li><span class="ok">✓</span><span><b>Guardrails first.</b> Your rules are applied before the best price is picked, never patched on afterwards.</span></li>
      <li><span class="ok">✓</span><span><b>Reproducible.</b> Any result can be rebuilt from the same data with one command, and every run is recorded.</span></li>
      <li><span class="ok">✓</span><span><b>Watches itself.</b> Predictions are compared with what really happened every month, and drift raises a flag.</span></li>
    </ul></div>
    <div class="card mt" style="text-align:center"><h2>Now see it on your own numbers</h2><p class="muted">Every figure in the next pages comes from your ${num(g.documents)} orders and ${num(g.lines)} order lines.</p><button class="verdict good" style="border:0;cursor:pointer;font-size:.95rem;padding:8px 18px" data-go="overview">Start with the overview →</button></div>
    <details class="tech card"><summary>For your data team: under the hood</summary>
      <div class="row c2">
        <div><h3>The model chain</h3><ul class="checks">${Object.entries(OV.links || {}).map(([k, v]) => `<li><span class="${bad(v) ? "no" : "ok"}">${bad(v) ? "!" : "✓"}</span><span><b>${esc(k)}</b>: ${esc(v)}</span></li>`).join("")}</ul>
          <h3 class="mt">Customer groups</h3><p class="small">${esc(sgm.note || "")} Silhouette ${num(sgm.silhouette, 2)} (${esc(sgm.structure || "")}); k-means and hierarchical agree on ${pct(100 * (sgm.agreement || 0), 0)}.</p>
          <h3 class="mt">Price sensitivity per group</h3><table><thead><tr><th>Group</th><th class="num">Elasticity</th><th class="num">94% interval</th><th>Used</th></tr></thead><tbody>${(e.estimates || []).map((z) => `<tr><td>${esc(segName(z.scope.segment_label || Object.values(z.scope)[0]))}</td><td class="num">${num(z.beta, 2)}</td><td class="num">${num(z.low, 2)} to ${num(z.high, 2)}</td><td>${z.fit ? `<span class="badge good">yes</span>` : `<span class="badge warn">no</span>`}</td></tr>`).join("")}</tbody></table>
          ${e.circularity ? `<p class="small muted mt">${esc(e.circularity.note)}</p>` : ""}</div>
        <div><h3>Win model checks</h3><ul class="checks">
            <li><span class="${w.price_effect && w.price_effect.win_change_high < 0 ? "ok" : "no"}">✓</span><span>Higher prices lose more deals. ${esc(w.price_effect_sentence || "")}</span></li>
            <li><span class="${m.calibration_error <= m.calibration_error_by_chance ? "ok" : "no"}">✓</span><span>Probabilities right on later deals: off by ${pct(100 * m.calibration_error, 1)}, chance explains ${pct(100 * m.calibration_error_by_chance, 1)}.</span></li>
            <li><span class="${m.brier < m.brier_no_model ? "ok" : "no"}">✓</span><span>Better than guessing the average: Brier ${num(m.brier, 3)} vs ${num(m.brier_no_model, 3)}.</span></li>
            ${m.log_loss_without_segment != null ? `<li><span class="${m.log_loss < m.log_loss_without_segment ? "ok" : "no"}">✓</span><span>Knowing the customer group helps: log loss ${num(m.log_loss, 4)} vs ${num(m.log_loss_without_segment, 4)} without.</span></li>` : ""}</ul>
          <h3 class="mt">Result contract (what the narrative layer receives)</h3><p class="small">${num(c.waterfalls)} waterfall records, ${num(c.segments)} customer groupings, ${num(c.elasticities)} elasticities, ${num(c.recommendations)} price recommendations; ${c.violations.length ? `<b>${c.violations.length} violations</b>` : "no rule violations"}. Contract ${esc(c.version)}.</p>
          <p><a href="/api/contract">Download engine_result.json</a></p>
          <div id="mlops-box"><p class="small muted">Loading run history…</p></div></div>
      </div>
    </details>`;
  if (!MLOPS) { try { MLOPS = await api("/api/mlops"); } catch (_) { MLOPS = {}; } }
  const prov = MLOPS.last_contract_provenance || {};
  const reg = MLOPS.registry || [];
  const box = $("#mlops-box"); if (!box) return;
  box.innerHTML = `<h3 class="mt">Reproducibility</h3><p class="small">Last pipeline run: MLflow <code>${esc(prov.mlflow_run_id || "–")}</code>, data fingerprint <code>${esc((prov.snapshot_hash || "").slice(0, 12))}…</code>, ${num((MLOPS.dvc_stages || []).length)} DVC stages.</p>
    ${reg.length ? `<div class="tablewrap"><table><thead><tr>${Object.keys(reg[0]).slice(0, 5).map((k) => `<th>${esc(k.replace(/_/g, " "))}</th>`).join("")}</tr></thead><tbody>${reg.map((r) => `<tr>${Object.values(r).slice(0, 5).map((v) => `<td>${esc(typeof v === "number" ? num(v, 4) : v)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : ""}
    <h3 class="mt">Drift monitoring</h3><pre class="report">${esc(MLOPS["monitoring_report.txt"] || "not found")}</pre>`;
}

poll();
