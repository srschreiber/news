/* Reads api-status.json and shows a dismissible top banner if warnings exist. */
(function () {
  fetch("/assets/api-status.json")
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (!data.warnings || !data.warnings.length) return;
      var bar = document.createElement("div");
      bar.className = "api-warning-bar";
      var strong = document.createElement("strong");
      strong.textContent = "⚠ API Warning: ";
      bar.appendChild(strong);
      bar.appendChild(document.createTextNode(data.warnings.join(" | ")));
      var btn = document.createElement("button");
      btn.className = "api-warning-dismiss";
      btn.setAttribute("aria-label", "Dismiss");
      btn.textContent = "✕";
      btn.onclick = function () { bar.remove(); };
      bar.appendChild(btn);
      document.body.prepend(bar);
    })
    .catch(function () {});
})();
