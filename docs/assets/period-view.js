/* Client-rendered Daily/Weekly/Monthly story widget. Reads a pre-aggregated
 * {daily, weekly, monthly, feedMeta} payload embedded in the page (already
 * sorted + deduped server-side) and lets the reader switch period, filter by
 * Feed/Subfeed, sort, and page through results — all without a page reload.
 * One widget per page. No fetch, no dependencies. */
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
  var scopeFeed = mount.getAttribute("data-scope-feed") || "";
  var PERIODS = ["daily", "weekly", "monthly"];
  var LABELS = { daily: "Daily", weekly: "Weekly", monthly: "Monthly" };
  var PAGE_SIZE = 6;
  var feedMeta = DATA.feedMeta || {};

  // Hydrate from the URL (?period=weekly&feed=technology&subfeed=ai&sort=date)
  // so a shared link reproduces the same filtered view. Invalid/stale values
  // (e.g. a period with no data) just fall back to the normal default rather
  // than erroring — a shared link should degrade gracefully, not break.
  var urlParams = new URLSearchParams(location.search);
  var urlPeriod = urlParams.get("period");
  var period = (PERIODS.indexOf(urlPeriod) !== -1 && (DATA[urlPeriod] || []).length > 0)
    ? urlPeriod
    : PERIODS.find(function (p) { return (DATA[p] || []).length > 0; }) || "daily";
  var _validSorts = { date: 1, received: 1, importance: 1 };
  var sortBy = _validSorts[urlParams.get("sort")] ? urlParams.get("sort") : "importance";
  var feedFilter = urlParams.get("feed") || "";
  var subfeedFilter = urlParams.get("subfeed") || "";
  var page = 1;
  // The URL is only rewritten once the reader actually interacts — a plain
  // page load stays a plain URL; hydration above is read-only until then.
  var urlSyncArmed = false;

  function syncUrl() {
    if (!urlSyncArmed) return;
    var p = new URLSearchParams();
    p.set("period", period);
    p.set("sort", sortBy);
    if (feedFilter) p.set("feed", feedFilter);
    if (subfeedFilter) p.set("subfeed", subfeedFilter);
    var newUrl = location.pathname + "?" + p.toString() + location.hash;
    history.replaceState(null, "", newUrl);
  }

  // Feeds present in the data itself (drives the Feed dropdown + its auto-hide).
  var ALL_FEEDS = collectTagged("feeds");
  // Subfeed options: narrowed to the selected feed's configured topics (via
  // feedMeta) when one is selected, else every subfeed present in the data.
  function subfeedOptions() {
    if (feedFilter && feedMeta[feedFilter]) return feedMeta[feedFilter].subfeeds || [];
    return collectTagged("subfeeds");
  }

  function collectTagged(key) {
    var map = {};
    PERIODS.forEach(function (p) {
      (DATA[p] || []).forEach(function (r) {
        (r[key] || []).forEach(function (t) { map[t.key] = t.title; });
      });
    });
    return Object.keys(map).sort().map(function (k) { return { key: k, title: map[k] }; });
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function localTime(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d)) return "";
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function meter(score) {
    var n = Math.max(1, Math.min(5, score | 0));
    var bars = "";
    for (var i = 0; i < 5; i++) bars += i < n ? '<i class="on"></i>' : "<i></i>";
    return '<span class="imp imp-' + n + '" title="Importance ' + n +
      '/5" aria-label="Importance ' + n + ' of 5">' + bars + "</span>";
  }

  function hasTag(list, key) { return (list || []).some(function (t) { return t.key === key; }); }

  // Counts for the dropdown labels, e.g. "Science (5)" — scoped to the current
  // period. Feed counts ignore the subfeed filter (so switching feeds shows
  // the full picture); subfeed counts respect whichever feed is selected.
  function countBy(key, pool) {
    var counts = {};
    (pool || DATA[period] || []).forEach(function (r) {
      (r[key] || []).forEach(function (t) { counts[t.key] = (counts[t.key] || 0) + 1; });
    });
    return counts;
  }

  function rows() {
    var list = (DATA[period] || []).slice();
    if (feedFilter) list = list.filter(function (r) { return hasTag(r.feeds, feedFilter); });
    if (subfeedFilter) list = list.filter(function (r) { return hasTag(r.subfeeds, subfeedFilter); });
    if (sortBy === "date") {
      list.sort(function (a, b) {
        if (a.date !== b.date) return a.date < b.date ? 1 : -1;
        return (b.importance || 0) - (a.importance || 0);
      });
    } else if (sortBy === "received") {
      list.sort(function (a, b) {
        var ra = a.receivedAt || "", rb = b.receivedAt || "";
        if (ra !== rb) return ra < rb ? 1 : -1;
        return (b.importance || 0) - (a.importance || 0);
      });
    }
    return list;
  }

  function badgeRow(label, items, cls, dataAttr) {
    if (!items || !items.length) return "";
    var badges = items.map(function (t) {
      return '<span class="' + cls + '" data-' + dataAttr + '="' + esc(t.key) + '">' +
        esc(t.title) + "</span>";
    }).join(" ");
    return '<div class="pv-meta-row"><span class="pv-meta-label">' + label + ":</span> " +
      badges + "</div>";
  }

  function render() {
    var all = rows();
    var pageCount = Math.max(1, Math.ceil(all.length / PAGE_SIZE));
    page = Math.max(1, Math.min(page, pageCount));
    var list = all.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

    var html = '<div class="pv-toolbar">';
    html += '<div class="pv-periods">';
    PERIODS.forEach(function (p) {
      var disabled = (DATA[p] || []).length === 0;
      html += '<button type="button" class="pv-btn' + (p === period ? " pv-active" : "") + '"' +
        (disabled ? " disabled" : "") + ' data-period="' + p + '">' + LABELS[p] + "</button>";
    });
    html += "</div>";

    var periodPool = DATA[period] || [];
    html += '<div class="pv-filters">';

    if (!scopeFeed && ALL_FEEDS.length > 1) {
      var feedCounts = countBy("feeds", periodPool);
      html += '<div class="pv-filter"><label>Feed ' +
        '<select id="pv-feed-select"><option value="">All feeds (' + periodPool.length + ")</option>";
      ALL_FEEDS.forEach(function (f) {
        html += '<option value="' + esc(f.key) + '"' + (f.key === feedFilter ? " selected" : "") + ">" +
          esc(f.title) + " (" + (feedCounts[f.key] || 0) + ")</option>";
      });
      html += "</select></label></div>";
    }

    var subPool = feedFilter ? periodPool.filter(function (r) { return hasTag(r.feeds, feedFilter); }) : periodPool;
    var subOpts = subfeedOptions();
    if (subOpts.length > 1) {
      var subCounts = countBy("subfeeds", subPool);
      html += '<div class="pv-filter"><label>Subfeed ' +
        '<select id="pv-subfeed-select"><option value="">All subfeeds (' + subPool.length + ")</option>";
      subOpts.forEach(function (t) {
        html += '<option value="' + esc(t.key) + '"' + (t.key === subfeedFilter ? " selected" : "") + ">" +
          esc(t.title) + " (" + (subCounts[t.key] || 0) + ")</option>";
      });
      html += "</select></label></div>";
    }

    html += '<div class="pv-filter pv-sort"><label>Sort ' +
      '<select id="pv-sort-select">' +
      '<option value="importance"' + (sortBy === "importance" ? " selected" : "") + ">Importance</option>" +
      '<option value="date"' + (sortBy === "date" ? " selected" : "") + ">Date</option>" +
      '<option value="received"' + (sortBy === "received" ? " selected" : "") + ">Last updated</option>" +
      "</select></label></div>";
    html += "</div>";
    if (feedFilter || subfeedFilter) {
      html += '<button type="button" class="pv-clear" id="pv-clear-filters">Clear filters ✕</button>';
    }
    html += "</div>";

    if (!list.length) {
      html += '<p class="pv-empty">No stories in this window yet.</p>';
    } else {
      html += '<ul class="pv-list">';
      list.forEach(function (r, i) {
        var href = prefix + r.url;
        var dateNote = period !== "daily" ? '<span class="pv-date">' + esc(r.date) + "</span>" : "";
        html += '<li class="pv-item">';
        html += '<div class="pv-title">' + meter(r.importance) +
          ' <span class="pv-title-text" data-url="' + href + '">' + esc(r.title) + "</span>" +
          (dateNote ? " " + dateNote : "") +
          ' <button type="button" class="share-link" data-share-index="' + i + '" ' +
          'title="Copy a link to this story" aria-label="Copy a link to this story">🔗</button></div>';
        if (r.summary) html += '<div class="pv-summary">' + esc(r.summary) + "</div>";
        html += badgeRow("Feed", r.feeds, "pv-feed-badge", "filter-feed");
        html += badgeRow("Subfeed", r.subfeeds, "pv-subfeed-badge", "filter-subfeed");
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
          html += '<div class="pv-meta-row pv-sources"><span class="pv-meta-label">Sources:</span> ' +
            srcs + "</div>";
        }
        var localReceived = localTime(r.receivedAt);
        if (localReceived) html += '<div class="pv-received">Received ' + localReceived + "</div>";
        if (r.relatedTitle && r.relatedUrl) {
          html += '<div class="pv-related">See also: <a href="' + prefix + r.relatedUrl + '">' +
            esc(r.relatedTitle) + "</a></div>";
        }
        if (r.updatesTitle && r.updatesUrl) {
          html += '<div class="pv-related">Update to: <a href="' + prefix + r.updatesUrl + '">' +
            esc(r.updatesTitle) + "</a></div>";
        }
        html += "</li>";
      });
      html += "</ul>";

      if (pageCount > 1) {
        html += '<div class="pv-pager">';
        for (var i = 1; i <= pageCount; i++) {
          html += '<button type="button" class="pv-page' + (i === page ? " pv-active" : "") +
            '" data-page="' + i + '">' + i + "</button>";
        }
        html += "</div>";
      }
    }
    mount.innerHTML = html;
    syncUrl();
    bind();
  }

  function bind() {
    PERIODS.forEach(function (p) {
      var btn = mount.querySelector('[data-period="' + p + '"]');
      if (btn && !btn.disabled) {
        btn.addEventListener("click", function () {
          urlSyncArmed = true; period = p; page = 1; render();
        });
      }
    });
    var feedSel = document.getElementById("pv-feed-select");
    if (feedSel) feedSel.addEventListener("change", function () {
      urlSyncArmed = true; feedFilter = feedSel.value; subfeedFilter = ""; page = 1; render();
    });
    var subSel = document.getElementById("pv-subfeed-select");
    if (subSel) subSel.addEventListener("change", function () {
      urlSyncArmed = true; subfeedFilter = subSel.value; page = 1; render();
    });
    var sortSel = document.getElementById("pv-sort-select");
    if (sortSel) sortSel.addEventListener("change", function () {
      urlSyncArmed = true; sortBy = sortSel.value; render();
    });
    var clearBtn = document.getElementById("pv-clear-filters");
    if (clearBtn) clearBtn.addEventListener("click", function () {
      urlSyncArmed = true; feedFilter = ""; subfeedFilter = ""; page = 1; render();
    });
    mount.querySelectorAll("[data-filter-feed]").forEach(function (b) {
      b.addEventListener("click", function () {
        urlSyncArmed = true;
        feedFilter = b.getAttribute("data-filter-feed"); subfeedFilter = ""; page = 1; render();
      });
    });
    mount.querySelectorAll("[data-filter-subfeed]").forEach(function (b) {
      b.addEventListener("click", function () {
        urlSyncArmed = true;
        subfeedFilter = b.getAttribute("data-filter-subfeed"); page = 1; render();
      });
    });
    mount.querySelectorAll("[data-page]").forEach(function (b) {
      b.addEventListener("click", function () {
        page = parseInt(b.getAttribute("data-page"), 10) || 1; render();
        mount.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });
    mount.querySelectorAll("[data-share-index]").forEach(function (btn) {
      var titleEl = btn.parentElement && btn.parentElement.querySelector("[data-url]");
      if (!titleEl) return;
      // Titles aren't links (clicking one would land on a page showing the
      // same info already visible here) — resolve the relative URL to
      // absolute via a detached <a> purely so the copied link still works.
      var resolver = document.createElement("a");
      resolver.href = titleEl.getAttribute("data-url");
      var url = resolver.href;
      btn.addEventListener("click", function () {
        var flash = function () {
          btn.classList.add("copied");
          btn.textContent = "✓ copied";
          setTimeout(function () {
            btn.classList.remove("copied");
            btn.textContent = "🔗";
          }, 1400);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(url).then(flash, flash);
        } else {
          flash();
        }
      });
    });
  }

  render();
})();
