/* Client-side keyword search over docs/search-index.json.
 * Keyword-weighted relevance (BM25-lite) + date-range + topic + importance.
 * Runs only on the search page (guards on #news-search). No dependencies. */
(function () {
  "use strict";

  var root = document.getElementById("news-search");
  if (!root) return; // not the search page

  var els = {
    q: document.getElementById("ns-q"),
    topic: document.getElementById("ns-topic"),
    from: document.getElementById("ns-from"),
    to: document.getElementById("ns-to"),
    imp: document.getElementById("ns-imp"),
    sort: document.getElementById("ns-sort"),
    status: document.getElementById("ns-status"),
    results: document.getElementById("ns-results"),
  };

  var INDEX = [];

  function meter(score) {
    score = Math.max(1, Math.min(5, score | 0));
    return "🔥".repeat(score) + "◯".repeat(5 - score);
  }

  function terms(s) {
    return (s || "")
      .toLowerCase()
      .split(/[^a-z0-9.+#]+/)
      .filter(function (t) { return t.length > 1; });
  }

  // Field-weighted term match: keywords > title > theme/topic.
  function score(rec, qterms) {
    if (!qterms.length) return 0;
    var kw = (rec.keywords || []).join(" ").toLowerCase();
    var title = (rec.title || "").toLowerCase();
    var meta = ((rec.theme || "") + " " + (rec.topic || "")).toLowerCase();
    var s = 0;
    qterms.forEach(function (t) {
      if (kw.indexOf(t) !== -1) s += 3;
      if (title.indexOf(t) !== -1) s += 2;
      if (meta.indexOf(t) !== -1) s += 1;
    });
    return s;
  }

  function render() {
    var qterms = terms(els.q.value);
    var topic = els.topic.value;
    var from = els.from.value; // YYYY-MM-DD or ""
    var to = els.to.value;
    var minImp = parseInt(els.imp.value, 10) || 1;
    var sort = els.sort.value;

    var rows = INDEX.filter(function (r) {
      if (topic && r.topic !== topic) return false;
      if ((r.importance || 0) < minImp) return false;
      if (from && r.date < from) return false;
      if (to && r.date > to) return false;
      return true;
    }).map(function (r) {
      return { rec: r, s: score(r, qterms) };
    });

    if (qterms.length) rows = rows.filter(function (x) { return x.s > 0; });

    rows.sort(function (a, b) {
      if (sort === "date") return a.rec.date < b.rec.date ? 1 : -1;
      if (sort === "importance")
        return (b.rec.importance || 0) - (a.rec.importance || 0);
      // relevance
      if (b.s !== a.s) return b.s - a.s;
      if (a.rec.date !== b.rec.date) return a.rec.date < b.rec.date ? 1 : -1;
      return (b.rec.importance || 0) - (a.rec.importance || 0);
    });

    els.results.innerHTML = "";
    rows.slice(0, 200).forEach(function (x) {
      var r = x.rec;
      var li = document.createElement("li");
      var sources = (r.sources || [])
        .map(function (s) {
          return '<a href="' + s.url + '" rel="noopener">' + escapeHtml(s.label) + "</a>";
        })
        .join(" · ");
      var kws = (r.keywords || [])
        .map(function (k) { return '<span class="ns-kw">' + escapeHtml(k) + "</span>"; })
        .join("");
      li.innerHTML =
        '<div class="ns-title">' + meter(r.importance) + " " +
        '<a href="../' + r.url + '">' + escapeHtml(r.title) + "</a></div>" +
        '<div class="ns-meta">' + escapeHtml(r.date) + " · " + escapeHtml(r.topic) +
        (r.theme ? " · " + escapeHtml(r.theme) : "") +
        (sources ? " · " + sources : "") + "</div>" +
        (kws ? '<div class="ns-meta">' + kws + "</div>" : "");
      els.results.appendChild(li);
    });

    els.status.textContent =
      rows.length + " result" + (rows.length === 1 ? "" : "s") +
      (INDEX.length ? " of " + INDEX.length + " indexed events" : "");
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function debounce(fn, ms) {
    var t;
    return function () {
      clearTimeout(t);
      t = setTimeout(fn, ms);
    };
  }

  // Fetch the index (relative to /search/ → site root).
  fetch("../search-index.json")
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (data) {
      INDEX = Array.isArray(data) ? data : [];
      var topics = Array.from(new Set(INDEX.map(function (r) { return r.topic; }))).sort();
      topics.forEach(function (t) {
        var o = document.createElement("option");
        o.value = t;
        o.textContent = t;
        els.topic.appendChild(o);
      });
      // Prefill query from ?q=
      var q = new URLSearchParams(location.search).get("q");
      if (q) els.q.value = q;
      render();
    })
    .catch(function (e) {
      els.status.textContent = "Could not load search index (" + e.message + ").";
    });

  var onChange = debounce(render, 120);
  ["q", "topic", "from", "to", "imp", "sort"].forEach(function (k) {
    els[k].addEventListener("input", onChange);
    els[k].addEventListener("change", onChange);
  });
})();
