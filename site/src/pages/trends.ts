// Trends: monthly median price per sqm and sales volume for a town and flat type.

import { hoverMarks, legend, plotOptions, Plot, responsive, token } from "../charts.ts";
import { loadJson, parseMonth, records, type TownMonth } from "../data.ts";
import { dataTable, h, notice, segmented, select, startPage } from "../dom.ts";
import * as fmt from "../format.ts";
import { setParams } from "../route.ts";

export const title = "Trends";

interface Row {
  town: string;
  flat_type: string;
  month: string;
  sales: number;
  median_psm: number;
  median_price: number;
}

const PERIODS = [
  { value: "2000", label: "Since 2000" },
  { value: "10", label: "10 years" },
  { value: "5", label: "5 years" },
];
// Monthly medians from fewer sales than this are noisy; the page says so.
const FEW_SALES = 10;

const placeName = (town: string) => (town === "ALL" ? "All Singapore" : fmt.title(town));

export async function render(root: HTMLElement, params: URLSearchParams, signal: AbortSignal): Promise<void> {
  const showBody = startPage(root, "Trends", "Monthly median resale price per square metre and the number of resales, for a town and flat type.");
  const data = records<Row>(await loadJson<TownMonth>("town_month.json"));
  const series = new Map<string, Row[]>();
  for (const row of data) {
    const key = `${row.town}|${row.flat_type}`;
    let rows = series.get(key);
    if (!rows) series.set(key, (rows = []));
    rows.push(row);
  }
  const towns = [...new Set(data.map((r) => r.town))].filter((t) => t !== "ALL").sort();
  const types = [...new Set(data.map((r) => r.flat_type))].filter((t) => t !== "ALL").sort();
  const last = data.reduce((m, r) => (r.month > m ? r.month : m), "");

  const state = {
    town: towns.includes(params.get("town") ?? "") ? params.get("town")! : "ALL",
    type: types.includes(params.get("type") ?? "") ? params.get("type")! : "ALL",
    period: PERIODS.some((p) => p.value === params.get("period")) ? params.get("period")! : "2000",
  };

  const priceChart = h("div", { class: "chart" });
  const volumeChart = h("div", { class: "chart" });
  const priceLegend = h("div");
  const summary = h("p", { class: "small", "aria-live": "polite" });
  const tableSlot = h("div");

  /** First month shown: January 2000, or the last N years of data. */
  function start(): string {
    if (state.period === "2000") return "2000-01";
    const [year, month] = last.split("-").map(Number) as [number, number];
    const first = new Date(Date.UTC(year - Number(state.period), month, 1));
    return `${first.getUTCFullYear()}-${String(first.getUTCMonth() + 1).padStart(2, "0")}`;
  }

  function selection(town: string): Row[] {
    const from = start();
    return (series.get(`${town}|${state.type}`) ?? []).filter((r) => r.month >= from);
  }

  const x = (d: Row) => parseMonth(d.month);

  function drawPrice(width: number): Element {
    const rows = selection(state.town);
    const reference = state.town === "ALL" ? [] : selection("ALL");
    const label = (rows: Row[], text: string, color: string) =>
      rows.length && width >= 560
        ? [Plot.text([rows[rows.length - 1]!], { x, y: "median_psm", text: () => text, dx: 6, textAnchor: "start", fill: color })]
        : [];
    return Plot.plot({
      ...plotOptions(width, width < 500 ? 260 : 340, `Line chart of median price per square metre, ${placeName(state.town)}, ${fmt.flatType(state.type)}`),
      marginRight: width >= 560 ? 104 : 16,
      y: { grid: true, label: "S$ per sqm", zero: true, tickFormat: (v: number) => fmt.count(v) },
      x: { label: null },
      marks: [
        Plot.ruleY([0], { stroke: token("grid") }),
        Plot.lineY(reference, { x, y: "median_psm", stroke: token("reference"), strokeWidth: 1.5, strokeDasharray: "4 3" }),
        Plot.lineY(rows, { x, y: "median_psm", stroke: token("series-1"), strokeWidth: 2 }),
        ...label(reference, "All Singapore", token("text-2")),
        ...label(rows, placeName(state.town), token("text")),
        ...hoverMarks(
          rows,
          x,
          (d) => d.median_psm,
          (d) => `${fmt.month(d.month)}\n${fmt.psm(d.median_psm)} per sqm\nMedian price ${fmt.money(d.median_price)}\n${fmt.count(d.sales)} resales`,
        ),
      ],
    });
  }

  function drawVolume(width: number): Element {
    const rows = selection(state.town);
    return Plot.plot({
      ...plotOptions(width, 180, `Bar chart of monthly resales, ${placeName(state.town)}, ${fmt.flatType(state.type)}`),
      y: { grid: true, label: "Resales", tickFormat: (v: number) => fmt.count(v) },
      x: { label: null },
      marks: [
        Plot.rectY(rows, { x, y: "sales", interval: "month", fill: token("series-1"), insetLeft: 0.5, insetRight: 0.5 }),
        Plot.ruleY([0], { stroke: token("grid") }),
        Plot.tip(rows, Plot.pointerX({ x, y: "sales", title: (d: Row) => `${fmt.month(d.month)}\n${fmt.count(d.sales)} resales`, fill: token("surface"), stroke: token("border") })),
      ],
    });
  }

  function describe(): void {
    const rows = selection(state.town);
    const latest = rows[rows.length - 1];
    priceLegend.replaceChildren(
      state.town === "ALL"
        ? ""
        : legend([
            { label: placeName(state.town), color: token("series-1") },
            { label: "All Singapore", color: token("reference"), dashed: true },
          ]),
    );
    if (!latest) {
      summary.replaceChildren(notice(`No resales of ${fmt.flatType(state.type).toLowerCase()} flats in ${placeName(state.town)} in this period.`));
      tableSlot.replaceChildren();
      return;
    }
    const sparse = rows.filter((r) => r.sales < FEW_SALES).length;
    summary.replaceChildren(
      `${placeName(state.town)}, ${fmt.flatType(state.type).toLowerCase()}: in ${fmt.month(latest.month)} the median was ${fmt.psm(latest.median_psm)} per sqm (${fmt.money(latest.median_price)}) from ${fmt.count(latest.sales)} resales.`,
      sparse ? ` ${sparse} of ${rows.length} months had fewer than ${FEW_SALES} resales, so their medians are noisy.` : "",
    );
    tableSlot.replaceChildren(
      dataTable(
        `Monthly medians, ${placeName(state.town)}, ${fmt.flatType(state.type)}`,
        [
          { label: "Month", value: (d: Row) => fmt.month(d.month) },
          { label: "Median per sqm", value: (d) => fmt.psm(d.median_psm), numeric: true },
          { label: "Median price", value: (d) => fmt.money(d.median_price), numeric: true },
          { label: "Resales", value: (d) => fmt.count(d.sales), numeric: true },
        ],
        [...rows].reverse(),
      ),
    );
  }

  let redraw: (() => void)[] = [];
  function update(): void {
    setParams({ town: state.town, type: state.type, period: state.period });
    describe();
    for (const draw of redraw) draw();
  }

  showBody(
    h(
      "div",
      { class: "controls" },
      select("Town", [{ value: "ALL", label: "All Singapore" }, ...towns.map((t) => ({ value: t, label: fmt.title(t) }))], state.town, (v) => {
        state.town = v;
        update();
      }),
      select("Flat type", [{ value: "ALL", label: "All flat types" }, ...types.map((t) => ({ value: t, label: fmt.flatType(t) }))], state.type, (v) => {
        state.type = v;
        update();
      }),
      segmented("Period", PERIODS, state.period, (v) => {
        state.period = v;
        update();
      }),
    ),
    h("h2", {}, "Median price per square metre"),
    priceLegend,
    priceChart,
    summary,
    h("h2", {}, "Resales per month"),
    volumeChart,
    tableSlot,
  );
  describe();
  redraw = [responsive(priceChart, drawPrice, signal), responsive(volumeChart, drawVolume, signal)];
}
