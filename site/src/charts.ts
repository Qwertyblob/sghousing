// Chart plumbing: theme colours from the CSS tokens, Plot defaults, and re-rendering on
// resize and theme change.

import * as Plot from "@observablehq/plot";

/** The current value of a colour token from styles.css (it changes with the theme). */
export function token(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(`--${name}`).trim();
}

/** Draw into `container` at its width, and again when it resizes or the theme changes,
 * until `signal` aborts. Returns a function that redraws on demand (after a filter change). */
export function responsive(
  container: HTMLElement,
  draw: (width: number) => Element,
  signal: AbortSignal,
): () => void {
  let width = 0;
  let frame = 0;
  const render = () => {
    width = container.clientWidth;
    const chart = draw(Math.max(280, width));
    accessible(chart);
    container.replaceChildren(chart);
  };
  const observer = new ResizeObserver(() => {
    if (container.clientWidth === width) return;
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(render);
  });
  observer.observe(container);
  window.addEventListener("themechange", render, { signal });
  signal.addEventListener("abort", () => observer.disconnect());
  render();
  return render;
}

/** A Plot figure as one labelled image: Plot labels its inner <g> groups, which have no role
 * (an accessibility error), so those labels go; each chart also has a data table. */
function accessible(chart: Element): void {
  const svg = chart.tagName.toLowerCase() === "svg" ? chart : chart.querySelector("svg");
  svg?.setAttribute("role", "img");
  for (const group of chart.querySelectorAll("g[aria-label], g[aria-description]")) {
    group.removeAttribute("aria-label");
    group.removeAttribute("aria-description");
  }
}

/** Shared Plot options: recessive axes in the secondary ink, transparent background. */
export function plotOptions(width: number, height: number, ariaLabel: string): Plot.PlotOptions {
  return {
    width,
    height,
    ariaLabel,
    marginLeft: 64,
    marginRight: 16,
    style: {
      background: "transparent",
      color: token("text-2"),
      fontSize: "12px",
      fontFamily: "inherit",
      overflow: "visible",
    },
  };
}

/** Tooltip box styled for the current theme. */
export function tipStyle(): { fill: string; stroke: string; textColor?: string } {
  return { fill: token("surface"), stroke: token("border") };
}

export { Plot };

/** The hover layer for a line: a crosshair, a dot on the nearest point and a tooltip. */
export function hoverMarks<T>(
  data: T[],
  x: (d: T) => Date | number,
  y: (d: T) => number,
  title: (d: T) => string,
  stroke = token("series-1"),
): Plot.Markish[] {
  return [
    Plot.ruleX(data, Plot.pointerX({ x, stroke: token("reference"), strokeWidth: 1 })),
    Plot.dot(data, Plot.pointerX({ x, y, r: 4, fill: stroke, stroke: token("surface"), strokeWidth: 2 })),
    Plot.tip(data, Plot.pointerX({ x, y, title, ...tipStyle() })),
  ];
}

/** A legend row; identity is never colour alone, so every chart with two series has one. */
export function legend(items: { label: string; color: string; dashed?: boolean }[]): HTMLElement {
  const list = document.createElement("ul");
  list.className = "legend";
  for (const item of items) {
    const swatch = document.createElement("span");
    swatch.className = "swatch";
    swatch.style.background = item.dashed
      ? `repeating-linear-gradient(90deg, ${item.color} 0 4px, transparent 4px 7px)`
      : item.color;
    const entry = document.createElement("li");
    entry.append(swatch, item.label);
    list.append(entry);
  }
  return list;
}
