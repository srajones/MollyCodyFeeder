/* Shared top navigation for every Poor Puppy web page (one source of truth; served at /nav.js). */
(() => {
  "use strict";
  const LINKS = [
    ["/", "🐶 Feeder", (p) => p === "/" || p === "/ui"],
    ["/label", "🏷 Labels", (p) => p.startsWith("/label")],
    ["/ha", "🏠 Home Assistant", (p) => p === "/ha"],
    ["/system", "⚙️ Settings", (p) => p.startsWith("/system")],
  ];
  const here = location.pathname.replace(/\/+$/, "") || "/";
  const css = document.createElement("style");
  css.textContent = `
    .pp-nav { position: sticky; top: 0; z-index: 50; display: flex; gap: 4px; align-items: center; flex-wrap: wrap;
      padding: 8px max(12px, env(safe-area-inset-left)); background: rgb(15 18 22 / .92); backdrop-filter: blur(8px);
      border-bottom: 1px solid #272e38; font: 15px/1.2 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
    .pp-nav a { color: #b7c0cc; text-decoration: none; padding: 8px 12px; border-radius: 9px; white-space: nowrap; }
    .pp-nav a:hover { background: #1d222a; color: #e8ebf0; }
    .pp-nav a:focus-visible { outline: 2px solid #7cb3ff; outline-offset: 1px; }
    .pp-nav a[aria-current="page"] { background: #233046; color: #e8ebf0; font-weight: 600; }
    .pp-nav.compact { position: static; padding: 0 0 8px; background: none; border: 0; backdrop-filter: none; font-size: 13px; }
    .pp-nav.compact a { padding: 5px 8px; }
    @media (max-width: 520px) { .pp-nav a { padding: 7px 9px; font-size: 14px; } }`;
  document.head.append(css);
  const nav = document.createElement("nav");
  nav.className = "pp-nav";
  nav.setAttribute("aria-label", "Poor Puppy pages");
  nav.innerHTML = LINKS.map(([href, text, active]) =>
    `<a href="${href}"${active(here) ? ' aria-current="page"' : ""}>${text}</a>`).join("");
  const sidebar = document.querySelector("aside header"); // labelling page: full-height sidebar layout
  if (sidebar) { nav.classList.add("compact"); sidebar.prepend(nav); }
  else document.body.prepend(nav);
})();
