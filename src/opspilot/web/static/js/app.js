// OpsPilot UI behaviour. Progressive: every page works without this file; the server
// enforces every rule. No business logic here (rules.md §13).
(() => {
  "use strict";

  // Queue tables: ↑/↓ move the selected row, Home/End jump, Enter opens. Grid keys act only
  // while the table has focus, so there are no global single-key shortcuts (WCAG 2.1.4).
  for (const table of document.querySelectorAll("table[data-queue]")) {
    const rows = [...table.querySelectorAll("tbody tr[data-href]")];
    if (!rows.length) continue;
    table.tabIndex = 0;
    table.setAttribute("aria-label", (table.querySelector("caption")?.textContent || "Cases") + ". Use arrow keys to move and Enter to open.");
    let index = -1;
    const select = (next) => {
      rows[index]?.classList.remove("selected");
      index = Math.max(0, Math.min(rows.length - 1, next));
      rows[index].classList.add("selected");
      rows[index].scrollIntoView({ block: "nearest" });
    };
    table.addEventListener("keydown", (event) => {
      const moves = { ArrowDown: index + 1, ArrowUp: index - 1, Home: 0, End: rows.length - 1 };
      if (event.key in moves) { event.preventDefault(); select(moves[event.key]); }
      else if (event.key === "Enter" && index >= 0 && event.target === table) { window.location.assign(rows[index].dataset.href); }
    });
    for (const row of rows) {
      row.addEventListener("click", (event) => {
        if (event.target.closest("a, button, input, select, label")) return;
        window.location.assign(row.dataset.href);
      });
    }
  }

  // Approval review: approval stays locked until every resolution is accepted.
  for (const form of document.querySelectorAll("form[data-review]")) {
    const boxes = [...form.querySelectorAll("input[data-accept]")];
    const button = form.querySelector("[data-approve]");
    const progress = form.querySelector("[data-review-progress]");
    if (!boxes.length || !button) continue;
    const update = () => {
      // The resolution chain's 2D fallback: a link turns solid once its resolution is accepted.
      for (const box of boxes) {
        for (const link of document.querySelectorAll(`[data-accept-link="${box.id}"]`)) link.classList.toggle("accepted", box.checked);
      }
      const done = boxes.filter((b) => b.checked).length;
      const all = done === boxes.length;
      button.disabled = !all;
      if (progress) progress.textContent = all ? `All ${boxes.length} accepted.` : `${done} of ${boxes.length} accepted. Accept all ${boxes.length} to approve.`;
    };
    boxes.forEach((b) => b.addEventListener("change", update));
    update();
  }

  // Copy buttons: "Copied" in text for 2 s.
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-copy]");
    if (!button) return;
    const status = button.parentElement.querySelector("[data-copied]");
    try {
      await navigator.clipboard.writeText(button.dataset.copy);
      if (status) { status.textContent = "Copied"; setTimeout(() => { status.textContent = ""; }, 2000); }
    } catch { if (status) status.textContent = "Copy failed"; }
  });

  // Reason fields: a live count of 1000.
  for (const field of document.querySelectorAll("textarea[data-count]")) {
    const counter = document.getElementById(field.getAttribute("aria-describedby"));
    const show = () => { if (counter) counter.textContent = `${field.value.length} of ${field.maxLength}`; };
    field.addEventListener("input", show);
    show();
  }

  // Busy state: one submit per form, the button says what is happening.
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (form.method.toLowerCase() !== "post") return;
    const button = event.submitter || form.querySelector("button[type=submit]");
    if (!button) return;
    if (form.dataset.busy) { event.preventDefault(); return; }
    form.dataset.busy = "1";
    button.setAttribute("aria-busy", "true");
    button.dataset.label = button.textContent;
    setTimeout(() => { button.disabled = true; button.textContent = "Submitting…"; }, 0);
  });

  // A link to a disclosure ("Request recheck") opens it, so its fields can be reached.
  const openTarget = () => {
    const target = location.hash && document.getElementById(location.hash.slice(1));
    if (target && target.tagName === "DETAILS") { target.open = true; target.querySelector("summary")?.focus(); }
  };
  window.addEventListener("hashchange", openTarget);
  openTarget();

  // Evidence: an optional 3D view below the table (owner decision, P13). The table is the
  // default and authoritative view and never changes; without script the link reloads.
  for (const toggle of document.querySelectorAll("[data-evidence-3d]")) {
    toggle.addEventListener("click", (event) => {
      const figure = document.getElementById(toggle.getAttribute("aria-controls"));
      if (!figure) return;
      event.preventDefault();
      const show = figure.hidden;
      figure.hidden = !show;
      toggle.setAttribute("aria-pressed", String(show));
      toggle.textContent = show ? "Hide 3D view" : "Show 3D view";
      if (show) start3d();
    });
  }

  // 3D scenes (07-design.md §11): the stage loads only once a scene comes into view and 3D
  // views are on. Until then, and whenever 3D can't run, each scene shows its 2D fallback.
  let stageLoaded = false;
  function start3d() {
    if (stageLoaded || document.documentElement.dataset.views3d !== "on" || !("IntersectionObserver" in window)) return;
    const figures = [...document.querySelectorAll("figure[data-scene]")];
    if (!figures.length) return;
    const first = new IntersectionObserver((entries) => {
      if (stageLoaded || !entries.some((e) => e.isIntersecting)) return;
      stageLoaded = true;
      first.disconnect();
      import("/static/js/stage3d.js").then((stage) => stage.start()).catch(() => {});
    });
    figures.forEach((f) => first.observe(f));
  }
  start3d();

  // Session idle warning, 2 minutes before the server ends an idle session.
  const idle = Number(document.body.dataset.idleSeconds || 0);
  const warning = document.querySelector("[data-idle-warning]");
  if (idle > 180 && warning) setTimeout(() => { warning.hidden = false; }, (idle - 120) * 1000);
})();
