// A tiny hash router: "#/", "#/speakers", ... Each route is a view with
//   mount(root)     build the view's DOM into `root`
//   update(state)   show the current state (called after mount and on every state change)
//   unmount()       optional clean-up (timers, listeners)
//   canLeave()      optional; return false to keep the user on the view (unsaved edits)
// Hash routes need no server support, so reloading or bookmarking a view just works.

import { state, subscribe } from "./store.js";

const SUFFIX = "Bluetooth Splitter";

export function startRouter({ routes, outlet, nav }) {
  let current = null; // { route, view }
  let reverting = false;
  let first = true;

  const pathOf = () => location.hash.replace(/^#/, "") || "/";

  function show(route) {
    if (current) {
      current.view.unmount?.();
      outlet.replaceChildren();
    }
    const view = route.view;
    current = { route, view };
    view.mount(outlet);
    view.update(state);
    document.title = `${route.title} · ${SUFFIX}`;
    for (const link of nav.querySelectorAll("a[data-route]")) {
      if (link.dataset.route === route.path) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
    if (!first) {
      window.scrollTo(0, 0);
      outlet.focus({ preventScroll: true }); // screen readers: start at the new view
    }
    first = false;
  }

  function resolve() {
    if (reverting) {
      reverting = false;
      return;
    }
    const wanted = routes.find((r) => r.path === pathOf());
    const route = wanted || routes[0];
    if (!wanted) history.replaceState(null, "", "#" + route.path);
    if (current && route !== current.route && current.view.canLeave && !current.view.canLeave()) {
      reverting = true; // put the old address back without leaving the view
      location.hash = "#" + current.route.path;
      return;
    }
    if (current && route === current.route) return;
    show(route);
  }

  subscribe((s) => current?.view.update(s));
  window.addEventListener("hashchange", resolve);
  resolve();
}
