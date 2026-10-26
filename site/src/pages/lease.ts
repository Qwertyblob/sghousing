// Lease decay: how price per sqm falls as the remaining lease shortens, by flat type.

import { hoverMarks, legend, plotOptions, Plot, responsive, token } from "../charts.ts";
import { loadJson, records, type LeaseDecay } from "../data.ts";
import { dataTable, h, segmented, startPage } from "../dom.ts";
import * as fmt from "../format.ts";
import { setParams } from "../route.ts";

export const title = "Lease decay";

interface Row {
  flat_type: string;
  lease_years: number;
  sales: number;
  median_psm: number;
  median_rel_psm: number;
}

const MEASURES = [
  { value: "relative", label: "Relative to town" },
  { value: "psm", label: "Price per sqm" },
];

export async function render(root: HTMLElement, params: URLSearchParams, signal: AbortSignal): Promise<void> {
  const showBody = startPage(root, "Lease decay", "HDB flats are sold on 99-year leases, and flats with less lease left sell for less. Each point is the median of resales with that many whole years of lease remaining.");
  const file = await loadJson<LeaseDecay>("lease_decay.json");
  const data = records<Row>(file);
  const types = ["ALL", ...[...new Set(data.map((r) => r.flat_type))].filter((t) => t !== "ALL").sort()];
  const state = {
    type: types.includes(params.get("type") ?? "") ? params.get("type")! : "ALL",
    measure: params.get("measure") === "psm" ? "psm" : "relative",
  };
  const chart = h("div", { class: "chart" });
  const legendSlot = h("div");
  const tableSlot = h("div");

  const rowsFor = (type: string) => data.filter((r) => r.flat_type === type).sort((a, b) => a.lease_years - b.lease_years);
  const value = (d: Row) => (state.measure === "relative" ? d.median_rel_psm : d.median_psm);
  const show = (v: number) => (state.measure === "relative" ? `${(v * 100).toFixed(0)}% of town median` : `${fmt.psm(v)} per sqm`);

  function draw(width: number): Element {
    const rows = rowsFor(state.type);
    const reference = state.type === "ALL" ? [] : rowsFor("ALL");
    const relative = state.measure === "relative";
    return Plot.plot({
      ...plotOptions(width, width < 500 ? 260 : 340, `Line chart of ${relative ? "price relative to the town median" : "median price per square metre"} by remaining lease, ${fmt.flatType(state.type)}`),
      x: { label: "Remaining lease (years)", labelAnchor: "center", labelArrow: "none" },
      y: relative
        ? { grid: true, label: "Relative to town median", tickFormat: (v: number) => `${Math.round(v * 100)}%` }
        : { grid: true, label: "S$ per sqm", zero: true, tickFormat: (v: number) => fmt.count(v) },
      marginBottom: 40,
      marks: [
        relative ? Plot.ruleY([1], { stroke: token("text-2"), strokeDasharray: "2 3" }) : Plot.ruleY([0], { stroke: token("grid") }),
        Plot.lineY(reference, { x: "lease_years", y: value, stroke: token("reference"), strokeWidth: 1.5, strokeDasharray: "4 3" }),
        Plot.lineY(rows, { x: "lease_years", y: value, stroke: token("series-1"), strokeWidth: 2 }),
        Plot.dot(rows, { x: "lease_years", y: value, r: 2.5, fill: token("series-1") }),
        ...hoverMarks(
          rows,
          (d) => d.lease_years,
          value,
          (d) => `${d.lease_years} years left\n${show(value(d))}\n${fmt.count(d.sales)} resales`,
        ),
      ],
    });
  }

  function describe(): void {
    legendSlot.replaceChildren(
      state.type === "ALL"
        ? ""
        : legend([
            { label: fmt.flatType(state.type), color: token("series-1") },
            { label: "All flat types", color: token("reference"), dashed: true },
          ]),
    );
    tableSlot.replaceChildren(
      dataTable(
        `Median by whole years of remaining lease, ${fmt.flatType(state.type)}`,
        [
          { label: "Remaining lease (years)", value: (d: Row) => String(d.lease_years), numeric: true },
          { label: "Relative to town median", value: (d) => fmt.percent(d.median_rel_psm, 0), numeric: true },
          { label: "Median per sqm", value: (d) => fmt.psm(d.median_psm), numeric: true },
          { label: "Resales", value: (d) => fmt.count(d.sales), numeric: true },
        ],
        rowsFor(state.type),
      ),
    );
  }

  let redraw = () => {};
  const update = () => {
    setParams({ type: state.type, measure: state.measure });
    describe();
    redraw();
  };

  showBody(
    h(
      "div",
      { class: "controls" },
      segmented("Flat type", types.map((t) => ({ value: t, label: t === "ALL" ? "All" : fmt.flatType(t) })), state.type, (v) => {
        state.type = v;
        update();
      }),
      segmented("Measure", MEASURES, state.measure, (v) => {
        state.measure = v;
        update();
      }),
    ),
    legendSlot,
    chart,
    h(
      "p",
      { class: "small muted" },
      `"Relative to town" divides each flat's price per sqm by the median for the same town, flat type and year, which removes most of the effect of location and of the market rising over time: 90% means 10% below a typical flat of that type in the same town. Resales from ${fmt.month(file.from)} to ${fmt.month(file.to)}; years with fewer than ${file.min_sales} resales are left out. Older flats also differ in other ways (layout, size, upgrading), so the curve is not the effect of lease alone.`,
    ),
    tableSlot,
  );
  describe();
  redraw = responsive(chart, draw, signal);
}
