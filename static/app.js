/* netwatch dashboard.
   Polls the JSON API and renders target tiles plus a latency history chart.
   All values are inserted as text nodes, never as HTML, so nothing a monitored
   host reports back can become markup in this page. */
(function () {
  "use strict";

  var REFRESH_MS = 5000;
  var selected = null;

  var tilesEl = document.getElementById("tiles");
  var summaryEl = document.getElementById("summary");
  var updatedEl = document.getElementById("updated");
  var emptyEl = document.getElementById("empty");
  var detailEl = document.getElementById("detail");
  var detailTitle = document.getElementById("detail-title");
  var detailHint = document.getElementById("detail-hint");
  var chartEl = document.getElementById("chart");

  document.getElementById("close-detail").addEventListener("click", function () {
    selected = null;
    detailEl.hidden = true;
  });

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function fmtLatency(ms) {
    if (ms === null || ms === undefined) return "—";
    return ms < 10 ? ms.toFixed(1) + " ms" : Math.round(ms) + " ms";
  }

  function fmtUptime(pct) {
    if (pct === null || pct === undefined) return "—";
    return pct.toFixed(pct >= 99.95 ? 0 : 2) + "%";
  }

  function buildTile(target) {
    var up = target.up === 1;
    var tile = el("button", "tile " + (up ? "up" : "down"));
    tile.type = "button";

    var head = el("div", "tile-head");
    head.appendChild(el("span", "tile-name", target.target));
    head.appendChild(el("span", "badge " + (up ? "up" : "down"), up ? "up" : "down"));
    tile.appendChild(head);

    tile.appendChild(el("div", "tile-addr", target.host + ":" + target.port));

    var stats = el("div", "stats");
    [["Latency", fmtLatency(target.latency_ms)],
     ["Uptime 24h", fmtUptime(target.uptime_24h)],
     ["Checks", target.checks_24h]].forEach(function (pair) {
      var box = el("div");
      box.appendChild(el("div", "stat-label", pair[0]));
      box.appendChild(el("div", "stat-value", pair[1]));
      stats.appendChild(box);
    });
    tile.appendChild(stats);

    if (!up && target.error) {
      tile.appendChild(el("div", "error", target.error));
    }

    tile.addEventListener("click", function () {
      selected = target.target;
      loadHistory(selected);
    });
    return tile;
  }

  function render(data) {
    var targets = data.targets || [];
    tilesEl.replaceChildren();

    if (targets.length === 0) {
      emptyEl.hidden = false;
      summaryEl.textContent = data.configured + " target(s) configured";
      return;
    }
    emptyEl.hidden = true;

    targets.forEach(function (target) {
      tilesEl.appendChild(buildTile(target));
    });

    var up = targets.filter(function (t) { return t.up === 1; }).length;
    summaryEl.textContent = up + " of " + targets.length + " up";
    updatedEl.textContent = "updated " +
      new Date(data.generated_at).toLocaleTimeString();
  }

  function renderHistory(data) {
    var points = data.points || [];
    chartEl.replaceChildren();

    if (points.length === 0) {
      detailHint.textContent = "No history recorded yet.";
      return;
    }

    var peak = points.reduce(function (max, p) {
      return Math.max(max, p.latency_ms || 0);
    }, 1);

    points.forEach(function (point) {
      var isUp = point.up === 1;
      var column = el("div", "bar-col" + (isUp ? "" : " down"));
      var height = isUp ? Math.max(3, (point.latency_ms / peak) * 100) : 100;
      column.style.height = height + "%";
      column.title = new Date(point.checked_at).toLocaleString() + " — " +
        (isUp ? fmtLatency(point.latency_ms) : "down");
      chartEl.appendChild(column);
    });

    detailHint.textContent = points.length + " checks shown, peak latency " +
      fmtLatency(peak) + ". Red columns are failed checks.";
  }

  function loadHistory(name) {
    fetch("/api/history?target=" + encodeURIComponent(name))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        detailTitle.textContent = "History — " + name;
        detailEl.hidden = false;
        renderHistory(data);
      })
      .catch(function () {
        detailHint.textContent = "Could not load history.";
      });
  }

  function poll() {
    fetch("/api/status")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        render(data);
        if (selected) loadHistory(selected);
      })
      .catch(function () {
        summaryEl.textContent = "connection lost — retrying";
      });
  }

  poll();
  setInterval(poll, REFRESH_MS);
})();
