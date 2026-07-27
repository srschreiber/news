// Word quiz over the last ~15 Words of the Day (docs/words.json). Given a
// definition (with the word blanked out), pick the right word from four choices.
(function () {
  var mount = document.getElementById("word-quiz");
  if (!mount) return;

  fetch("../words.json")
    .then(function (r) { return r.ok ? r.json() : []; })
    .then(function (all) { start(all.slice(-15)); })
    .catch(function () { mount.textContent = "Couldn't load the word list."; });

  function start(words) {
    if (words.length < 4) {
      mount.innerHTML =
        "<p>Not enough Words of the Day yet — a new one is added daily, so " +
        "check back once a few more have accumulated.</p>";
      return;
    }
    var qs = shuffle(words).map(function (w) {
      return { def: w.definition, example: w.example, answer: w.word,
               options: options(w, words) };
    });
    var i = 0, score = 0;
    render();

    function render() {
      if (i >= qs.length) {
        mount.innerHTML =
          '<div class="wq-done">You scored <strong>' + score + " / " + qs.length +
          '</strong><br><button class="wq-btn" id="wq-again">Play again</button></div>';
        document.getElementById("wq-again").onclick = function () {
          i = 0; score = 0; qs = shuffle(qs); render();
        };
        return;
      }
      var q = qs[i];
      var h = '<div class="wq-progress">Question ' + (i + 1) + " of " + qs.length +
              " &middot; Score " + score + "</div>";
      h += '<div class="wq-def">Which word means:<br><strong>' +
           blank(q.def, q.answer) + "</strong></div>";
      h += '<div class="wq-options">';
      q.options.forEach(function (o) {
        h += '<button class="wq-opt" data-w="' + esc(o) + '">' + esc(o) + "</button>";
      });
      h += '</div><div class="wq-feedback"></div>';
      mount.innerHTML = h;

      var fb = mount.querySelector(".wq-feedback");
      mount.querySelectorAll(".wq-opt").forEach(function (btn) {
        btn.onclick = function () {
          var chosen = btn.getAttribute("data-w");
          mount.querySelectorAll(".wq-opt").forEach(function (b) {
            b.disabled = true;
            if (b.getAttribute("data-w") === q.answer) b.classList.add("correct");
            else if (b === btn) b.classList.add("wrong");
          });
          if (chosen === q.answer) {
            score++;
            fb.innerHTML = '<span class="wq-ok">Correct!</span>';
          } else {
            fb.innerHTML = '<span class="wq-no">Nope — it\'s <strong>' +
              esc(q.answer) + "</strong>.</span>";
          }
          if (q.example) fb.innerHTML += '<div class="wq-ex">' + blank(q.example, q.answer) + "</div>";
          fb.innerHTML += '<button class="wq-btn" id="wq-next">Next &rarr;</button>';
          document.getElementById("wq-next").onclick = function () { i++; render(); };
        };
      });
    }
  }

  function options(w, pool) {
    var others = pool.filter(function (x) { return x.word !== w.word; });
    return shuffle(shuffle(others).slice(0, 3).map(function (x) { return x.word; })
      .concat([w.word]));
  }
  // Blank the answer word (and simple inflections) so the definition/example
  // don't give it away.
  function blank(text, word) {
    if (!text) return "";
    var w = word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return esc(text).replace(new RegExp("\\b" + w + "\\w*", "gi"), "&mdash;&mdash;&mdash;");
  }
  function shuffle(a) {
    a = a.slice();
    for (var j = a.length - 1; j > 0; j--) {
      var k = Math.floor(Math.random() * (j + 1)), t = a[j]; a[j] = a[k]; a[k] = t;
    }
    return a;
  }
  function esc(s) { var d = document.createElement("div"); d.textContent = s == null ? "" : s; return d.innerHTML; }
})();
