// Small enhancements, in a file because the Content-Security-Policy forbids inline script.
document.addEventListener("DOMContentLoaded", () => {
  // <form data-confirm="Question?"> asks before submitting.
  document.querySelectorAll("form[data-confirm]").forEach((f) =>
    f.addEventListener("submit", (e) => { if (!window.confirm(f.dataset.confirm)) e.preventDefault(); }));
  // <button class="tab" data-tab="x"> shows elements with data-pane="x".
  document.querySelectorAll(".tab[data-tab]").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll("[data-pane]").forEach((p) => { p.hidden = p.dataset.pane !== b.dataset.tab; });
    document.querySelectorAll(".tab[data-tab]").forEach((x) => x.classList.toggle("active", x === b));
  }));
});
