(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var MAX_LINES = 1000;
  var STATUS_ORDER = ["synced", "plain", "has_lrc", "not_found", "rejected", "instrumental",
    "untagged", "error", "pending", "missing"];
  var LABELS = { has_lrc: "has .lrc", not_found: "not found" };
  var state = { paused: false, stick: true, lastLogId: 0, cooldownUntil: 0, job: null, busy: false };
  var toastTimer = 0;

  // ---- error reporting (POST /api/client-log -> "ui.error" in the server's JSONL log) ----------------------
  // Must never throw or loop: it uses the original fetch, swallows every failure and is rate limited.
  var rawFetch = window.fetch ? window.fetch.bind(window) : null;
  var MAX_REPORTS = 20, SAME_KEY_MS = 30000;
  var reportCount = 0, reportLast = {}, appVersion = "", sseState = "none";

  function snapshot() {
    var j = state.job;
    return {
      job: j ? { id: j.id, type: j.type, state: j.state, phase: j.phase, done: j.done, total: j.total } : null,
      busy: !!state.busy,
      lastLogId: state.lastLogId,
      viewport: { w: window.innerWidth, h: window.innerHeight },
      userAgent: navigator.userAgent,
      version: appVersion,
      sse: sseState,
      online: navigator.onLine
    };
  }

  function report(kind, message, stack, key) {
    try {
      if (!rawFetch || reportCount >= MAX_REPORTS) return;
      var k = key || kind + ":" + String(message).slice(0, 80);
      var now = Date.now();
      if (reportLast[k] && now - reportLast[k] < SAME_KEY_MS) return;
      reportLast[k] = now;
      reportCount++;
      var body = JSON.stringify({
        kind: kind,
        message: String(message).slice(0, 1000),
        stack: stack ? String(stack).slice(0, 4000) : "",
        url: location.href.slice(0, 300),
        state: snapshot()
      });
      if (body.length > 15000) body = JSON.stringify({ kind: kind, message: String(message).slice(0, 1000) });
      rawFetch("/api/client-log", {
        method: "POST",
        credentials: "same-origin",
        keepalive: true,
        headers: { "Content-Type": "application/json", "X-Requested-With": "lrclib-web" },
        body: body
      }).catch(function () { /* the report itself failed: give up silently */ });
    } catch (x) { /* never throw from the reporter */ }
  }

  window.addEventListener("error", function (e) {
    report("window.onerror", e.message || "error",
      (e.error && e.error.stack) || (e.filename ? e.filename + ":" + e.lineno + ":" + e.colno : ""));
  });
  window.addEventListener("unhandledrejection", function (e) {
    var r = e.reason;
    report("unhandledrejection", (r && r.message) || String(r), r && r.stack);
  });
  if (rawFetch) {
    window.fetch = function (input, init) {
      var url = typeof input === "string" ? input : (input && input.url) || "";
      var p = rawFetch(input, init);
      if (url.indexOf("/api/client-log") < 0) {
        p.then(function (r) {
          if (!r.ok && r.status !== 409) report("fetch", "HTTP " + r.status + " " + url, "", "fetch:" + url + ":" + r.status);
        }, function (err) {
          report("fetch", "network error " + url + ": " + (err && err.message), err && err.stack, "fetch:" + url + ":net");
        });
      }
      return p;
    };
  }

  function fmtNum(n) {
    return String(Math.round(Number(n) || 0)).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  }
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function fmtTime(sec) {
    var d = new Date(sec * 1000);
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }
  function fmtDateTime(sec) {
    var d = new Date(sec * 1000);
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " +
      pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  function fmtEta(s) {
    s = Math.max(0, Math.round(s));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    if (h) return h + "h " + m + "m " + r + "s";
    if (m) return m + "m " + r + "s";
    return r + "s";
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function chip(key, label, value) {
    var c = el("span", "chip " + key, label);
    c.appendChild(el("b", "", fmtNum(value)));
    return c;
  }

  function toast(msg) {
    var t = $("toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.hidden = true; }, 4000);
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Requested-With": "lrclib-web" },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      if (r.status === 409) { toast("A job is already running"); return; }
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          toast(j.error || ("Request failed (" + r.status + ")"));
        });
      }
    }).catch(function (e) { toast("Network error: " + e.message); });
  }

  // ---- rendering -------------------------------------------------------
  function renderHeader(s) {
    $("version").textContent = s.version ? "v" + s.version : "";
    var p = s.plex || {};
    $("plex-wrap").hidden = !p.enabled;
    var d = $("dot-plex");
    d.className = "dot " + (p.ok ? "ok" : "bad");
    d.title = p.ok ? "Plex OK" : (p.error || "Plex unreachable");
  }

  function renderLibrary(s) {
    var lib = s.library || {}, counts = lib.counts || {};
    $("total").textContent = fmtNum(lib.total) + " tracks";
    var box = $("lib-chips");
    box.replaceChildren();
    STATUS_ORDER.forEach(function (k) {
      if (counts[k]) box.appendChild(chip(k, LABELS[k] || k, counts[k]));
    });
    Object.keys(counts).forEach(function (k) {
      if (STATUS_ORDER.indexOf(k) < 0 && counts[k]) box.appendChild(chip(k, k, counts[k]));
    });
    var ls = lib.last_scan;
    $("last-scan").textContent = ls ? "Last scan " + fmtDateTime(ls.at) + " (" + ls.mode + ")" : "No scan yet";
  }

  function renderJob(s) {
    var j = s.job, busy = !!s.busy;
    var line = "No job yet", pct = 0, eta = "";
    if (j) {
      line = [j.state, j.phase, fmtNum(j.done) + " / " + fmtNum(j.total)].filter(Boolean).join(" · ");
      pct = j.total ? Math.min(100, (j.done / j.total) * 100) : 0;
      if (busy && j.eta_s != null) eta = "ETA " + fmtEta(j.eta_s);
    }
    $("job-line").textContent = line;
    $("bar").value = pct;
    $("eta").textContent = eta;
    state.cooldownUntil = j && j.cooldown_until ? j.cooldown_until : 0;
    tickCooldown();
    var box = $("job-chips");
    box.replaceChildren();
    var c = (j && j.counters) || {};
    Object.keys(c).forEach(function (k) {
      if (c[k]) box.appendChild(chip(k, LABELS[k] || k, c[k]));
    });
    ["btn-scan", "btn-full", "btn-download", "btn-run"].forEach(function (id) { $(id).disabled = busy; });
    $("btn-stop").disabled = !busy;
  }

  function renderSchedule(s) {
    var box = $("schedule");
    box.replaceChildren();
    (s.schedule || []).forEach(function (it) {
      var what = it.type + (it.mode ? " (" + it.mode + ")" : "");
      box.appendChild(el("div", "", "Next " + what + ": " + (it.next_at ? String(it.next_at).replace("T", " ") : "-") +
        (s.tz ? " " + s.tz : "")));
    });
    var ul = $("runs");
    ul.replaceChildren();
    (s.last_runs || []).forEach(function (r) {
      var txt = "#" + r.id + " " + r.type + " · " + r.state;
      if (r.summary) txt += " · " + (typeof r.summary === "string" ? r.summary : JSON.stringify(r.summary));
      if (r.finished_at) txt += " · " + fmtDateTime(r.finished_at);
      ul.appendChild(el("li", "", txt));
    });
    if (!ul.children.length) ul.appendChild(el("li", "muted", "No runs yet"));
  }

  function tickCooldown() {
    var left = Math.ceil(state.cooldownUntil - Date.now() / 1000);
    var b = $("cooldown");
    b.hidden = left <= 0;
    if (left > 0) b.textContent = "cooldown " + left + "s";
  }

  function applyStatus(s) {
    state.job = s.job || null;
    state.busy = !!s.busy;
    if (s.version) appVersion = s.version;
    renderHeader(s); renderLibrary(s); renderJob(s); renderSchedule(s);
  }

  // ---- log -------------------------------------------------------------
  function addLog(l) {
    if (l.id && l.id <= state.lastLogId) return;
    if (l.id) state.lastLogId = l.id;
    var box = $("log");
    var row = el("div", l.level || "");
    row.appendChild(el("span", "t", fmtTime(l.ts)));
    row.appendChild(document.createTextNode(l.msg));
    box.appendChild(row);
    while (box.childElementCount > MAX_LINES) box.firstElementChild.remove();
    if (!state.paused && state.stick) box.scrollTop = box.scrollHeight;
  }

  function saveLog() {
    var rows = $("log").children;
    if (!rows.length) { toast("Log is empty"); return; }
    var lines = [];
    for (var i = 0; i < rows.length; i++) {
      var t = rows[i].querySelector(".t");
      var time = t ? t.textContent : "";
      lines.push(time + " " + (rows[i].className || "info") + " " + rows[i].textContent.slice(time.length));
    }
    var d = new Date();
    var stamp = d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) + "-" + pad(d.getHours()) + pad(d.getMinutes());
    saveBlob(new Blob([lines.join("\n") + "\n"], { type: "text/plain" }), "lrclib-web-log-" + stamp + ".txt");
  }

  function saveBlob(blob, name) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  function saveDiag() {
    var btn = $("btn-diag");
    btn.disabled = true;
    fetch("/api/diag", { credentials: "same-origin" }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      var cd = r.headers.get("Content-Disposition") || "";
      var m = /filename="?([^";]+)"?/.exec(cd);
      return r.blob().then(function (b) { return { blob: b, name: m ? m[1] : "lrclib-web-diag.json" }; });
    }).then(function (res) {
      saveBlob(res.blob, res.name);
    }).catch(function (e) {
      toast("Diagnostics failed: " + e.message);
    }).then(function () { btn.disabled = false; });
  }

  // ---- live data -------------------------------------------------------
  function setConn(ok) {
    var d = $("dot-lrclib");
    d.className = "dot " + (ok ? "ok" : "bad");
    d.title = ok ? "Connected" : "Disconnected";
  }

  function jget(url) {
    return fetch(url, { credentials: "same-origin" }).then(function (r) { return r.json(); });
  }

  function startPolling() {
    function tick() {
      jget("/api/status").then(function (s) { setConn(true); applyStatus(s); })
        .catch(function () { setConn(false); });
      jget("/api/log?since=" + state.lastLogId).then(function (r) { (r.lines || []).forEach(addLog); })
        .catch(function () {});
    }
    tick();
    setInterval(tick, 2000);
  }

  function startSSE() {
    var es = new EventSource("/api/events");
    es.onopen = function () { sseState = "open"; setConn(true); };
    es.onerror = function () {
      sseState = es.readyState === 2 ? "closed" : "reconnecting";
      setConn(false);
      report("sse", "EventSource error (readyState " + es.readyState + ")", "", "sse");
    };
    es.addEventListener("status", function (e) {
      setConn(true);
      try { applyStatus(JSON.parse(e.data)); } catch (x) { /* ignore malformed */ }
    });
    es.addEventListener("log", function (e) {
      try { addLog(JSON.parse(e.data)); } catch (x) { /* ignore malformed */ }
    });
  }

  // ---- init ------------------------------------------------------------
  function init() {
    var log = $("log");
    log.addEventListener("scroll", function () {
      state.stick = log.scrollHeight - log.scrollTop - log.clientHeight < 24;
    });
    $("btn-save-log").addEventListener("click", saveLog);
    $("btn-diag").addEventListener("click", saveDiag);
    $("btn-pause").addEventListener("click", function () {
      state.paused = !state.paused;
      this.textContent = state.paused ? "Resume" : "Pause";
      if (!state.paused) log.scrollTop = log.scrollHeight;
    });
    $("btn-scan").addEventListener("click", function () { post("/api/jobs", { type: "scan", mode: "quick" }); });
    $("btn-full").addEventListener("click", function () { post("/api/jobs", { type: "scan", mode: "full" }); });
    $("btn-download").addEventListener("click", function () { post("/api/jobs", { type: "download" }); });
    $("btn-run").addEventListener("click", function () { post("/api/jobs", { type: "run", mode: "quick" }); });
    $("btn-stop").addEventListener("click", function () { post("/api/jobs/current/stop"); });
    $("btn-stop").disabled = true;
    setInterval(tickCooldown, 1000);
    if (window.EventSource) startSSE(); else startPolling();
  }
  init();
})();
