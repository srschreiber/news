// Paginated cards of every past Fact of the Day (docs/funfacts.json), newest first.
(function () {
  var mount = document.getElementById("fact-list");
  if (!mount) return;
  var PER_PAGE = 15;

  fetch("../funfacts.json")
    .then(function (r) { return r.ok ? r.json() : []; })
    .then(function (all) { render(all.slice().reverse()); })
    .catch(function () { mount.textContent = "Couldn't load the facts."; });

  function render(facts) {
    if (!facts.length) {
      mount.innerHTML = "<p>No facts yet — a new one is added daily.</p>";
      return;
    }
    var pages = Math.ceil(facts.length / PER_PAGE);
    var page = 0;
    draw();

    function draw() {
      var slice = facts.slice(page * PER_PAGE, page * PER_PAGE + PER_PAGE);
      var h = '<div class="wl-cards">';
      slice.forEach(function (f) {
        h += '<div class="wl-card">';
        if (f.date) h += '<div class="wl-date">' + esc(f.date) + "</div>";
        h += '<div class="wl-def">' + esc(f.text) + "</div></div>";
      });
      h += "</div>";
      if (pages > 1) {
        h += '<div class="wl-pager">' +
          '<button class="wq-btn" id="fl-prev"' + (page === 0 ? " disabled" : "") + ">&larr; Prev</button>" +
          '<span class="wl-page">Page ' + (page + 1) + " of " + pages + "</span>" +
          '<button class="wq-btn" id="fl-next"' + (page === pages - 1 ? " disabled" : "") + ">Next &rarr;</button></div>";
      }
      mount.innerHTML = h;
      var prev = document.getElementById("fl-prev"), next = document.getElementById("fl-next");
      if (prev) prev.onclick = function () { if (page > 0) { page--; draw(); mount.scrollIntoView({ behavior: "smooth", block: "start" }); } };
      if (next) next.onclick = function () { if (page < pages - 1) { page++; draw(); mount.scrollIntoView({ behavior: "smooth", block: "start" }); } };
    }
  }

  function esc(s) { var d = document.createElement("div"); d.textContent = s == null ? "" : s; return d.innerHTML; }
})();
