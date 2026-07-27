// Adds a "copy link" button to each story heading on briefing pages so a reader
// can share a direct link that scrolls straight to that story. Deep-linking
// itself already works (each heading has a slug anchor) — this just makes it
// obvious and one-click.
(function () {
  if (!/\/(news|weekly|monthly|yearly)\//.test(location.pathname)) return;
  document.addEventListener("DOMContentLoaded", function () {
    var content = document.querySelector(".md-content__inner");
    if (!content) return;
    content.querySelectorAll("h3[id]").forEach(function (h) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "share-link";
      btn.title = "Copy a link to this story";
      btn.setAttribute("aria-label", "Copy a link to this story");
      btn.textContent = "🔗";
      btn.addEventListener("click", function () {
        var url = location.origin + location.pathname + "#" + h.id;
        if (history.replaceState) history.replaceState(null, "", "#" + h.id);
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
      h.appendChild(btn);
    });
  });
})();
