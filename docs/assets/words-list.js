// Paginated cards of every past Word of the Day (docs/words.json), newest first.
(function () {
  var mount = document.getElementById("word-list");
  if (!mount) return;
  var PER_PAGE = 10;

  fetch("../words.json")
    .then(function (r) { return r.ok ? r.json() : []; })
    .then(function (all) { render(all.slice().reverse()); })
    .catch(function () { mount.textContent = "Couldn't load the word list."; });

  function render(words) {
    if (!words.length) {
      mount.innerHTML = "<p>No words yet — a new Word of the Day is added daily.</p>";
      return;
    }
    var pages = Math.ceil(words.length / PER_PAGE);
    var page = 0;
    draw();

    function draw() {
      var start = page * PER_PAGE;
      var slice = words.slice(start, start + PER_PAGE);
      var h = '<div class="wl-cards">';
      slice.forEach(function (w) {
        h += '<div class="wl-card">';
        h += '<div class="wl-word">' + esc(w.word) +
          (w.part_of_speech ? ' <span class="wotd-pos">' + esc(w.part_of_speech) + "</span>" : "") +
          "</div>";
        h += '<div class="wl-def">' + esc(w.definition) + "</div>";
        if (w.example) h += '<div class="wl-ex">' + esc(w.example) + "</div>";
        if (w.link) h += '<a class="wl-src" href="' + esc(w.link) +
          '" target="_blank" rel="noopener">Merriam-Webster</a>';
        h += "</div>";
      });
      h += "</div>";
      if (pages > 1) {
        h += '<div class="wl-pager">' +
          '<button class="wq-btn" id="wl-prev"' + (page === 0 ? " disabled" : "") + "># Prev</button>" +
          '<span class="wl-page">Page ' + (page + 1) + " of " + pages + "</span>" +
          '<button class="wq-btn" id="wl-next"' + (page === pages - 1 ? " disabled" : "") + ">Next #</button>" +
          "</div>";
      }
      mount.innerHTML = h.replace("# Prev", "&larr; Prev").replace("Next #", "Next &rarr;");
      var prev = document.getElementById("wl-prev"), next = document.getElementById("wl-next");
      if (prev) prev.onclick = function () { if (page > 0) { page--; draw(); scroll(); } };
      if (next) next.onclick = function () { if (page < pages - 1) { page++; draw(); scroll(); } };
    }
    function scroll() { mount.scrollIntoView({ behavior: "smooth", block: "start" }); }
  }

  function esc(s) { var d = document.createElement("div"); d.textContent = s == null ? "" : s; return d.innerHTML; }
})();
