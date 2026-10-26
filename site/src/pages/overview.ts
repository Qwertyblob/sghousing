// Overview: headline figures for the latest 12 months, the national trend, and freshness.

import { hoverMarks, plotOptions, Plot, responsive, token } from "../charts.ts";
import { loadJson, loadMeta, parseMonth, records, type Overview } from "../data.ts";
import { dataTable, h, startPage, table } from "../dom.ts";
import * as fmt from "../format.ts";

export const title = "Overview";

interface FlatTypeRow {
  flat_type: string;
  sales_12m: number;
  median_price_12m: number | null;
  median_psm_12m: number | null;
  sales_prev_12m: number;
  median_price_prev_12m: number | null;
  median_psm_prev_12m: number | null;
}

interface NationalRow {
  month: string;
  sales: number;
  median_psm: number;
  median_price: number;
}

function change(latest: number | null, previous: number | null): string {
  if (latest === null || previous === null || previous === 0) return "no earlier figure";
  return `${fmt.signedPercent(latest / previous - 1)} on the 12 months before`;
}

function tile(label: string, value: string, detail: string): HTMLElement {
  return h(
    "div",
    { class: "card tile" },
    h("p", { class: "label" }, label),
    h("p", { class: "value" }, value),
    h("p", { class: "detail" }, detail),
  );
}

export async function render(root: HTMLElement, _params: URLSearchParams, signal: AbortSignal): Promise<void> {
  const showBody = startPage(root, "HDB resale prices", "Prices of resale flats in Singapore from official HDB records: how they have moved, where they differ, how lease length matters, and an estimate for a flat you choose.");
  const [meta, overview] = await Promise.all([loadMeta(), loadJson<Overview>("overview.json")]);
  const types = records<FlatTypeRow>(overview.by_flat_type);
  const all = types.find((r) => r.flat_type === "ALL");
  const national = records<NationalRow>(overview.national);
  const period = `${fmt.month(overview.from)} to ${fmt.month(overview.to)}`;

  const tiles = h("div", { class: "tiles" });
  if (all) {
    tiles.append(
      tile(
        "Median resale price",
        all.median_price_12m === null ? "–" : fmt.money(all.median_price_12m),
        change(all.median_price_12m, all.median_price_prev_12m),
      ),
      tile(
        "Median price per sqm",
        all.median_psm_12m === null ? "–" : fmt.psm(all.median_psm_12m),
        change(all.median_psm_12m, all.median_psm_prev_12m),
      ),
      tile("Resales", fmt.count(all.sales_12m), `${fmt.count(all.sales_prev_12m)} in the 12 months before`),
    );
  }
  tiles.append(
    tile(
      "Data up to",
      fmt.month(meta.coverage.to),
      `Refreshed ${fmt.day(meta.last_refresh)} · ${fmt.count(meta.coverage.transactions)} resales since ${fmt.month(meta.coverage.from)}`,
    ),
  );

  const chart = h("div", { class: "chart" });
  const byType = types.filter((r) => r.flat_type !== "ALL" && r.sales_12m > 0);

  showBody(
    h("p", { class: "muted small" }, `Figures for the latest 12 months of data, ${period}.`),
    tiles,
    h("h2", {}, "Median price per square metre, all of Singapore"),
    h("p", { class: "muted small" }, `Monthly median across all flat types since ${fmt.month(overview.national.from)}.`),
    chart,
    dataTable(
      "Monthly median price per square metre, all flat types",
      [
        { label: "Month", value: (d: NationalRow) => fmt.month(d.month) },
        { label: "Median per sqm", value: (d) => fmt.psm(d.median_psm), numeric: true },
        { label: "Median price", value: (d) => fmt.money(d.median_price), numeric: true },
        { label: "Resales", value: (d) => fmt.count(d.sales), numeric: true },
      ],
      [...national].reverse(),
    ),
    h("h2", {}, "By flat type"),
    table(
      `Latest 12 months (${period}) compared with the 12 months before`,
      [
        { label: "Flat type", value: (d: FlatTypeRow) => fmt.flatType(d.flat_type) },
        {
          label: "Median price",
          value: (d) => (d.median_price_12m === null ? "–" : fmt.money(d.median_price_12m)),
          numeric: true,
        },
        {
          label: "Change",
          value: (d) =>
            d.median_price_12m === null || !d.median_price_prev_12m
              ? "–"
              : fmt.signedPercent(d.median_price_12m / d.median_price_prev_12m - 1),
          numeric: true,
        },
        {
          label: "Median per sqm",
          value: (d) => (d.median_psm_12m === null ? "–" : fmt.psm(d.median_psm_12m)),
          numeric: true,
        },
        { label: "Resales", value: (d) => fmt.count(d.sales_12m), numeric: true },
      ],
      byType,
    ),
    h(
      "p",
      { class: "small" },
      "Explore ",
      h("a", { href: "#/trends" }, "trends by town"),
      ", the ",
      h("a", { href: "#/map" }, "map of planning areas"),
      ", or ",
      h("a", { href: "#/predict" }, "estimate a flat's price"),
      ".",
    ),
  );

  const x = (d: NationalRow) => parseMonth(d.month);
  const y = (d: NationalRow) => d.median_psm;
  responsive(
    chart,
    (width) =>
      Plot.plot({
        ...plotOptions(width, width < 500 ? 240 : 320, "Line chart of the national monthly median price per square metre"),
        y: { grid: true, label: "S$ per sqm", zero: true, tickFormat: (v: number) => fmt.count(v) },
        x: { label: null },
        marks: [
          Plot.ruleY([0], { stroke: token("grid") }),
          Plot.lineY(national, { x, y, stroke: token("series-1"), strokeWidth: 2 }),
          ...hoverMarks(
            national,
            x,
            y,
            (d) => `${fmt.month(d.month)}\n${fmt.psm(d.median_psm)} per sqm\n${fmt.count(d.sales)} resales`,
          ),
        ],
      }),
    signal,
  );
}
