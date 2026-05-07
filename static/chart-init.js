// SVG sparklines + heatmap helpers. The design renders the APR chart and
// sparklines directly as inline SVG in templates, so this file is a thin
// progressive-enhancement layer for any client-only flourishes.
(function () {
  // Prevent flash-of-unstyled segmented-control click when JS is unavailable.
  document.querySelectorAll(".toggle-group[data-storage-key]").forEach((group) => {
    const key = group.dataset.storageKey;
    const stored = key && localStorage.getItem(key);
    if (stored) {
      group.querySelectorAll("a, button").forEach((el) => {
        el.classList.toggle("active", el.dataset.value === stored);
      });
    }
    group.addEventListener("click", (e) => {
      const el = e.target.closest("[data-value]");
      if (!el || !key) return;
      localStorage.setItem(key, el.dataset.value);
    });
  });
})();
