/* Reads api-status.json and shows a dismissible top banner if warnings exist. */
(function () {
  fetch("/assets/api-status.json")
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (!data.warnings || !data.warnings.length) return;
      var bar = document.createElement("div");
      bar.className = "api-warning-bar";
      bar.innerHTML =
        "<strong>⚠ API Warning:</strong> " +
        data.warnings.map(function (w) { return w; }).join(" &nbsp;|&nbsp; ") +
        ' <button class="api-warning-dismiss" onclick="this.parentNode.remove()" ' +
        'aria-label="Dismiss">✕</button>';
      document.body.prepend(bar);
    })
    .catch(function () {});
})();
