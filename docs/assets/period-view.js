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

  // Index all records by URL for update-chain traversal.
  var ALL_BY_URL = {};
  PERIODS.forEach(function (p) {
    (DATA[p] || []).forEach(function (r) { if (r.url) ALL_BY_URL[r.url] = r; });
  });

  function chainDepth(r) {
    var depth = 0, cur = r, seen = {};
    while (cur && cur.updatesUrl && !seen[cur.updatesUrl] && depth < 6) {
      seen[cur.updatesUrl] = true;
      cur = ALL_BY_URL[cur.updatesUrl];
      depth++;
    }
    return depth;
  }

  // Build ordered history: [{title, url, date}, ...] newest first.
  function buildChain(r) {
    var chain = [], cur = r, seen = {};
    while (cur && !seen[cur.url] && chain.length < 7) {
      seen[cur.url] = true;
      chain.push({ title: cur.title, url: cur.url, date: cur.date || "" });
      if (!cur.updatesUrl) break;
      cur = ALL_BY_URL[cur.updatesUrl] || null;
      if (!cur) {
        // Linked record not in index — use the title/url from parent
        var last = chain[chain.length - 1];
        var parentR = (function () {
          for (var u in ALL_BY_URL) {
            if (ALL_BY_URL[u].updatesUrl === last.url) return null; // avoid re-lookup
          }
          return null;
        })();
        break;
      }
    }
    return chain;
  }

  function fmtDate(iso) {
    if (!iso) return "";
    var d = new Date(iso + "T00:00:00");
    if (isNaN(d)) return iso;
    return d.toLocaleDateString([], { month: "short", day: "numeric" });
  }
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
    var today = new Date();
    var sameDay = d.getFullYear() === today.getFullYear() &&
                  d.getMonth() === today.getMonth() &&
                  d.getDate() === today.getDate();
    var time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    if (sameDay) return time;
    return d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + time;
  }

  function meter(score) {
    var n = Math.max(1, Math.min(10, score));
    var label = (n === Math.floor(n)) ? Math.floor(n) : Math.round(n * 10) / 10;
    var tier = n >= 7 ? "high" : (n >= 4 ? "mid" : "low");
    return '<span class="imp imp-' + tier + '" title="Importance ' + label +
      '/10" aria-label="Importance ' + label + ' of 10">' + label + "</span>";
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
    html += '<p class="pv-rank-note">Ranked by estimated significance, source breadth, and freshness.</p>';
    if (feedFilter || subfeedFilter) {
      html += '<button type="button" class="pv-clear" id="pv-clear-filters">Clear filters ✕</button>';
    }
    html += "</div>";

    if (!list.length) {
      html += '<p class="pv-empty">No stories in this window yet.</p>';
    } else {
      html += '<div class="pv-grid">';
      list.forEach(function (r, i) {
        var href = prefix + r.url;
        var dateNote = period !== "daily" ? '<span class="pv-date">' + esc(r.date) + "</span>" : "";
        var depth = (r.updatesTitle && r.updatesUrl) ? chainDepth(r) : 0;
        var cardClass = "pv-card" + (depth > 0 ? " pv-card--update" : "");
        html += '<div class="' + cardClass + '" data-depth="' + depth + '" data-card-index="' + i + '">';

        // Header row: title + update badge + share
        var updateBadge = depth > 0 ? '<span class="pv-update-badge">↩ Update</span> ' : "";
        html += '<div class="pv-title">' + updateBadge +
          '<span class="pv-title-text" data-url="' + href + '">' + esc(r.title) + "</span>" +
          (dateNote ? " " + dateNote : "") +
          ' <button type="button" class="share-link" data-share-index="' + i + '" ' +
          'title="Copy a link to this story" aria-label="Copy a link to this story">🔗</button></div>';

        if (r.summary) html += '<div class="pv-summary">' + esc(r.summary) + "</div>";

        var badgesHtml = badgeRow("Feed", r.feeds, "pv-feed-badge", "filter-feed") +
          badgeRow("Subfeed", r.subfeeds, "pv-subfeed-badge", "filter-subfeed");
        if (badgesHtml) html += '<div class="pv-badges">' + badgesHtml + "</div>";

        // Takeaways: first one always visible, rest in a toggle
        if (r.takeaways && r.takeaways.length) {
          html += '<div class="pv-takeaway-first">▸ ' + esc(r.takeaways[0]) + "</div>";
          if (r.takeaways.length > 1) {
            html += '<details class="pv-takeaways-details"><summary class="pv-expand-btn">+' +
              (r.takeaways.length - 1) + ' more</summary><ul class="takeaways">';
            r.takeaways.slice(1).forEach(function (t) { html += "<li>" + esc(t) + "</li>"; });
            html += "</ul></details>";
          }
        }

        var footerParts = [];
        if (r.sources && r.sources.length) {
          var srcCount = r.sources.length;
          var srcLabel = srcCount === 1 ? "1 source" : srcCount + " sources";
          var srcNames = r.sources.map(function (s) { return s.label || ""; }).filter(Boolean).join(", ");
          footerParts.push('<span class="pv-src-count"' + (srcNames ? ' title="' + esc(srcNames) + '"' : '') + '>' + srcLabel + '</span>');
        }
        var localReceived = localTime(r.receivedAt);
        if (localReceived) footerParts.push('<span class="pv-received">Updated ' + localReceived + "</span>");
        if (footerParts.length) html += '<div class="pv-card-footer">' + footerParts.join(" &middot; ") + "</div>";

        if (r.relatedTitle && r.relatedUrl) {
          html += '<div class="pv-related">See also: <a href="' + prefix + r.relatedUrl + '">' +
            esc(r.relatedTitle) + "</a></div>";
        }

        // Deck reveal panel — timeline + full history
        if (depth > 0) {
          var chain = buildChain(r);
          // Timeline row: Aug 14 ●── Aug 13 ── Aug 11
          var timelineHtml = '<div class="pv-timeline">';
          chain.forEach(function (node, idx) {
            if (idx > 0) timelineHtml += '<span class="pv-tl-sep">──</span>';
            timelineHtml += '<span class="pv-tl-node' + (idx === 0 ? ' pv-tl-current' : '') + '">' +
              (idx === 0 ? '●' : '○') + ' ' + esc(fmtDate(node.date)) + '</span>';
          });
          timelineHtml += '</div>';

          // History entries: current card bold, previous ones progressively lighter
          var historyHtml = '';
          chain.forEach(function (node, idx) {
            var opacity = Math.max(0.45, 1 - idx * 0.18);
            historyHtml += '<div class="pv-hist-entry" style="opacity:' + opacity + '">' +
              (idx === 0
                ? '<span class="pv-hist-current">' + esc(node.title) + '</span>'
                : '<a href="' + esc(prefix + node.url) + '" class="pv-hist-link">' + esc(node.title) + '</a>') +
              '</div>';
          });

          html += '<div class="pv-prev-panel" id="pv-prev-' + i + '" hidden>' +
            '<div class="pv-prev-label">' + chain.length + ' version' + (chain.length > 1 ? 's' : '') + ' of this story</div>' +
            timelineHtml + historyHtml +
            '</div>';
          html += '<button class="pv-deck-tab" data-target="pv-prev-' + i + '" aria-label="Show story history">◀</button>';
        }

        html += "</div>";
      });
      html += "</div>";

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
    mount.querySelectorAll(".pv-deck-tab").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var targetId = btn.getAttribute("data-target");
        var panel = document.getElementById(targetId);
        if (!panel) return;
        var open = !panel.hidden;
        panel.hidden = open;
        btn.textContent = open ? "◀" : "▶";
        btn.classList.toggle("pv-deck-tab--open", !open);
      });
    });
  }

  render();
})();
