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

  // Build ordered history: [{title, url, date, summary, takeaways}, ...] newest first.
  // Returns single-element array if no prior versions are in the loaded index.
  function buildChain(r) {
    var chain = [], cur = r, seen = {};
    while (cur && !seen[cur.url] && chain.length < 7) {
      seen[cur.url] = true;
      chain.push({ title: cur.title, url: cur.url, date: cur.date || "",
                   summary: cur.summary || "", takeaways: cur.takeaways || [] });
      if (!cur.updatesUrl) break;
      cur = ALL_BY_URL[cur.updatesUrl] || null;
      if (!cur) break;
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

  // Deep-link: ?story=<encoded-url> — jump to that card on load.
  var storyTarget = urlParams.get("story") || "";
  var storyHighlighted = false;
  if (storyTarget) {
    // Switch to whichever period contains this story
    PERIODS.forEach(function (p) {
      if ((DATA[p] || []).some(function (r) { return r.url === storyTarget; })) period = p;
    });
    feedFilter = "";
    subfeedFilter = "";
    // Jump to the page that contains the card
    var storyList = (DATA[period] || []);
    for (var si = 0; si < storyList.length; si++) {
      if (storyList[si].url === storyTarget) {
        page = Math.ceil((si + 1) / PAGE_SIZE);
        break;
      }
    }
  }

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
    return items.map(function (t) {
      return '<span class="' + cls + '" data-' + dataAttr + '="' + esc(t.key) + '">' +
        esc(t.title) + "</span>";
    }).join(" ");
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
        var chain = buildChain(r);
        var isUpdate = chain.length > 1;
        var cardDepth = Math.min(chain.length - 1, 3);
        var cardClass = "pv-card" + (isUpdate ? " pv-card--update" : "");
        html += '<div class="' + cardClass + '" data-depth="' + cardDepth + '" data-card-index="' + i + '"' +
          ' data-story-url="' + esc(r.url) + '"' +
          (isUpdate ? ' data-chain="' + esc(JSON.stringify(chain)) + '"' : '') + '>';

        // Share button — absolutely positioned in upper-right corner
        html += '<button type="button" class="share-link" title="Copy link" aria-label="Copy link to this story">🔗</button>';

        // Title
        html += '<div class="pv-title">' +
          '<span class="pv-title-text" data-url="' + href + '">' + esc(r.title) + "</span>" +
          (dateNote ? " " + dateNote : "") +
          '</div>';

        html += '<div class="pv-summary">' + (r.summary ? esc(r.summary) : "") + "</div>";

        // Key Takeaways: all visible
        if (r.takeaways && r.takeaways.length) {
          html += '<div class="pv-takeaways-block">';
          html += '<div class="pv-section-label">Key Takeaways</div>';
          html += '<ul class="takeaways pv-takeaways-list">';
          r.takeaways.forEach(function (t) { html += "<li>" + esc(t) + "</li>"; });
          html += "</ul>";
          html += '</div>';
        }

        // Footer row 1: badges
        var badgesHtml = badgeRow("Feed", r.feeds, "pv-feed-badge", "filter-feed") +
          badgeRow("Subfeed", r.subfeeds, "pv-subfeed-badge", "filter-subfeed");
        if (badgesHtml) html += '<div class="pv-card-footer pv-footer-badges">' + badgesHtml + "</div>";

        // Footer row 2: sources + freshness
        var srcParts = [];
        if (r.sources && r.sources.length) {
          var srcs = r.sources;
          var mkLink = function (s) {
            return s.url
              ? '<a class="pv-src" href="' + esc(s.url) + '" target="_blank" rel="noopener">' + esc(s.label || "Source") + '</a>'
              : '<span class="pv-src">' + esc(s.label || "Source") + '</span>';
          };
          if (srcs.length === 1) {
            srcParts.push(mkLink(srcs[0]));
          } else {
            var restLinks = srcs.slice(1).map(mkLink).join(" · ");
            srcParts.push(
              mkLink(srcs[0]) +
              ' <button class="pv-src-toggle-btn" type="button" data-count="' + (srcs.length - 1) +
              '">+' + (srcs.length - 1) + ' more</button>' +
              '<span class="pv-src-rest" hidden> · ' + restLinks + '</span>'
            );
          }
        }
        var localReceived = localTime(r.receivedAt);
        if (localReceived) srcParts.push('<span class="pv-received">Updated ' + localReceived + "</span>");
        if (srcParts.length) html += '<div class="pv-card-footer">' + srcParts.join(" · ") + "</div>";

        if (r.relatedTitle && r.relatedUrl) {
          html += '<div class="pv-related">See also: <a href="' + prefix + r.relatedUrl + '">' +
            esc(r.relatedTitle) + "</a></div>";
        }

        // Clickable timeline at card bottom — only when prior versions exist in the loaded index
        if (isUpdate) {
          html += '<div class="pv-timeline">';
          chain.forEach(function (node, idx) {
            if (idx > 0) html += '<span class="pv-tl-sep">──</span>';
            html += '<span class="pv-tl-node' + (idx === 0 ? ' pv-tl-current' : '') +
              '" data-chain-idx="' + idx + '" data-date="' + esc(fmtDate(node.date)) +
              '" role="button" tabindex="0">' +
              (idx === 0 ? '●' : '○') + ' ' + esc(fmtDate(node.date)) + '</span>';
          });
          html += '</div>';
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

    // Highlight the deep-linked card on first render
    if (storyTarget && !storyHighlighted) {
      storyHighlighted = true;
      var cards = mount.querySelectorAll("[data-story-url]");
      for (var ci = 0; ci < cards.length; ci++) {
        if (cards[ci].getAttribute("data-story-url") === storyTarget) {
          cards[ci].scrollIntoView({ behavior: "smooth", block: "center" });
          cards[ci].classList.add("pv-card--highlight");
          setTimeout((function (c) {
            return function () { c.classList.remove("pv-card--highlight"); };
          })(cards[ci]), 2500);
          break;
        }
      }
    }
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
    mount.querySelectorAll(".share-link").forEach(function (btn) {
      var card = btn.closest(".pv-card");
      var storyUrl = card ? card.getAttribute("data-story-url") : "";
      var deepLink = location.origin + location.pathname +
        (storyUrl ? "?story=" + encodeURIComponent(storyUrl) : "");
      btn.addEventListener("click", function () {
        var flash = function () {
          btn.classList.add("copied");
          btn.textContent = "✓";
          setTimeout(function () {
            btn.classList.remove("copied");
            btn.textContent = "🔗";
          }, 1400);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(deepLink).then(flash, flash);
        } else {
          flash();
        }
      });
    });
    mount.querySelectorAll(".pv-src-toggle-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var rest = btn.nextElementSibling;
        if (!rest) return;
        var nowHidden = !rest.hidden;
        rest.hidden = nowHidden;
        btn.textContent = nowHidden ? "+" + btn.getAttribute("data-count") + " more" : "−";
      });
    });
    mount.querySelectorAll(".pv-tl-node[data-chain-idx]").forEach(function (node) {
      node.addEventListener("click", function () {
        var card = node.closest(".pv-card");
        if (!card) return;
        var chainData;
        try { chainData = JSON.parse(card.getAttribute("data-chain") || "[]"); } catch (e) { return; }
        var idx = parseInt(node.getAttribute("data-chain-idx"), 10);
        var item = chainData[idx];
        if (!item) return;

        // Update timeline markers
        card.querySelectorAll(".pv-tl-node").forEach(function (n) {
          n.classList.remove("pv-tl-current");
          n.textContent = "○ " + n.getAttribute("data-date");
        });
        node.classList.add("pv-tl-current");
        node.textContent = "● " + node.getAttribute("data-date");

        // Swap card content to the selected version
        var titleEl = card.querySelector(".pv-title-text");
        if (titleEl) {
          titleEl.textContent = item.title;
          titleEl.setAttribute("data-url", prefix + item.url);
        }
        var summaryEl = card.querySelector(".pv-summary");
        if (summaryEl) summaryEl.textContent = item.summary || "";

        var ul = card.querySelector(".pv-takeaways-list");
        if (ul) ul.innerHTML = (item.takeaways || []).map(function (t) { return "<li>" + esc(t) + "</li>"; }).join("");
      });
    });
  }

  render();
})();
