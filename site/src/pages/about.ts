// About: method, sources with credit and licence, model accuracy, limitations, repository.

import { loadMeta, type Source } from "../data.ts";
import { h, startPage, table } from "../dom.ts";
import * as fmt from "../format.ts";

export const title = "About";

const REPO = "https://github.com/qwertyblob/sghousing";

export async function render(root: HTMLElement): Promise<void> {
  const showBody = startPage(root, "About", "This site is built from public data by a pipeline anyone can rerun. It has no server: every chart and every estimate is computed in your browser from files published with the site.");
  const meta = await loadMeta();
  const test = meta.model.test;
  const window = meta.model.data_window;
  showBody(
    h("h2", {}, "Method"),
    h(
      "p",
      {},
      `Resale transactions from ${fmt.month(meta.coverage.from)} to ${fmt.month(meta.coverage.to)} (${fmt.count(meta.coverage.transactions)} resales) are cleaned and stored in PostgreSQL with PostGIS. Each block is located with the ${meta.geocoder.name} and given location features: distance to the nearest MRT exit, primary schools within 1 km, the nearest hawker centre and park, and the distance to the city centre.`,
    ),
    h(
      "p",
      {},
      `Estimates come from three LightGBM models trained on resales from ${window.train_from?.slice(0, 7) ?? "2015"} onwards, predicting the 10th, 50th and 90th percentile of the price. Besides the flat itself (town, flat type and model, floor area, storey, remaining lease), they use recent median prices in the town and block. The intervals are calibrated on past years so that about 80% of actual prices fall inside them. The models run in your browser with ONNX Runtime Web.`,
    ),
    test
      ? h(
          "p",
          {},
          `On ${fmt.count(test.sales)} resales the model never saw (sold after ${fmt.month(window.valid_end ?? "2024-12")}), the typical error was ${fmt.money(test.mae)} (${fmt.percent(test.mape)} of the price), and ${fmt.percent(test.interval_80_coverage)} of prices fell inside the 80% range.`,
        )
      : "",
    h(
      "p",
      {},
      `The estimate is for ${fmt.month(meta.priced_for)}, the month after the latest data. The remaining lease is HDB's figure for the block at its latest resale, counted down to that month (and to today's month after it), which is more precise than counting from the lease start year. The factors shown with an estimate compare the flat with a typical flat of the same type in its town, changing one group of inputs at a time; they are an approximation, not an exact decomposition.`,
    ),
    h("h2", {}, "Limitations"),
    h(
      "ul",
      {},
      h("li", {}, "Estimates are indicative only, not valuations. The model knows nothing about a flat's condition, renovation, facing or view, or its exact floor (only a 3-storey range)."),
      h("li", {}, "Accuracy falls the further the market moves from the training data; the model cannot anticipate a change in market direction."),
      h("li", {}, "Only blocks with a recorded resale since 2015 can be priced, and only their current building."),
      h("li", {}, "There is no supermarket feature: data.gov.sg has no current dataset of supermarket locations."),
      h("li", {}, "Monthly medians from few resales, and the lease decay curve, mix flats that differ in more than the figure shown."),
    ),
    h("h2", {}, "Data sources"),
    h(
      "p",
      {},
      "Contains information from data.gov.sg accessed under the ",
      h("a", { href: meta.licence.url }, meta.licence.name),
      ". Addresses were geocoded with the ",
      h("a", { href: meta.geocoder.url }, meta.geocoder.name),
      `. Data last refreshed ${fmt.day(meta.last_refresh)}.`,
    ),
    table(
      "Datasets used",
      [
        { label: "Dataset", value: (s: Source) => s.name },
        { label: "Updated by publisher", value: (s) => (s.last_updated ? fmt.day(s.last_updated) : "–") },
        { label: "Retrieved", value: (s) => fmt.day(s.retrieved) },
      ],
      meta.sources,
    ),
    h(
      "ul",
      { class: "small link-list" },
      meta.sources.map((s) => h("li", {}, h("a", { href: s.url }, `${s.name} on data.gov.sg`))),
    ),
    h("h2", {}, "Code"),
    h(
      "p",
      {},
      "The pipeline, model and this site are open source: ",
      h("a", { href: REPO }, "github.com/qwertyblob/sghousing"),
      ". The code is MIT licensed.",
    ),
    h("p", { class: "notice" }, meta.disclaimer),
  );
}
