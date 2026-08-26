/* ==========================================================================
   CloudFinOps Sentinel — Command Deck
   Dependency-free dashboard: polls /api/state and renders hand-built SVG.
   ========================================================================== */
(() => {
  "use strict";

  const POLL_MS = 5000;
  const NS = "http://www.w3.org/2000/svg";

  const COLORS = {
    cyan: "#3fe0ff", blue: "#4d7cfe", violet: "#9b6bff",
    green: "#2ffcaa", amber: "#ffc44d", red: "#ff5f79",
    muted: "#7189b8", dim: "#4a5f8f",
  };

  // Green means "nothing to do here" — including resources that are technically
  // idle but whose recoverable waste is under the action threshold.
  const STATUS_COLOR = {
    Healthy: COLORS.green, Tolerated: COLORS.green,
    Oversized: COLORS.amber, Idle: COLORS.red,
    Orphaned: COLORS.red, Unused: COLORS.amber, Untagged: COLORS.violet,
  };
  const SETTLED = new Set(["Healthy", "Tolerated"]);

  let lastState = null;
  let auditRunning = false;

  // ------------------------------------------------------------------ i18n
  const LANGS = ["en", "es"];

  function detectLang() {
    try {
      const saved = localStorage.getItem("sentinel.lang");
      if (LANGS.includes(saved)) return saved;
    } catch { /* private mode: fall through to the browser preference */ }
    const nav = (navigator.language || "en").slice(0, 2).toLowerCase();
    return LANGS.includes(nav) ? nav : "en";
  }

  let lang = detectLang();

  /** Translate `key`, interpolating {placeholders}. Falls back to English. */
  function t(key, params) {
    const dict = window.I18N[lang] || window.I18N.en;
    let out = dict[key] ?? window.I18N.en[key] ?? key;
    if (params) {
      for (const [k, v] of Object.entries(params)) out = out.replaceAll(`{${k}}`, v);
    }
    return out;
  }

  window.T = t; // used by chart renderers that receive catalogue keys

  function applyStaticTranslations() {
    document.documentElement.lang = lang;
    document.querySelectorAll("[data-i18n]").forEach((el) => {
      el.textContent = t(el.dataset.i18n);
    });
    document.querySelectorAll(".lang-btn").forEach((b) => {
      const on = b.dataset.lang === lang;
      b.classList.toggle("active", on);
      b.setAttribute("aria-pressed", String(on));
    });
  }

  function setLang(next) {
    if (!LANGS.includes(next) || next === lang) return;
    lang = next;
    try { localStorage.setItem("sentinel.lang", next); } catch { /* non-fatal */ }
    applyStaticTranslations();
    // Server-generated text (evidence, rules, events) is translated server
    // side, so a refetch is required — a client re-render is not enough.
    fetchState();
  }

  const $ = (id) => document.getElementById(id);

  // --------------------------------------------------------------- helpers
  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const money = (n) =>
    Number(n || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  const clockTime = (iso) => {
    if (!iso) return "--:--";
    const d = new Date(iso);
    return isNaN(d) ? "--:--" : d.toTimeString().slice(0, 5);
  };

  function svg(tag, attrs = {}) {
    const el = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    return el;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function emptyState(node, label) {
    node.innerHTML = `<div class="empty">${esc(label)}</div>`;
  }

  // ------------------------------------------------------------ animation
  const counters = new WeakMap();

  function animateNumber(el, target, format) {
    const from = counters.get(el) ?? 0;
    if (Math.abs(from - target) < 0.005) { el.firstChild.nodeValue = format(target); return; }
    counters.set(el, target);
    const start = performance.now();
    const DURATION = 750;

    (function step(now) {
      const p = Math.min(1, (now - start) / DURATION);
      const eased = 1 - Math.pow(1 - p, 3);
      el.firstChild.nodeValue = format(from + (target - from) * eased);
      if (p < 1) requestAnimationFrame(step);
    })(start);
  }

  // ---------------------------------------------------------------- toasts
  function toast(message, kind = "") {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = message;
    $("toasts").appendChild(el);
    setTimeout(() => {
      el.classList.add("out");
      setTimeout(() => el.remove(), 260);
    }, 3600);
  }

  // ==================================================================== KPIs
  function renderKPIs(k) {
    animateNumber($("kpi-spend"), k.monthly_spend, (v) => money(v));
    animateNumber($("kpi-savings"), k.realized_savings, (v) => money(v));
    $("kpi-waste").textContent = `$${money(k.wasted_spend)}`;
    $("kpi-actions").textContent = k.remediations_count ?? 0;
    $("m-resources").textContent = k.resources_monitored;
    $("m-anomalies").textContent = k.anomalies_open;
    $("m-pending").textContent = k.approvals_pending;
    $("m-audits").textContent = k.audits_completed;
    $("radar-score").textContent = t("score", { n: k.efficiency_score });
  }

  // ================================================================= ranking
  function renderRanking(rows) {
    const host = $("rank-list");
    if (!rows || !rows.length) return emptyState(host, "No resources");

    const max = Math.max(...rows.map((r) => r.allocated), 1);
    host.innerHTML = rows
      .map(
        (r) => `
      <div class="rank-row">
        <div class="rank-name" title="${esc(r.label)}">${esc(r.label)}</div>
        <div class="rank-bars">
          <div class="bar-track">
            <div class="bar-fill alloc" style="width:${(r.allocated / max) * 100}%">
              <span class="bar-val">$${money(r.allocated)}</span>
            </div>
          </div>
          <div class="bar-track">
            <div class="bar-fill waste" style="width:${(r.wasted / max) * 100}%">
              <span class="bar-val">$${money(r.wasted)}</span>
            </div>
          </div>
        </div>
      </div>`
      )
      .join("");
  }

  // =================================================================== donut
  function renderDonut(svgEl, legendEl, items, centerLabel, centerValue) {
    clear(svgEl);
    const total = items.reduce((s, i) => s + i.value, 0);
    if (!total) {
      legendEl.innerHTML = `<div class="empty">${t("nodata")}</div>`;
      return;
    }

    const cx = 60, cy = 60, r = 42, w = 13;
    const circumference = 2 * Math.PI * r;

    svgEl.appendChild(svg("circle", {
      cx, cy, r, fill: "none",
      stroke: "rgba(72,190,255,0.09)", "stroke-width": w,
    }));

    let offset = 0;
    items.forEach((item, i) => {
      const frac = item.value / total;
      const arc = svg("circle", {
        cx, cy, r, fill: "none",
        stroke: item.color, "stroke-width": w, "stroke-linecap": "butt",
        "stroke-dasharray": `${frac * circumference - 2} ${circumference}`,
        "stroke-dashoffset": -offset,
        transform: `rotate(-90 ${cx} ${cy})`,
        opacity: 0,
      });
      arc.style.filter = `drop-shadow(0 0 5px ${item.color}66)`;
      arc.style.transition = "opacity .5s ease";
      svgEl.appendChild(arc);
      requestAnimationFrame(() => { arc.setAttribute("opacity", "0.95"); });
      offset += frac * circumference;
    });

    const value = svg("text", {
      x: cx, y: cy + 1, "text-anchor": "middle", fill: COLORS.cyan,
      "font-size": "17", "font-weight": "700", class: "donut-center",
    });
    value.textContent = centerValue;
    svgEl.appendChild(value);

    const label = svg("text", {
      x: cx, y: cy + 15, "text-anchor": "middle", fill: COLORS.dim,
      "font-size": "8", "letter-spacing": "1.4",
    });
    label.textContent = centerLabel;
    svgEl.appendChild(label);

    legendEl.innerHTML = items
      .map(
        (i) => `
      <div class="item">
        <span class="swatch" style="background:${i.color};box-shadow:0 0 7px ${i.color}"></span>
        <span class="lbl">${esc(i.label)}</span>
        <span class="val">${esc(i.display)}</span>
      </div>`
      )
      .join("");
  }

  // =================================================================== trend
  function renderTrend(points, source) {
    const el = $("chart-trend");
    const box = el.parentElement.getBoundingClientRect();
    const W = Math.max(180, box.width), H = Math.max(120, box.height);
    el.setAttribute("viewBox", `0 0 ${W} ${H}`);
    clear(el);

    // Real metrics start sparse: a freshly deployed service has minutes of
    // history, not hours. Say so instead of drawing a degenerate line.
    if (!points || points.length < 3) {
      const n = points ? points.length : 0;
      const msg = svg("text", {
        x: W / 2, y: H / 2 - 4, "text-anchor": "middle",
        fill: COLORS.dim, "font-size": "11", "letter-spacing": "1",
      });
      msg.textContent = source === "monitoring" ? t("collecting") : t("notrend");
      el.appendChild(msg);

      const sub = svg("text", {
        x: W / 2, y: H / 2 + 14, "text-anchor": "middle",
        fill: COLORS.dim, "font-size": "9", opacity: "0.7",
      });
      sub.textContent =
        source === "monitoring" ? t("collecting.sub", { n }) : t("notrend.sub");
      el.appendChild(sub);
      return;
    }

    const pad = { t: 20, r: 24, b: 20, l: 30 };
    const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
    const x = (i) => pad.l + (i / (points.length - 1)) * iw;
    const y = (v) => pad.t + ih - (v / 100) * ih;

    // grid + y labels
    [0, 25, 50, 75, 100].forEach((v) => {
      el.appendChild(svg("line", {
        x1: pad.l, x2: W - pad.r, y1: y(v), y2: y(v), class: "grid-line",
      }));
      const t = svg("text", { x: pad.l - 6, y: y(v) + 3, "text-anchor": "end", class: "axis-lbl" });
      t.textContent = v;
      el.appendChild(t);
    });

    const series = [
      { key: "cpu", color: COLORS.cyan },
      { key: "memory", color: COLORS.violet },
    ];

    series.forEach((s, si) => {
      const line = points.map((p, i) => `${i ? "L" : "M"}${x(i)},${y(p[s.key])}`).join(" ");
      const area = `${line} L${x(points.length - 1)},${pad.t + ih} L${x(0)},${pad.t + ih} Z`;

      const gid = `grad-${s.key}`;
      const defs = svg("defs");
      const grad = svg("linearGradient", { id: gid, x1: "0", y1: "0", x2: "0", y2: "1" });
      grad.appendChild(svg("stop", { offset: "0%", "stop-color": s.color, "stop-opacity": si ? "0.28" : "0.4" }));
      grad.appendChild(svg("stop", { offset: "100%", "stop-color": s.color, "stop-opacity": "0" }));
      defs.appendChild(grad);
      el.appendChild(defs);

      el.appendChild(svg("path", { d: area, fill: `url(#${gid})` }));
      const stroke = svg("path", {
        d: line, fill: "none", stroke: s.color, "stroke-width": "1.8",
        "stroke-linejoin": "round", "stroke-linecap": "round",
      });
      stroke.style.filter = `drop-shadow(0 0 4px ${s.color}88)`;
      el.appendChild(stroke);

      // leading dot
      const last = points[points.length - 1];
      el.appendChild(svg("circle", {
        cx: x(points.length - 1), cy: y(last[s.key]), r: "2.8",
        fill: s.color, stroke: "#04081a", "stroke-width": "1.2",
      }));
    });

    // x labels: first, middle, last
    [0, Math.floor(points.length / 2), points.length - 1].forEach((i) => {
      const t = svg("text", { x: x(i), y: H - 6, "text-anchor": "middle", class: "axis-lbl" });
      t.textContent = points[i].t;
      el.appendChild(t);
    });

    // inline legend
    series.forEach((s, i) => {
      el.appendChild(svg("rect", { x: W - 92 + i * 46, y: 4, width: 7, height: 7, fill: s.color, rx: 1 }));
      const t = svg("text", { x: W - 82 + i * 46, y: 11, class: "axis-lbl", fill: COLORS.muted });
      t.textContent = s.key === "cpu" ? "CPU" : "MEM";
      el.appendChild(t);
    });
  }

  // =================================================================== radar
  function renderRadar(axes) {
    const el = $("chart-radar");
    const box = el.parentElement.getBoundingClientRect();
    const W = Math.max(180, box.width), H = Math.max(140, box.height);
    el.setAttribute("viewBox", `0 0 ${W} ${H}`);
    clear(el);
    if (!axes || !axes.length) return;

    const cx = W / 2, cy = H / 2 + 2;
    const R = Math.min(W, H) / 2 - 30;
    const n = axes.length;
    const pt = (i, frac) => {
      const a = (Math.PI * 2 * i) / n - Math.PI / 2;
      return [cx + Math.cos(a) * R * frac, cy + Math.sin(a) * R * frac];
    };

    // concentric rings
    [0.25, 0.5, 0.75, 1].forEach((f) => {
      const pts = Array.from({ length: n }, (_, i) => pt(i, f).join(",")).join(" ");
      el.appendChild(svg("polygon", {
        points: pts, fill: "none",
        stroke: "rgba(72,190,255,0.13)", "stroke-width": "1",
      }));
    });

    // spokes + labels
    axes.forEach((ax, i) => {
      const [px, py] = pt(i, 1);
      el.appendChild(svg("line", {
        x1: cx, y1: cy, x2: px, y2: py, stroke: "rgba(72,190,255,0.1)",
      }));
      const [lx, ly] = pt(i, 1.24);
      const t = svg("text", {
        x: lx, y: ly + 3, "text-anchor": "middle", class: "axis-lbl", fill: COLORS.muted,
      });
      t.textContent = ax.axis.startsWith("radar.") ? window.T(ax.axis) : ax.axis;
      el.appendChild(t);
    });

    // value polygon
    const pts = axes.map((ax, i) => pt(i, Math.max(0.04, ax.value / 100)).join(",")).join(" ");
    const poly = svg("polygon", {
      points: pts, fill: "rgba(255,196,77,0.18)",
      stroke: COLORS.amber, "stroke-width": "1.8", "stroke-linejoin": "round",
    });
    poly.style.filter = `drop-shadow(0 0 6px ${COLORS.amber}66)`;
    el.appendChild(poly);

    axes.forEach((ax, i) => {
      const [px, py] = pt(i, Math.max(0.04, ax.value / 100));
      el.appendChild(svg("circle", { cx: px, cy: py, r: "2.6", fill: COLORS.amber }));
    });
  }

  // ================================================================ topology
  function renderTopology(resources) {
    const host = $("topo");
    const box = host.getBoundingClientRect();
    const W = Math.max(300, box.width), H = Math.max(240, box.height);

    const el = svg("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "xMidYMid meet" });
    clear(host);
    host.appendChild(el);

    if (!resources || !resources.length) {
      const source = lastState?.data_source;
      return emptyState(
        host,
        source === "error"
          ? t("unreachable")
          : source === "idle"
          ? t("trace.hint")
          : t("nofound", { project: lastState?.project_id || "" })
      );
    }

    const cx = W / 2, cy = H / 2;
    const nodes = resources.slice(0, 9);
    // Elliptical orbit so a wide, short panel is used fully.
    const RX = Math.max(90, W / 2 - 90);
    const RY = Math.max(70, H / 2 - 48);
    const maxCost = Math.max(...nodes.map((n) => n.monthly_cost), 1);

    // orbit rings
    [1, 0.62].forEach((f, i) => {
      el.appendChild(svg("ellipse", {
        cx, cy, rx: RX * f, ry: RY * f, fill: "none",
        stroke: "rgba(72,190,255,0.11)", "stroke-width": "1",
        "stroke-dasharray": i ? "3 6" : "none",
      }));
    });

    // links + nodes
    nodes.forEach((n, i) => {
      const a = (Math.PI * 2 * i) / nodes.length - Math.PI / 2;
      const nx = cx + Math.cos(a) * RX;
      const ny = cy + Math.sin(a) * RY;
      const color = STATUS_COLOR[n.status] || COLORS.blue;
      const size = 7 + (n.monthly_cost / maxCost) * 9;

      const link = svg("line", {
        x1: cx, y1: cy, x2: nx, y2: ny,
        stroke: color, "stroke-width": "1.1", opacity: "0.45", class: "link",
      });
      link.style.animationDelay = `${i * 0.16}s`;
      el.appendChild(link);

      if (!SETTLED.has(n.status)) {
        const ring = svg("circle", {
          cx: nx, cy: ny, r: size, fill: "none",
          stroke: color, "stroke-width": "1.4", class: "node-ring",
        });
        ring.style.animationDelay = `${i * 0.3}s`;
        el.appendChild(ring);
      }

      const node = svg("circle", {
        cx: nx, cy: ny, r: size, fill: `${color}22`,
        stroke: color, "stroke-width": "1.8",
      });
      node.style.filter = `drop-shadow(0 0 7px ${color}99)`;
      el.appendChild(node);

      const spec = n.spec || `${n.cpu_limit} vCPU / ${n.memory_limit}`;
      const usage = n.utilization || `CPU ${n.cpu_utilization}% · MEM ${n.memory_utilization}%`;
      const title = svg("title");
      title.textContent =
        `${n.resource_id} — ${n.status}\n${n.type || "Cloud Run"} · ${spec}\n` +
        `$${money(n.monthly_cost)}/mo · ${usage}\nClick for the full analysis`;
      node.appendChild(title);
      node.style.cursor = "pointer";
      node.addEventListener("click", () => openDrawer(n));

      const below = Math.sin(a) > 0.25;
      const label = svg("text", {
        x: nx, y: ny + (below ? size + 15 : -size - 8),
        "text-anchor": "middle", fill: COLORS.muted, class: "node-label",
      });
      label.textContent = n.resource_id.length > 16 ? `${n.resource_id.slice(0, 15)}…` : n.resource_id;
      el.appendChild(label);

      const cost = svg("text", {
        x: nx, y: ny + (below ? size + 26 : -size - 19),
        "text-anchor": "middle", fill: color,
        "font-size": "9", "font-family": "JetBrains Mono, monospace",
      });
      cost.textContent = `$${Math.round(n.monthly_cost)}`;
      el.appendChild(cost);
    });

    // core
    const glow = svg("circle", { cx, cy, r: "34", fill: "rgba(63,224,255,0.07)" });
    el.appendChild(glow);
    const pulse = svg("circle", {
      cx, cy, r: "26", fill: "none", stroke: COLORS.cyan,
      "stroke-width": "1", opacity: "0.4",
    });
    pulse.appendChild(svg("animate", {
      attributeName: "r", values: "26;44;26", dur: "3.4s", repeatCount: "indefinite",
    }));
    pulse.appendChild(svg("animate", {
      attributeName: "opacity", values: "0.45;0;0.45", dur: "3.4s", repeatCount: "indefinite",
    }));
    el.appendChild(pulse);

    const hex = [];
    for (let i = 0; i < 6; i++) {
      const a = (Math.PI / 3) * i - Math.PI / 2;
      hex.push(`${cx + Math.cos(a) * 26},${cy + Math.sin(a) * 26}`);
    }
    const core = svg("polygon", {
      points: hex.join(" "), fill: "rgba(10,24,62,0.92)",
      stroke: COLORS.cyan, "stroke-width": "1.8",
    });
    core.style.filter = `drop-shadow(0 0 12px ${COLORS.cyan}aa)`;
    el.appendChild(core);

    const coreLabel = svg("text", {
      x: cx, y: cy + 4, "text-anchor": "middle", fill: COLORS.cyan, class: "core-label",
    });
    coreLabel.textContent = "SENTINEL";
    coreLabel.setAttribute("font-size", "9");
    el.appendChild(coreLabel);
  }

  // =================================================================== trace
  const trace = { steps: [], lastSeq: 0, source: null, connected: false, expanded: new Set() };

  /** Syntax-highlight a detail payload without pulling in a JSON viewer. */
  function highlight(value) {
    return esc(JSON.stringify(value, null, 2))
      .replace(/&quot;([^&]+?)&quot;:/g, '<span class="k">"$1"</span>:')
      .replace(/: &quot;([^&]*?)&quot;/g, ': <span class="s">"$1"</span>')
      .replace(/: (-?\d+\.?\d*)/g, ': <span class="n">$1</span>')
      .replace(/: (true|false|null)/g, ': <span class="n">$1</span>');
  }

  function renderTrace() {
    const host = $("trace");
    const head =
      `<div class="trace-head">
         <span class="live ${trace.connected ? "" : "off"}">
           <span class="dot ${trace.connected ? "live" : ""}"></span>
           ${trace.connected ? t("trace.live") : t("trace.off")}
         </span>
         <span class="count">${t("trace.steps", { n: trace.steps.length })}</span>
       </div>`;

    if (!trace.steps.length) {
      host.innerHTML = head + `<div class="empty">${t("trace.empty")}</div>`;
      return;
    }

    const rows = trace.steps
      .map((s) => {
        const open = trace.expanded.has(s.seq);
        const detail = s.detail
          ? `<div class="tdetail" ${open ? "" : "hidden"}>${highlight(s.detail)}</div>`
          : "";
        return `
        <div class="tstep ${esc(s.phase)} s-${esc(s.status)} ${s.detail ? "has-detail" : ""}"
             data-seq="${s.seq}">
          <span class="ts">${clockTime(s.timestamp)}</span>
          <span class="ph">${esc(s.phase)}</span>
          <span class="msg">${s.detail ? (open ? "▾ " : "▸ ") : ""}${esc(s.message)}</span>
          <span class="dur">${s.duration_ms != null ? `${s.duration_ms}ms` : ""}</span>
          ${detail}
        </div>`;
      })
      .join("");

    const atBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 60;
    host.innerHTML = head + rows;
    if (atBottom) host.scrollTop = host.scrollHeight;
  }

  function connectTrace() {
    if (trace.source) return;
    try {
      const source = new EventSource("/api/trace/stream");
      trace.source = source;

      source.onopen = () => { trace.connected = true; renderTrace(); };
      source.onerror = () => {
        // EventSource retries on its own; just reflect the state.
        trace.connected = false;
        renderTrace();
      };
      source.onmessage = (e) => {
        const message = JSON.parse(e.data);
        trace.connected = true;

        if (message.kind === "state") {
          // The server changed something we are displaying. Refresh now rather
          // than waiting up to POLL_MS for the next tick.
          fetchState();
          return;
        }

        if (message.seq <= trace.lastSeq) return; // replay of something we have
        trace.lastSeq = message.seq;
        trace.steps.push(message);
        if (trace.steps.length > 400) trace.steps.shift();
        renderTrace();
      };
    } catch (err) {
      console.error("trace stream unavailable", err);
    }
  }

  // =============================================================== inventory
  function renderInventory(resources) {
    const body = $("inventory").querySelector("tbody");
    if (!resources || !resources.length) {
      const idle = lastState?.data_source === "idle";
      body.innerHTML = `<tr><td colspan="9" style="padding:26px 8px">
        <div class="empty">${idle ? t("trace.hint") : t("noresources")}</div></td></tr>`;
      return;
    }

    // Resource types differ in shape, so the table describes each one in its
    // own terms rather than forcing Cloud Run's columns onto a disk.
    const spec = (r) =>
      r.spec || `${r.cpu_limit} vCPU · ${r.memory_limit} · min ${r.min_instances ?? 0}`;
    const usage = (r) =>
      r.utilization || `${r.cpu_utilization}% cpu · ${r.memory_utilization}% mem`;

    body.innerHTML = [...resources]
      .sort((a, b) => b.wasted_cost - a.wasted_cost)
      .map(
        (r) => `
      <tr data-id="${esc(r.resource_id)}">
        <td class="rid">${esc(r.resource_id)}</td>
        <td class="muted">${esc(r.type || "Cloud Run")}</td>
        <td class="muted">${esc(r.location || r.region || "—")}</td>
        <td class="muted">${esc(spec(r))}</td>
        <td class="muted">${esc(usage(r))}</td>
        <td class="num">$${money(r.monthly_cost)}</td>
        <td class="num ${r.wasted_cost > 0 ? "waste" : "zero"}">$${money(r.wasted_cost)}</td>
        <td><span class="state ${esc(r.status)}">${t("state." + r.status)}</span></td>
        <td><button class="why" data-id="${esc(r.resource_id)}">${t("col.why")}</button></td>
      </tr>`
      )
      .join("");
  }

  // =============================================================== rationale
  function evidenceTable(rows) {
    return `<table class="ev-table">${rows
      .map(
        (e) => `<tr>
          <td class="k">${esc(e.label)}</td>
          <td class="v">${esc(e.value)}</td>
          <td class="src">${esc(e.source)}</td>
        </tr>`
      )
      .join("")}</table>`;
  }

  function shapeBlock(sizing) {
    const row = (k, v) => `<div class="row"><span>${k}</span><span>${esc(v)}</span></div>`;
    return `<div class="shape">
      <div class="col">
        <h5>${t("drawer.current")}</h5>
        ${row(t("drawer.cpu"), sizing.current.cpu)}
        ${row(t("drawer.memory"), sizing.current.memory)}
        ${row(t("drawer.min"), sizing.current.min_instances)}
      </div>
      <div class="arrow">→</div>
      <div class="col to">
        <h5>${t("drawer.:proposed")}</h5>
        ${row(t("drawer.cpu"), sizing.target.cpu)}
        ${row(t("drawer.memory"), sizing.target.memory)}
        ${row(t("drawer.min"), sizing.target.min_instances)}
      </div>
    </div>`;
  }

  function openDrawer(resource) {
    const why = resource.rationale;
    $("drawer-title").textContent = resource.resource_id;
    $("drawer-sub").innerHTML =
      `<span>${esc(resource.type || "Cloud Run")}</span>` +
      `<span>${esc(resource.location || resource.region || "")}</span>` +
      `<span class="state ${esc(resource.status)}">${t("state." + resource.status)}</span>` +
      (resource.uri ? `<a class="link-out" href="${esc(resource.uri)}" target="_blank" rel="noopener">SERVICE URL ↗</a>` : "") +
      `<a class="link-out" href="${esc(resource.url)}" target="_blank" rel="noopener">GCP CONSOLE ↗</a>`;

    if (!why) {
      $("drawer-body").innerHTML = `<div class="empty">${t("drawer.noanalysis")}</div>`;
      $("drawer").hidden = false;
      return;
    }

    const parts = [];

    // The model's judgement comes first: it is what the operator is deciding on.
    const llm = resource.analysis;
    if (llm) {
      const conf = esc(llm.confidence || "low");
      parts.push(`<div class="block">
        <h4>${t("drawer.llm", { model: esc(lastState?.analysis?.model || "Gemini") })}
          <span class="badge ${conf}">${t("conf." + conf)}</span></h4>
        <div class="llm-block">
          <div class="llm-row"><span>${t("drawer.llm.diagnosis")}</span>${esc(llm.diagnosis)}</div>
          <div class="llm-row"><span>${t("drawer.llm.recommendation")}</span>${esc(llm.recommendation)}</div>
          <div class="llm-row risk"><span>${t("drawer.llm.risk")}</span>${esc(llm.risk)}</div>
        </div>
      </div>`);
    } else {
      parts.push(`<div class="block"><div class="note-box warn">${t("drawer.llm.none")}</div></div>`);
    }

    parts.push(`<div class="block"><h4>${t("drawer.measured")}</h4>${evidenceTable(why.evidence)}</div>`);

    if (why.rule) {
      parts.push(`<div class="block">
        <h4>${t("drawer.flagged")}</h4>
        <div class="rule-box">
          <strong>${esc(why.rule.id)}</strong>
          <code>IF ${esc(why.rule.condition)}</code>
          <code>OBSERVED ${esc(why.rule.observed)}</code>
          ${esc(why.rule.why_it_matters)}
        </div>
      </div>`);
    } else {
      parts.push(`<div class="block"><h4>${t("drawer.verdict")}</h4>
        <div class="note-box good">${esc(why.diagnosis)}</div></div>`);
    }

    if (why.sizing) {
      parts.push(`<div class="block"><h4>${t("drawer.change")}</h4>
        ${shapeBlock(why.sizing)}
        <div class="note-box" style="margin-top:12px">${esc(why.solution)}<br><br>
          ${esc(why.expected_result || "")}</div>
      </div>`);
    }

    if (why.capped) {
      parts.push(`<div class="block"><div class="note-box warn">${esc(why.capped)}</div></div>`);
    }

    if (why.confidence) {
      parts.push(`<div class="block">
        <h4>${t("drawer.confidence")} <span class="badge ${esc(why.confidence.level)}">${t("conf." + why.confidence.level)}</span></h4>
        <div class="note-box ${why.confidence.level === "high" ? "good" : "warn"}">
          ${esc(why.confidence.reason)}
        </div>
      </div>`);
    }

    if (why.autonomy) {
      parts.push(`<div class="block">
        <h4>${t("drawer.autonomy")}</h4>
        <div class="note-box">
          <strong>${esc(why.autonomy.level)} — ${esc(why.autonomy.decision)}</strong><br><br>
          ${esc(why.autonomy.reason)}
        </div>
      </div>`);
    }

    if (why.command) {
      parts.push(`<div class="block"><h4>${t("drawer.command")}</h4>
        <div class="cmd"><button class="copy" data-copy="${esc(why.command)}">${t("drawer.copy")}</button>${esc(why.command)}</div>
      </div>`);
    }

    $("drawer-body").innerHTML = parts.join("");
    $("drawer").hidden = false;
  }

  function openResourceById(id) {
    const pool = lastState?.inventory || lastState?.all_resources || [];
    const resource = pool.find((r) => r.resource_id === id);
    if (resource) openDrawer(resource);
  }

  // =============================================================== approvals
  function renderApprovals(approvals) {
    const host = $("approvals");
    const pending = (approvals || []).filter((a) => a.status === "PENDING");
    $("approval-note").textContent = pending.length
      ? t("awaiting", { n: pending.length })
      : t("panel.approvals.note");

    if (!pending.length) {
      host.innerHTML = `<div class="empty">
        <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="#2ffcaa" stroke-width="1.6">
          <path d="M9 12l2 2 4-4M21 12a9 9 0 11-18 0 9 9 0 0118 0z" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        ${t("noapprovals")}
      </div>`;
      return;
    }

    // Spell out the shape that approving this ticket applies. The headline is
  // prose; this is the contract, and it is what execution reads.
  const shapeChip = (shape) => {
    if (!shape || !shape.memory) return "";
    const bits = [];
    if (shape.cpu) bits.push(`${shape.cpu} vCPU`);
    bits.push(shape.memory);
    if (shape.min_instances !== null && shape.min_instances !== undefined) {
      bits.push(`min ${shape.min_instances}`);
    }
    return `<span class="shape">→ ${esc(bits.join(" / "))}</span>`;
  };

  host.innerHTML = pending
      .map(
        (a) => `
      <div class="approval ${a.severity === "HIGH" ? "high" : ""}">
        <div>
          <div class="act">${esc(a.proposed_action)}</div>
          <div class="meta">
            <span>${esc(a.resource_id)}</span>
            <span class="roi">+$${money(a.estimated_roi)}/mo</span>
            <span>${esc(a.severity || "HIGH")}</span>
            ${shapeChip(a.target_shape)}
            <a class="link-out" href="${esc(a.resource_url)}" target="_blank" rel="noopener">GCP CONSOLE ↗</a>
          </div>
          <div class="why">${esc(a.detailed_reason)}</div>
        </div>
        <div class="ctl">
          <button class="chip why-approval" data-id="${esc(a.resource_id)}">${t("why")}</button>
          <button class="chip deny" data-id="${esc(a.resource_id)}" data-status="REJECTED">${t("deny")}</button>
          <button class="chip approve" data-id="${esc(a.resource_id)}" data-status="APPROVED">${t("approve")}</button>
        </div>
      </div>`
      )
      .join("");
  }

  // ================================================================== stream
  function renderStream(events) {
    const host = $("stream");
    $("event-count").textContent = t("events", { n: (events || []).length });
    if (!events || !events.length) return emptyState(host, t("noactivity"));

    host.innerHTML = events
      .map(
        (e) => `
      <div class="ev ${esc(e.level)}">
        <span class="ts">${clockTime(e.timestamp)}</span>
        <span class="msg">${esc(e.message)}</span>
      </div>`
      )
      .join("");
  }

  // ================================================================ problems
  function renderProblems(problems) {
    const host = $("problems");
    if (!problems || !problems.length) {
      host.innerHTML = "";
      host.style.display = "none";
      return;
    }
    host.style.display = "";
    const label = (reason) => t(`pf.${reason}`);
    host.innerHTML = problems
      .map(
        (p) => `<span class="problem" title="${esc(p.detail)}">
          <b>${esc(p.source)}</b> ${esc(label(p.reason))}
        </span>`
      )
      .join("");
  }

  // ================================================================== header
  function renderHeader(s) {
    $("pill-project").innerHTML =
      `<span class="dot"></span> <b>${esc(s.project_id || "—")}</b> · ${esc(s.region || "")}`;

    const live = s.data_source === "gcp";
    const idle = s.data_source === "idle";
    const realMetrics = s.metrics_source === "monitoring";
    const src = $("pill-source");
    src.className = `pill ${idle ? "" : live ? (realMetrics ? "ok" : "warn") : "warn"}`;
    src.innerHTML = idle
      ? `${t("pill.source")} <b>${t("pill.idle")}</b>`
      : live
      ? `${t("pill.source")} <b>GCP</b> · ${realMetrics ? t("pill.monitoring") : t("pill.modelled")}`
      : `${t("pill.source")} <b>${t("pill.simulated")}</b>`;

    const eng = $("pill-model");
    const gemini = s.agent_mode === "gemini";
    eng.className = `pill ${gemini ? "info" : ""}`;
    eng.innerHTML = `${t("pill.engine")} <b>${gemini ? esc(s.model || "GEMINI") : t("pill.heuristic")}</b>`;

    // The single most important thing to see at a glance: is the agent
    // actually changing infrastructure, or only reporting what it would do?
    const mode = $("pill-mode");
    if (s.writes_enabled) {
      mode.className = "pill danger";
      mode.innerHTML = `<span class="dot live"></span> ${t("pill.livewrites")}`;
    } else {
      mode.className = "pill";
      mode.innerHTML = `${t("pill.mode")} <b>${t("pill.dryrun")}</b>`;
    }

    const agent = $("pill-agent");
    agent.className = `pill ${auditRunning ? "warn" : "ok"}`;
    agent.innerHTML = `<span class="dot live"></span> ${auditRunning ? t("pill.auditing") : t("pill.monitoring")}`;
  }

  // ============================================================ orchestration
  function render(state) {
    lastState = state;
    const c = state.charts || {};

    renderHeader(state);

    renderKPIs(state.kpis || {});
    renderRanking(c.ranking);

    renderDonut(
      $("donut-spend"), $("donut-spend-legend"),
      (c.distribution || []).map((d) => ({
        label: t(`state.${d.label}`), value: d.value,
        color: STATUS_COLOR[d.label] || COLORS.blue,
        display: `$${money(d.value)}`,
      })),
      t("donut.usd"),
      `$${Math.round(state.kpis?.monthly_spend || 0)}`
    );

    const byStatus = {};
    (state.inventory || state.all_resources || []).forEach((r) => {
      byStatus[r.status] = (byStatus[r.status] || 0) + 1;
    });
    renderDonut(
      $("donut-health"), $("donut-health-legend"),
      Object.entries(byStatus).map(([label, value]) => ({
        label: t(`state.${label}`), value,
        color: STATUS_COLOR[label] || COLORS.blue, display: String(value),
      })),
      t("donut.resources"),
      String(state.kpis?.resources_monitored || 0)
    );

    renderTrend(c.trend, c.trend_source);
    renderRadar(c.radar);
    renderTopology(state.inventory || state.all_resources);
    renderInventory(state.inventory || state.all_resources);
    renderApprovals(state.approvals);
    renderStream(state.events);

    const anomalies = state.kpis?.anomalies_open || 0;
    const regions = (state.regions_scanned || []).length;
    $("topo-note").textContent = anomalies
      ? t("anomalies", { n: anomalies, r: regions })
      : t("nominal", { r: regions });

    renderProblems(state.problems);

    const trendNote = document.querySelector("#chart-trend")
      .closest(".panel").querySelector(".panel-note");
    trendNote.textContent =
      c.trend_source === "monitoring" ? t("trend.real") : t("trend.modelled");
  }

  async function fetchState() {
    try {
      const res = await fetch(`/api/state?lang=${lang}`);
      if (res.status === 401) return handleUnauthorised();
      if (!res.ok) throw new Error(res.statusText);
      render(await res.json());
    } catch (err) {
      console.error("state fetch failed", err);
      const agent = $("pill-agent");
      agent.className = "pill warn";
      agent.innerHTML = `<span class="dot"></span> ${t("pill.linklost")}`;
    }
  }

  // ================================================================= actions
  async function decide(resourceId, status, button) {
    const card = button.closest(".approval");
    const buttons = card.querySelectorAll("button");
    buttons.forEach((b) => (b.disabled = true));
    card.style.opacity = "0.45";
    try {
      const res = await fetch("/api/approvals", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ resource_id: resourceId, status }),
      });
      if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
      toast(
        status === "APPROVED" ? t("toast.approved", { id: resourceId }) : t("toast.denied", { id: resourceId }),
        status === "APPROVED" ? "ok" : "err"
      );
      // The SSE push handles other clients; refresh ours too in case the
      // stream is unavailable.
      fetchState();
    } catch (err) {
      toast(t("toast.failed", { e: err.message }), "err");
      card.style.opacity = "";
      buttons.forEach((b) => (b.disabled = false));
    }
  }

  async function runAudit() {
    const btn = $("btn-audit");
    btn.disabled = true;
    auditRunning = true;
    if (lastState) renderHeader(lastState);
    try {
      const res = await fetch("/api/trigger", { method: "POST" });
      const body = await res.json();
      toast(body.status === "busy" ? t("toast.busy") : t("toast.dispatched"), "ok");
      if (body.status !== "busy") {
        trace.steps = [];
        trace.lastSeq = 0;
        trace.expanded.clear();
        showTraceTab();  // the whole point of pressing the button is to watch
      }
    } catch (err) {
      toast(t("toast.trigger", { e: err.message }), "err");
    }
    setTimeout(() => {
      auditRunning = false;
      btn.disabled = false;
      fetchState();
    }, 3500);
  }

  const PF_GLYPH = { ok: "✓", fail: "✗", warn: "!", skip: "–" };
  const PF_COLOR = { ok: COLORS.green, fail: COLORS.red, warn: COLORS.amber, skip: COLORS.dim };

  async function showPreflight() {
    $("modal-title").textContent = t("modal.preflight");
    $("modal-body").innerHTML = `<span style="color:#7189b8">${t("modal.running")}</span>`;
    $("modal").hidden = false;

    try {
      const r = await fetch(`/api/preflight?lang=${lang}`);
      const pf = await r.json();

      const head =
        `project ${esc(pf.project_id)} · region ${esc(pf.region)}\n` +
        (pf.service_account ? `identity ${esc(pf.service_account)}\n` : "") +
        `dry_run ${pf.dry_run} · mock_mode ${pf.mock_mode}\n\n`;

      const body = pf.checks
        .map((c) => {
          const color = PF_COLOR[c.status];
          const fix = c.fix
            ? c.fix.split("\n").map((l) => `      <span style="color:${COLORS.cyan}">→ ${esc(l)}</span>`).join("\n")
            : "";
          return (
            `<span style="color:${color}">${PF_GLYPH[c.status]}</span> <strong>${esc(c.name)}</strong>\n` +
            `      ${esc(c.detail)}` + (fix ? `\n${fix}` : "")
          );
        })
        .join("\n\n");

      const verdict = pf.ready
        ? `\n\n<span style="color:${COLORS.green}">${t("modal.ready")}</span>`
        : `\n\n<span style="color:${COLORS.red}">${t("modal.blocking", { n: pf.failures })}</span>`;

      $("modal-body").innerHTML = esc(head).replace(/\n/g, "<br>") +
        body.replace(/\n/g, "<br>") + verdict;
    } catch (err) {
      $("modal-body").textContent = t("modal.failed", { e: err.message });
    }
  }

  let showTraceTab = () => {};

  const DECISION_COLOR = {
    APPROVED: COLORS.green, REJECTED: COLORS.red, PENDING: COLORS.amber,
  };

  function historyEntry(h) {
    const c = h.counts;
    const when = `${h.started_at.slice(0, 10)} ${clockTime(h.started_at)}`;

    const chips = [
      ["hist.proposed", c.proposed, COLORS.cyan],
      ["hist.approved", c.approved, COLORS.green],
      ["hist.rejected", c.rejected, COLORS.red],
      ["hist.pending", c.pending, COLORS.amber],
      ["hist.executed", c.executed, COLORS.violet],
    ]
      .filter(([, n]) => n > 0)
      .map(([k, n, color]) =>
        `<span class="hchip" style="color:${color};border-color:${color}44">${n} ${t(k)}</span>`)
      .join("");

    // Pair each recommendation with what became of it.
    const decisions = h.approvals.length
      ? h.approvals
          .map((a) => {
            const rem = h.remediations.find((r) => r.resource_id === a.resource_id);
            const outcome = rem
              ? `<span style="color:${rem.applied ? COLORS.green : COLORS.amber}"> · ${
                  rem.applied ? t("hist.applied") : t("hist.simulated")}</span>`
              : "";
            return `<div class="hrow">
              <span class="hstatus" style="color:${DECISION_COLOR[a.status]}">${esc(a.status)}</span>
              <span class="hact">${esc(a.proposed_action)}</span>
              <span class="hroi">+$${money(a.estimated_roi)}${outcome}</span>
            </div>`;
          })
          .join("")
      : `<div class="hrow muted">${t("hist.nothing")}</div>`;

    const ch = h.changes || {};
    let changeLine;
    if (ch.first_scan) {
      changeLine = `<div class="hchange">${t("chg.first")}</div>`;
    } else if (!ch.added?.length && !ch.removed?.length && !ch.changed?.length) {
      changeLine = `<div class="hchange">${t("chg.none", { n: ch.unchanged || 0 })}</div>`;
    } else {
      const parts = [];
      if (ch.added?.length) parts.push(`<b style="color:${COLORS.cyan}">${t("chg.added", { n: ch.added.length })}</b>`);
      if (ch.removed?.length) parts.push(`<b style="color:${COLORS.dim}">${t("chg.removed", { n: ch.removed.length })}</b>`);
      if (ch.changed?.length) parts.push(`<b style="color:${COLORS.amber}">${t("chg.changed", { n: ch.changed.length })}</b>`);
      if (ch.unchanged) parts.push(`${t("chg.same", { n: ch.unchanged })}`);

      const details = (ch.changed || [])
        .map((c) => {
          const d = Object.entries(c.deltas)
            .map(([k, [a, b]]) => `${esc(k)} ${esc(String(a))} → ${esc(String(b))}`)
            .join(", ");
          return `<div class="hdelta">${esc(c.resource_id)}: ${d}</div>`;
        })
        .join("");

      changeLine = `<div class="hchange">
        <span class="hchange-title">${t("chg.title")}</span> ${parts.join(" · ")}
        ${details}
      </div>`;
    }

    const banner = h.degraded
      ? `<div class="hbanner">${esc(h.degraded)}</div>`
      : h.error
      ? `<div class="hbanner err">${esc(h.error)}</div>`
      : "";

    return `<div class="hentry">
      <div class="hhead">
        <strong>${t("hist.scan", { n: h.index })}</strong>
        <span class="hmeta">${esc(when)} · ${t("hist.anomalies", { n: h.anomalies_found })}${
          h.mode ? ` · ${esc(h.mode)}` : ""}</span>
      </div>
      ${banner}
      ${changeLine}
      <div class="hchips">${chips}</div>
      <div class="hsavings">${t("hist.savings", {
        realized: money(h.savings.realized), proposed: money(h.savings.proposed),
      })}</div>
      ${decisions}
    </div>`;
  }

  async function showReport() {
    $("modal-title").textContent = t("modal.report");
    $("modal-body").innerHTML = `<span style="color:#7189b8">${t("modal.running")}</span>`;
    $("modal").hidden = false;

    try {
      const res = await fetch(`/api/history?lang=${lang}`);
      const { history } = await res.json();
      $("modal-body").innerHTML = history.length
        ? history.map(historyEntry).join("")
        : `<div class="empty">${t("hist.empty")}</div>`;
    } catch (err) {
      $("modal-body").textContent = t("modal.failed", { e: err.message });
    }
  }

  // =================================================================== wiring
  // ==================================================================== auth
  function showGate(messageKey) {
    $("deck").hidden = true;
    $("gate").hidden = false;
    const err = $("gate-error");
    if (messageKey) {
      err.textContent = t(messageKey);
      err.hidden = false;
    } else {
      err.hidden = true;
    }
    $("gate-token").focus();
  }

  function showDeck() {
    $("gate").hidden = true;
    $("deck").hidden = false;
  }

  /** A 401 anywhere means the session went away; return to the gate. */
  function handleUnauthorised() {
    if (trace.source) {
      trace.source.close();
      trace.source = null;
      trace.connected = false;
    }
    showGate("auth.locked");
  }

  async function bootstrap() {
    applyStaticTranslations();

    let posture;
    try {
      posture = await (await fetch("/api/auth")).json();
    } catch {
      return showGate("auth.unreachable");
    }

    if (!posture.configured && !posture.managed_runtime) {
      return start();  // local development: no token configured
    }

    // Probe with a cheap protected call: a live cookie skips the gate.
    try {
      const probe = await fetch("/api/auth");
      const state = await fetch(`/api/state?lang=${lang}`);
      if (state.status === 401) return showGate();
      return start();
    } catch {
      return showGate("auth.unreachable");
    }
  }

  function start() {
    showDeck();
    connectTrace();
    fetchState();
    setInterval(fetchState, POLL_MS);
  }

  async function submitLogin(event) {
    event.preventDefault();
    const token = $("gate-token").value.trim();
    if (!token) return;

    try {
      const res = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      if (!res.ok) return showGate("auth.invalid");
      $("gate-token").value = "";
      start();
    } catch {
      showGate("auth.unreachable");
    }
  }

  async function logout() {
    try {
      await fetch("/api/logout", { method: "POST" });
    } catch { /* locking locally is what matters */ }
    handleUnauthorised();
  }

  function init() {
    $("approvals").addEventListener("click", (e) => {
      const why = e.target.closest("button.why-approval");
      if (why) return openResourceById(why.dataset.id);
      const btn = e.target.closest("button[data-status]");
      if (btn) decide(btn.dataset.id, btn.dataset.status, btn);
    });

    // Inventory: whole row opens the analysis.
    $("inventory").addEventListener("click", (e) => {
      const row = e.target.closest("tr[data-id]");
      if (row) openResourceById(row.dataset.id);
    });

    // Tabs
    const TABS = ["topology", "inventory", "trace"];
    const showTab = (name) => {
      TABS.forEach((tab) => {
        const on = tab === name;
        $(`tab-${tab}`).classList.toggle("active", on);
        $(`tab-${tab}`).setAttribute("aria-selected", String(on));
        $(`view-${tab}`).hidden = !on;
      });
      if (name === "topology" && lastState) {
        renderTopology(lastState.inventory || lastState.all_resources);
      }
      if (name === "trace") renderTrace();
    };
    TABS.forEach((tab) => $(`tab-${tab}`).addEventListener("click", () => showTab(tab)));
    showTraceTab = () => showTab("trace");

    // Clicking a step toggles its request/response payload.
    $("trace").addEventListener("click", (e) => {
      const row = e.target.closest(".tstep.has-detail");
      if (!row) return;
      const seq = Number(row.dataset.seq);
      trace.expanded.has(seq) ? trace.expanded.delete(seq) : trace.expanded.add(seq);
      renderTrace();
    });

    // Drawer
    $("drawer-close").addEventListener("click", () => ($("drawer").hidden = true));
    $("drawer").addEventListener("click", (e) => {
      if (e.target === $("drawer")) $("drawer").hidden = true;
    });
    $("drawer-body").addEventListener("click", async (e) => {
      const btn = e.target.closest("button[data-copy]");
      if (!btn) return;
      try {
        await navigator.clipboard.writeText(btn.dataset.copy);
        toast(t("toast.copied"), "ok");
      } catch {
        toast(t("toast.noclipboard"), "err");
      }
    });

    $("btn-audit").addEventListener("click", runAudit);
    $("btn-report").addEventListener("click", showReport);
    $("btn-preflight").addEventListener("click", showPreflight);
    $("modal-close").addEventListener("click", () => ($("modal").hidden = true));
    $("modal").addEventListener("click", (e) => {
      if (e.target === $("modal")) $("modal").hidden = true;
    });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      $("modal").hidden = true;
      $("drawer").hidden = true;
    });

    // Clock
    const tick = () => { $("pill-clock").textContent = new Date().toTimeString().slice(0, 8); };
    tick();
    setInterval(tick, 1000);

    // Redraw size-dependent SVGs when the layout changes.
    let resizeTimer;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => { if (lastState) render(lastState); }, 160);
    });

    document.querySelectorAll(".lang-btn").forEach((btn) => {
      btn.addEventListener("click", () => setLang(btn.dataset.lang));
    });

    $("gate-form").addEventListener("submit", submitLogin);
    $("btn-logout").addEventListener("click", logout);
    bootstrap();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
