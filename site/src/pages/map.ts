// Map: a choropleth of planning areas (median price per sqm, or its change on the year
// before). Hover shows a tooltip; clicking an area, or choosing it from the list, opens a
// side panel with its yearly trend. There is no basemap: the boundaries are the map.

import type { ExpressionSpecification, GeoJSONSourceSpecification, Map as MapLibreMap, MapLayerMouseEvent } from "maplibre-gl";
import { hoverMarks, plotOptions, Plot, responsive, token } from "../charts.ts";
import { loadJson, loadMeta, type AreaProperties, type PlanningAreas } from "../data.ts";
import { dataTable, h, notice, segmented, select, startPage, table } from "../dom.ts";
import * as fmt from "../format.ts";
import { setParams } from "../route.ts";

export const title = "Map";

type Metric = "psm" | "yoy";

interface Period {
  label: string;
  psm: number | null;
  sales: number;
}

const METRICS = [
  { value: "psm", label: "Price per sqm" },
  { value: "yoy", label: "Change on year before" },
];
const SEQUENTIAL = ["seq-0", "seq-1", "seq-2", "seq-3", "seq-4"];
const SINGAPORE: [[number, number], [number, number]] = [
  [103.6, 1.16],
  [104.09, 1.48],
];

const property = (metric: Metric) => (metric === "psm" ? "median_psm_12m" : "yoy_change");

function valueText(p: AreaProperties, metric: Metric): string {
  if (metric === "psm") return p.median_psm_12m === null ? "No HDB resales" : `${fmt.psm(p.median_psm_12m)} per sqm`;
  return p.yoy_change === null ? "Too few resales to compare" : `${fmt.signedPercent(p.yoy_change)} on the year before`;
}

export async function render(root: HTMLElement, params: URLSearchParams, signal: AbortSignal): Promise<void> {
  const showBody = startPage(root, "Map", "Median resale price per square metre by URA planning area over the latest 12 months, or its change on the 12 months before.");
  const [areas, meta] = await Promise.all([loadJson<PlanningAreas>("planning_areas.geojson"), loadMeta()]);
  const byName = new Map(areas.features.map((f) => [f.properties.planning_area, f.properties]));
  const names = [...byName.keys()].sort();
  const boundarySource = meta.sources.find((s) => s.source === "planning_areas");
  const state = {
    metric: (params.get("metric") === "yoy" ? "yoy" : "psm") as Metric,
    area: byName.has(params.get("area") ?? "") ? params.get("area")! : "",
  };

  // Colour stops at quantiles of the areas' values, so one expensive central area does not
  // wash out the rest; the legend labels each stop.
  const psmValues = areas.features
    .map((f) => f.properties.median_psm_12m)
    .filter((v): v is number => v !== null)
    .sort((a, b) => a - b);
  const psmStops = SEQUENTIAL.map((_, i) => psmValues[Math.round((i * (psmValues.length - 1)) / (SEQUENTIAL.length - 1))]!);
  const yoyLimit = Math.max(0.01, ...areas.features.map((f) => Math.abs(f.properties.yoy_change ?? 0)));

  /** Colour stops [value, colour, ...] for the current metric and theme. */
  function stops(metric: Metric): (number | string)[] {
    if (metric === "yoy") {
      return [-yoyLimit, token("diverging-low"), 0, token("diverging-mid"), yoyLimit, token("diverging-high")];
    }
    return SEQUENTIAL.flatMap((name, i) => [psmStops[i]!, token(name)]);
  }

  function fillColor(metric: Metric): ExpressionSpecification {
    const key = property(metric);
    return [
      "case",
      ["==", ["get", key], null],
      token("no-data"),
      ["interpolate", ["linear"], ["get", key], ...stops(metric)],
    ] as ExpressionSpecification;
  }

  const legendSlot = h("div", { class: "small" });
  function drawLegend(): void {
    const colours = stops(state.metric).filter((_, i) => i % 2 === 1);
    const labels =
      state.metric === "psm"
        ? psmStops.map((v) => fmt.psm(v))
        : [fmt.signedPercent(-yoyLimit), "0%", fmt.signedPercent(yoyLimit)];
    legendSlot.replaceChildren(
      h("p", { class: "muted", style: "margin:0" }, state.metric === "psm" ? "Median price per sqm, latest 12 months (stops at the lowest, quartiles and highest area)" : "Change in median price per sqm on the 12 months before"),
      h("div", { class: "ramp", style: `background: linear-gradient(to right, ${colours.join(", ")})`, "aria-hidden": "true" }),
      h("div", { class: "ramp-labels" }, labels.map((label) => h("span", {}, label))),
      h(
        "p",
        { class: "muted", style: "margin:0.25rem 0 0" },
        h("span", { style: `display:inline-block;width:0.75rem;height:0.75rem;margin-right:0.4rem;vertical-align:-1px;background:${token("no-data")};border:1px solid ${token("border")}` }),
        state.metric === "psm" ? "No HDB resales" : "Fewer than 20 resales in either year",
      ),
    );
  }

  const panel = h("section", { class: "card", "aria-live": "polite", "aria-label": "Selected planning area" });
  let panelChartAbort: AbortController | null = null;

  function showPanel(): void {
    panelChartAbort?.abort();
    const p = byName.get(state.area);
    if (!p) {
      panel.replaceChildren(
        h("h2", { style: "margin-top:0" }, "Planning areas"),
        h("p", { class: "muted" }, "Hover over an area to see its figures, and click it (or choose it from the list) for its trend since 2015."),
      );
      return;
    }
    const chart = h("div", { class: "chart", style: "min-height:10rem" });
    const trend = p.trend.map(([year, sales, psm]) => ({ year, sales, psm }));
    panel.replaceChildren(
      h("button", { type: "button", class: "panel-close", onclick: () => choose(""), "aria-label": `Close ${fmt.title(p.planning_area)}` }, "Close"),
      h("h2", { style: "margin-top:0" }, fmt.title(p.planning_area)),
      h("p", { class: "muted small" }, fmt.title(p.region)),
      table(
        "Median price per sqm and resales, latest 12 months and the 12 before",
        [
          { label: "Period", value: (r: Period) => r.label },
          { label: "Per sqm", value: (r) => (r.psm === null ? "–" : fmt.psm(r.psm)), numeric: true },
          { label: "Sales", value: (r) => fmt.count(r.sales), numeric: true },
        ],
        [
          { label: "Latest 12 months", psm: p.median_psm_12m, sales: p.sales_12m },
          { label: "Year before", psm: p.median_psm_prev_12m, sales: p.sales_prev_12m },
        ],
      ),
      h("p", { class: "small" }, p.yoy_change === null ? "Too few resales to compare the two years." : `Change: ${fmt.signedPercent(p.yoy_change)}.`),
      trend.length
        ? h("h3", {}, "Median price per sqm by year")
        : h("p", { class: "muted small" }, "No years since 2015 with at least 20 resales."),
      chart,
      trend.length
        ? dataTable(
            `Yearly median price per sqm, ${fmt.title(p.planning_area)}`,
            [
              { label: "Year", value: (d: (typeof trend)[number]) => String(d.year) },
              { label: "Median per sqm", value: (d) => fmt.psm(d.psm), numeric: true },
              { label: "Resales", value: (d) => fmt.count(d.sales), numeric: true },
            ],
            trend,
          )
        : "",
    );
    if (!trend.length) return;
    panelChartAbort = new AbortController();
    signal.addEventListener("abort", () => panelChartAbort?.abort(), { once: true });
    responsive(
      chart,
      (width) =>
        Plot.plot({
          ...plotOptions(width, 200, `Line chart of the yearly median price per square metre in ${fmt.title(p.planning_area)}`),
          marginLeft: 52,
          x: { label: null, tickFormat: (v: number) => String(v), ticks: 5 },
          y: { grid: true, label: "S$ per sqm", tickFormat: (v: number) => fmt.count(v) },
          marks: [
            Plot.lineY(trend, { x: "year", y: "psm", stroke: token("series-1"), strokeWidth: 2 }),
            Plot.dot(trend, { x: "year", y: "psm", r: 3, fill: token("series-1") }),
            ...hoverMarks(trend, (d) => d.year, (d) => d.psm, (d) => `${d.year}\n${fmt.psm(d.psm)} per sqm\n${fmt.count(d.sales)} resales`),
          ],
        }),
      panelChartAbort.signal,
    );
  }

  let map: MapLibreMap | null = null;
  const areaSelect = select(
    "Planning area",
    [{ value: "", label: "Choose an area" }, ...names.map((n) => ({ value: n, label: fmt.title(n) }))],
    state.area,
    (v) => choose(v),
  );

  function choose(name: string): void {
    state.area = name;
    (areaSelect.querySelector("select") as HTMLSelectElement).value = name;
    setParams(name ? { metric: state.metric, area: name } : { metric: state.metric });
    map?.setFilter("selected", ["==", ["get", "planning_area"], name]);
    showPanel();
  }

  const mapElement = h("div", { class: "map", role: "region", "aria-label": "Map of planning areas. Use the planning area list to choose an area with the keyboard." });
  const tooltip = h("div", { class: "map-tooltip", hidden: true, "aria-hidden": "true" });
  mapElement.append(tooltip);

  showBody(
    h(
      "div",
      { class: "controls" },
      segmented("Show", METRICS, state.metric, (v) => {
        state.metric = v as Metric;
        setParams(state.area ? { metric: state.metric, area: state.area } : { metric: state.metric });
        map?.setPaintProperty("areas-fill", "fill-color", fillColor(state.metric));
        drawLegend();
      }),
      areaSelect,
    ),
    h("div", { class: "map-layout" }, h("div", {}, mapElement, legendSlot), panel),
    dataTable(
      "All planning areas",
      [
        { label: "Planning area", value: (p: AreaProperties) => fmt.title(p.planning_area) },
        { label: "Median per sqm, latest 12 months", value: (p) => (p.median_psm_12m === null ? "–" : fmt.psm(p.median_psm_12m)), numeric: true },
        { label: "Change", value: (p) => (p.yoy_change === null ? "–" : fmt.signedPercent(p.yoy_change)), numeric: true },
        { label: "Resales", value: (p) => fmt.count(p.sales_12m), numeric: true },
      ],
      names.map((n) => byName.get(n)!),
    ),
  );
  drawLegend();
  showPanel();

  const [maplibre, { default: workerUrl }] = await Promise.all([
    import("maplibre-gl"),
    // MapLibre loads its worker from a separate module; ?url makes Vite publish it.
    import("maplibre-gl/dist/maplibre-gl-worker.mjs?url"),
    import("maplibre-gl/dist/maplibre-gl.css"),
  ]);
  if (signal.aborted) return;
  maplibre.setWorkerUrl(workerUrl);
  try {
    map = new maplibre.Map({
      container: mapElement,
      bounds: SINGAPORE,
      fitBoundsOptions: { padding: 8 },
      minZoom: 9,
      maxZoom: 15,
      maxBounds: [
        [103.3, 1.0],
        [104.4, 1.65],
      ],
      dragRotate: false,
      pitchWithRotate: false,
      attributionControl: {
        compact: true,
        customAttribution: boundarySource
          ? `<a href="${boundarySource.url}">${boundarySource.name}</a>, URA, data.gov.sg`
          : "URA Master Plan 2019 planning areas, data.gov.sg",
      },
      style: {
        version: 8,
        sources: { areas: { type: "geojson", data: areas as unknown as GeoJSONSourceSpecification["data"], promoteId: "planning_area" } },
        layers: [
          { id: "background", type: "background", paint: { "background-color": token("surface") } },
          { id: "areas-fill", type: "fill", source: "areas", paint: { "fill-color": fillColor(state.metric), "fill-opacity": 0.9 } },
          {
            id: "areas-line",
            type: "line",
            source: "areas",
            paint: {
              "line-color": token("surface"),
              "line-width": ["case", ["boolean", ["feature-state", "hover"], false], 2.5, 1],
            },
          },
          {
            id: "selected",
            type: "line",
            source: "areas",
            filter: ["==", ["get", "planning_area"], state.area],
            paint: { "line-color": token("text"), "line-width": 3 },
          },
        ],
      },
    });
  } catch (error) {
    console.error(error);
    mapElement.replaceChildren(notice("The map needs WebGL, which this browser does not provide. Use the planning area list instead.", true));
    return;
  }
  const instance = map;
  instance.addControl(new maplibre.NavigationControl({ showCompass: false }), "top-right");
  instance.getCanvas().setAttribute("aria-label", "Choropleth map of planning areas");
  signal.addEventListener("abort", () => instance.remove(), { once: true });

  let hovered: string | null = null;
  const setHover = (name: string | null) => {
    if (hovered) instance.setFeatureState({ source: "areas", id: hovered }, { hover: false });
    hovered = name;
    if (name) instance.setFeatureState({ source: "areas", id: name }, { hover: true });
  };
  instance.on("mousemove", "areas-fill", (e: MapLayerMouseEvent) => {
    const feature = e.features?.[0];
    if (!feature) return;
    const p = byName.get(String(feature.properties.planning_area));
    if (!p) return;
    setHover(p.planning_area);
    instance.getCanvas().style.cursor = "pointer";
    tooltip.replaceChildren(
      h("strong", {}, fmt.title(p.planning_area)),
      h("br"),
      valueText(p, state.metric),
      h("br"),
      `${fmt.count(p.sales_12m)} resales in the latest 12 months`,
    );
    tooltip.hidden = false;
    const x = Math.min(e.point.x + 12, mapElement.clientWidth - tooltip.offsetWidth - 4);
    const y = Math.min(e.point.y + 12, mapElement.clientHeight - tooltip.offsetHeight - 4);
    tooltip.style.left = `${Math.max(4, x)}px`;
    tooltip.style.top = `${Math.max(4, y)}px`;
  });
  instance.on("mouseleave", "areas-fill", () => {
    setHover(null);
    tooltip.hidden = true;
    instance.getCanvas().style.cursor = "";
  });
  instance.on("click", "areas-fill", (e: MapLayerMouseEvent) => {
    const name = e.features?.[0]?.properties.planning_area;
    if (typeof name === "string") choose(name);
  });

  window.addEventListener(
    "themechange",
    () => {
      instance.setPaintProperty("background", "background-color", token("surface"));
      instance.setPaintProperty("areas-fill", "fill-color", fillColor(state.metric));
      instance.setPaintProperty("areas-line", "line-color", token("surface"));
      instance.setPaintProperty("selected", "line-color", token("text"));
      drawLegend();
      showPanel();
    },
    { signal },
  );
}
