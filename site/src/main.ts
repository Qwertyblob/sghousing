// Entry point: hash routing between the six pages, the theme toggle, and focus handling.

import "./styles.css";
import { parseHash } from "./route.ts";

export interface PageModule {
  title: string;
  /** Replace the contents of `root` (attached, showing "Loading…"); listeners and resources
   * are released when `signal` aborts. */
  render(root: HTMLElement, params: URLSearchParams, signal: AbortSignal): Promise<void>;
}

const pages: Record<string, () => Promise<PageModule>> = {
  overview: () => import("./pages/overview.ts"),
  trends: () => import("./pages/trends.ts"),
  map: () => import("./pages/map.ts"),
  lease: () => import("./pages/lease.ts"),
  predict: () => import("./pages/predict.ts"),
  about: () => import("./pages/about.ts"),
};

const main = document.getElementById("main") as HTMLElement;
let controller: AbortController | null = null;
let firstRoute = true;

async function route(): Promise<void> {
  const [path, params] = parseHash(location.hash);
  const name = path in pages ? path : "overview";
  controller?.abort();
  const current = (controller = new AbortController());
  for (const link of document.querySelectorAll<HTMLAnchorElement>("[data-route]")) {
    if (link.dataset.route === name) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  // Each route renders into its own root, attached at once so charts can measure it; a page
  // still loading when the user moves on only ever writes into its detached root.
  const root = document.createElement("div");
  root.append(Object.assign(document.createElement("p"), { className: "loading", textContent: "Loading…" }));
  main.replaceChildren(root);
  try {
    const page = await pages[name]!();
    if (current.signal.aborted) return;
    document.title = name === "overview" ? "HDB resale prices" : `${page.title} · HDB resale prices`;
    await page.render(root, params, current.signal);
  } catch (error) {
    if (current.signal.aborted) return;
    console.error(error);
    const message = document.createElement("p");
    message.className = "notice error";
    message.setAttribute("role", "alert");
    message.textContent = "This page could not load its data. Check your connection and reload.";
    root.replaceChildren(message);
  }
  // Move focus to the new page for keyboard and screen reader users, but not on first load.
  if (!firstRoute) main.focus({ preventScroll: false });
  firstRoute = false;
}

// Theme: the toggle stores an explicit choice; without one the OS setting applies.
const toggle = document.getElementById("theme-toggle") as HTMLButtonElement;
const darkQuery = matchMedia("(prefers-color-scheme: dark)");

function isDark(): boolean {
  const chosen = document.documentElement.dataset.theme;
  return chosen ? chosen === "dark" : darkQuery.matches;
}

function updateToggle(): void {
  toggle.textContent = isDark() ? "Light theme" : "Dark theme";
  toggle.setAttribute("aria-label", isDark() ? "Switch to light theme" : "Switch to dark theme");
}

toggle.addEventListener("click", () => {
  const next = isDark() ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem("theme", next);
  } catch {
    // Storage can be unavailable (private mode); the choice then lasts for this page only.
  }
  updateToggle();
  window.dispatchEvent(new Event("themechange"));
});
darkQuery.addEventListener("change", () => {
  updateToggle();
  window.dispatchEvent(new Event("themechange"));
});

updateToggle();
window.addEventListener("hashchange", () => void route());
void route();
