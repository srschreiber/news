/* Client-rendered Daily/Weekly/Monthly story widget. Reads a pre-aggregated
 * {daily, weekly, monthly} payload embedded in the page (already sorted +
 * deduped server-side) and lets the reader switch period and sort order
 * without a page reload. One widget per page. No fetch, no dependencies. */
(function () {
  "use strict";

  var mount = document.getElementById("period-view");
  var dataEl = document.getElementById("period-view-data");
  if (!mount || !dataEl) return;

  var DATA;
  try {
    DATA = JSON.parse(dataEl.textContent);
  } catch (e) {
    mount.textContent = "Couldn't load stories.";
    return;
  }

  var prefix = mount.getAttribute("data-prefix") || "";
  var PERIODS = ["daily", "weekly", "monthly"];
  var LABELS = { daily: "Daily", weekly: "Weekly", monthly: "Monthly" };
  var period = PERIODS.find(function (p) { return (DATA[p] || []).length > 0; }) || "daily";
  var sortBy = "importance";
  var topicFilter = "";

  var ALL_TOPICS = Array.from(PERIODS.reduce(function (set, p) {
    (DATA[p] || []).forEach(function (r) { (r.topics || []).forEach(function (t) { set.add(t); }); });
    return set;
  }, new Set())).sort();

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function meter(score) {
    var n = Math.max(1, Math.min(5, score | 0));
    var bars = "";
    for (var i = 0; i < 5; i++) bars += i < n ? '<i class="on"></i>' : "<i></i>";
    return '<span class="imp imp-' + n + '" title="Importance ' + n +
      '/5" aria-label="Importance ' + n + ' of 5">' + bars + "</span>";
  }

  function rows() {
    var list = (DATA[period] || []).slice();
    if (topicFilter) {
      list = list.filter(function (r) { return (r.topics || []).indexOf(topicFilter) !== -1; });
    }
    if (sortBy === "date") {
      list.sort(function (a, b) {
        if (a.date !== b.date) return a.date < b.date ? 1 : -1;
        return (b.importance || 0) - (a.importance || 0);
      });
    }
    return list;
  }

  function render() {
    var list = rows();
    var html = '<div class="pv-toolbar">';
    html += '<div class="pv-periods">';
    PERIODS.forEach(function (p) {
      var disabled = (DATA[p] || []).length === 0;
      html += '<button type="button" class="pv-btn' + (p === period ? " pv-active" : "") + '"' +
        (disabled ? " disabled" : "") + ' data-period="' + p + '">' + LABELS[p] + "</button>";
    });
    html += "</div>";
    if (ALL_TOPICS.length > 1) {
      html += '<div class="pv-filter"><label>Topic ' +
        '<select id="pv-topic-select"><option value="">All topics</option>';
      ALL_TOPICS.forEach(function (t) {
        html += '<option value="' + esc(t) + '"' + (t === topicFilter ? " selected" : "") + ">" +
          esc(t) + "</option>";
      });
      html += "</select></label></div>";
    }
    html += '<div class="pv-sort"><label>Sort ' +
      '<select id="pv-sort-select">' +
      '<option value="importance"' + (sortBy === "importance" ? " selected" : "") + ">Importance</option>" +
      '<option value="date"' + (sortBy === "date" ? " selected" : "") + ">Date</option>" +
      "</select></label></div></div>";

    if (!list.length) {
      html += '<p class="pv-empty">No stories in this window yet.</p>';
    } else {
      html += '<ul class="pv-list">';
      list.forEach(function (r) {
        var href = prefix + r.url;
        var badge = r.researched ? ' <span class="src-badge src-research">AI Researched</span>' : "";
        var feedBadges = (r.feeds || []).map(function (f) {
          return '<span class="pv-feed-badge">' + esc(f) + "</span>";
        }).join(" ");
        var topics = (r.topics || []).join(", ");
        var dateNote = period !== "daily" ? '<span class="pv-date">' + esc(r.date) + "</span>" : "";
        html += '<li class="pv-item">';
        html += '<div class="pv-title">' + meter(r.importance) + ' <a href="' + href + '">' +
          esc(r.title) + "</a>" + badge + "</div>";
        if (r.summary) html += '<div class="pv-summary">' + esc(r.summary) + "</div>";
        var metaParts = [];
        if (topics) metaParts.push("<em>" + esc(topics) + "</em>");
        if (dateNote) metaParts.push(dateNote);
        if (feedBadges) metaParts.push(feedBadges);
        if (metaParts.length) html += '<div class="pv-meta">' + metaParts.join(" · ") + "</div>";
        if (r.takeaways && r.takeaways.length) {
          html += '<ul class="takeaways">';
          r.takeaways.forEach(function (t) { html += "<li>" + esc(t) + "</li>"; });
          html += "</ul>";
        }
        if (r.sources && r.sources.length) {
          var srcs = r.sources.map(function (s) {
            var label = s.origin === "research" ? "Web Search" : "RSS";
            return '<a href="' + esc(s.url) + '" rel="noopener" target="_blank">' + esc(s.label) +
              '</a> <span class="src-badge src-' + esc(s.origin || "rss") + '">' + label + "</span>";
          }).join(", ");
          html += '<div class="pv-sources">Sources: ' + srcs + "</div>";
        }
        html += "</li>";
      });
      html += "</ul>";
    }
    mount.innerHTML = html;

    PERIODS.forEach(function (p) {
      var btn = mount.querySelector('[data-period="' + p + '"]');
      if (btn && !btn.disabled) {
        btn.addEventListener("click", function () { period = p; render(); });
      }
    });
    var sel = document.getElementById("pv-sort-select");
    if (sel) sel.addEventListener("change", function () { sortBy = sel.value; render(); });
    var topicSel = document.getElementById("pv-topic-select");
    if (topicSel) topicSel.addEventListener("change", function () { topicFilter = topicSel.value; render(); });
  }

  render();
})();
