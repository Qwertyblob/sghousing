// Predict: price a flat in the browser. Choose a block (fuzzy search over blocks.json), flat
// type, floor area and storey; the three quantile models give the median estimate and an 80%
// range. Below it: approximate factors and recent sales at the same block or street.

import Fuse from "fuse.js";
import { plotOptions, Plot, responsive, token } from "../charts.ts";
import { loadJson, loadMeta, records, type Meta, type RecentSales } from "../data.ts";
import { h, notice, startPage, table, uid } from "../dom.ts";
import * as fmt from "../format.ts";
import { describe, typicalFlat, variations } from "../predict/factors.ts";
import { blockRows, encode, rawFeatures, storeyMid, type Block, type BlocksFile, type FeatureSpec } from "../predict/features.ts";
import { loadModels, predict, predictMedian, type Models } from "../predict/model.ts";
import { setParams } from "../route.ts";

export const title = "Predict";

interface Sale {
  month: string;
  block: string;
  street_name: string;
  flat_type: string;
  flat_model: string;
  storey_lo: number;
  storey_hi: number;
  floor_area_sqm: number;
  lease_commence_year: number;
  resale_price: number;
}

const MAX_RESULTS = 8;
const MAX_COMPARABLES = 10;
const MAX_FACTORS = 6;

// Models stay loaded for the rest of the visit, so returning to the page is instant.
let modelsPromise: Promise<Models> | null = null;

function recentSales(meta: Meta, town: string): Promise<Sale[]> {
  const file = meta.towns.find((t) => t.town === town)?.file;
  if (!file) return Promise.resolve([]);
  return loadJson<RecentSales>(file).then((f) => records<Sale>(f));
}

function leaseText(months: number): string {
  const years = Math.floor(months / 12);
  const rest = months % 12;
  return rest ? `${years} years ${rest} months` : `${years} years`;
}

export async function render(root: HTMLElement, params: URLSearchParams, signal: AbortSignal): Promise<void> {
  const showBody = startPage(root, "Estimate a resale price", "Choose a block, flat type, floor area and storey. Three models trained on past resales give a median estimate and a range; they run in your browser, and nothing you enter leaves it.");
  const [meta, file, spec] = await Promise.all([
    loadMeta(),
    loadJson<BlocksFile>("blocks.json"),
    loadJson<FeatureSpec>("model/features.json"),
  ]);
  const blocks = blockRows(file);
  const byKey = new Map(blocks.map((b) => [b.address_key, b]));
  const flatTypes = Object.keys(spec.categories.flat_type ?? {}).sort();
  // The search index is built on the first search rather than on page load.
  let fuse: Fuse<Block> | null = null;
  const searchIndex = () =>
    (fuse ??= new Fuse(blocks, {
      keys: [
        { name: "label", weight: 0.7 },
        { name: "address_key", weight: 0.2 },
        { name: "town", weight: 0.1 },
      ],
      threshold: 0.3,
      ignoreLocation: true,
    }));

  // --- Model download: started when the visitor first uses the form, not on page load, so
  // a visit that never prices a flat never downloads the models (about 11 MB with the runtime).
  const progressText = h("p", { class: "small muted", style: "margin:0", "aria-live": "polite" }, "The price models download when you start filling in the form, and then run in your browser.");
  const modelStatus = h("div", { class: "card small" }, progressText);
  function startModels(): Promise<Models> {
    if (!modelsPromise) {
      const progress = h("progress", { class: "progress", max: 1, value: 0, "aria-label": "Price model download" });
      progressText.textContent = "Downloading the price models…";
      modelStatus.append(progress);
      modelsPromise = loadModels(spec, (loaded, total) => {
        // Without a known total the bar is indeterminate rather than wrongly full.
        if (total) {
          progress.max = total;
          progress.value = Math.min(loaded, total);
        } else {
          progress.removeAttribute("value");
        }
        progressText.textContent = `Downloading the price models: ${(loaded / 1e6).toFixed(1)}${total ? ` of ${(total / 1e6).toFixed(1)}` : ""} MB…`;
      });
      modelsPromise.catch(() => (modelsPromise = null));
    }
    modelsPromise.then(
      () => modelStatus.replaceChildren(h("p", { class: "small muted", style: "margin:0" }, "Price models ready. Everything runs in your browser.")),
      (error: unknown) => {
        console.error(error);
        modelStatus.replaceChildren(notice("The price models could not be loaded. Check your connection and reload the page.", true));
      },
    );
    return modelsPromise;
  }

  // --- Form -----------------------------------------------------------------------------
  let chosen: Block | null = null;
  const searchId = uid("block");
  const listId = uid("blocks");
  const search = h("input", {
    id: searchId,
    type: "text",
    role: "combobox",
    autocomplete: "off",
    spellcheck: "false",
    "aria-autocomplete": "list",
    "aria-expanded": "false",
    "aria-controls": listId,
    "aria-describedby": `${searchId}-hint`,
    placeholder: "e.g. 122 Ang Mo Kio Ave 3",
    required: true,
  });
  const list = h("ul", { id: listId, role: "listbox", "aria-label": "Matching blocks", hidden: true });
  const blockInfo = h("p", { class: "hint", id: `${searchId}-hint` }, `Type a block number and street. ${fmt.count(blocks.length)} blocks with a resale since 2015.`);
  let matches: Block[] = [];
  let active = -1;

  function closeList(): void {
    list.hidden = true;
    search.setAttribute("aria-expanded", "false");
    search.removeAttribute("aria-activedescendant");
    active = -1;
  }

  function highlight(index: number): void {
    active = index;
    for (const [i, option] of [...list.children].entries()) option.setAttribute("aria-selected", String(i === index));
    const option = list.children[index] as HTMLElement | undefined;
    if (option) {
      search.setAttribute("aria-activedescendant", option.id);
      option.scrollIntoView({ block: "nearest" });
    }
  }

  function showMatches(): void {
    const query = search.value.trim();
    matches = query.length < 2 ? [] : searchIndex().search(query, { limit: MAX_RESULTS }).map((r) => r.item);
    list.replaceChildren(
      ...matches.map((b, i) =>
        h(
          "li",
          {
            id: `${listId}-${i}`,
            role: "option",
            "aria-selected": "false",
            onmousedown: (e: Event) => e.preventDefault(),
            onclick: () => pick(b),
          },
          b.label,
          " ",
          h("span", { class: "muted" }, fmt.title(b.town)),
        ),
      ),
    );
    list.hidden = matches.length === 0;
    search.setAttribute("aria-expanded", String(matches.length > 0));
    active = -1;
    search.removeAttribute("aria-activedescendant");
  }

  search.addEventListener("input", () => {
    chosen = null;
    showMatches();
  });
  search.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" && matches.length) {
      e.preventDefault();
      if (list.hidden) showMatches();
      highlight((active + 1) % matches.length);
    } else if (e.key === "ArrowUp" && matches.length) {
      e.preventDefault();
      highlight(active <= 0 ? matches.length - 1 : active - 1);
    } else if (e.key === "Enter" && !list.hidden && active >= 0) {
      e.preventDefault();
      pick(matches[active]!);
    } else if (e.key === "Escape") {
      closeList();
    }
  });
  search.addEventListener("blur", () => closeList());

  const typeId = uid("type");
  const typeSelect = h("select", { id: typeId, required: true });
  const areaId = uid("area");
  const area = h("input", { id: areaId, type: "number", min: 20, max: 300, step: "any", inputmode: "decimal", required: true, "aria-describedby": `${areaId}-hint` });
  const areaHint = h("p", { class: "hint", id: `${areaId}-hint` }, "Floor area in square metres.");
  const storeyId = uid("storey");
  const storey = h("input", { id: storeyId, type: "number", min: 1, max: 50, step: 1, inputmode: "numeric", required: true, "aria-describedby": `${storeyId}-hint` });
  const storeyHint = h("p", { class: "hint", id: `${storeyId}-hint` }, "The storey the flat is on.");
  const submit = h("button", { type: "submit", class: "primary" }, "Estimate price");
  const formError = h("p", { class: "small", role: "alert" });
  let areaEdited = false;
  area.addEventListener("input", () => (areaEdited = true));

  async function typicalArea(block: Block, flatType: string): Promise<{ value: number; where: string } | null> {
    const sales = (await recentSales(meta, block.town)).filter((s) => s.flat_type === flatType);
    const here = sales.filter((s) => s.block === block.block && s.street_name === block.street_name);
    const pool = here.length ? here : sales;
    if (!pool.length) return null;
    const sorted = pool.map((s) => s.floor_area_sqm).sort((a, b) => a - b);
    return { value: sorted[sorted.length >> 1]!, where: here.length ? "at this block" : `in ${fmt.title(block.town)}` };
  }

  // Each lookup may wait for a town's sales file; only the latest one may update the form,
  // so a slow answer for an earlier block or flat type never overwrites the current one.
  let areaRequest = 0;
  async function updateArea(): Promise<void> {
    if (!chosen) return;
    const request = ++areaRequest;
    const flatType = typeSelect.value;
    const typical = await typicalArea(chosen, flatType);
    if (request !== areaRequest || signal.aborted) return;
    if (!typical) {
      areaHint.textContent = "Floor area in square metres.";
      return;
    }
    areaHint.textContent = `Typical ${fmt.flatType(flatType).toLowerCase()} flat ${typical.where}: ${typical.value} sqm.`;
    if (!areaEdited) area.value = String(typical.value);
  }

  function pick(block: Block, flatType?: string): void {
    chosen = block;
    search.value = block.label;
    closeList();
    const sold = Object.keys(block.flat_models).sort();
    const others = flatTypes.filter((t) => !sold.includes(t));
    const previous = flatType ?? typeSelect.value;
    typeSelect.replaceChildren(
      h("optgroup", { label: "Sold at this block" }, sold.map((t) => h("option", { value: t }, fmt.flatType(t)))),
      others.length ? h("optgroup", { label: "Not sold here since 2015" }, others.map((t) => h("option", { value: t }, fmt.flatType(t)))) : "",
    );
    typeSelect.value = sold.includes(previous) || others.includes(previous) ? previous : sold.includes("4 ROOM") ? "4 ROOM" : sold[0]!;
    storey.max = String(block.block_max_floor);
    storeyHint.textContent = `This block has ${block.block_max_floor} storeys.`;
    blockInfo.textContent = `${fmt.title(block.town)} · lease from ${block.lease_commence_year}, ${leaseText(block.remaining_lease_months)} left · ${Math.round(block.dist_mrt_m)} m to ${fmt.title(block.nearest_mrt_name)}`;
    void updateArea();
  }
  typeSelect.addEventListener("change", () => void updateArea());

  const result = h("section", { "aria-live": "polite", "aria-label": "Estimate" });
  const form = h(
    "form",
    { class: "card predict-form", novalidate: true },
    h("div", { class: "field" }, h("label", { for: searchId }, "Block and street"), h("div", { class: "combobox" }, search, list), blockInfo),
    h("div", { class: "field" }, h("label", { for: typeId }, "Flat type"), typeSelect),
    h("div", { class: "field" }, h("label", { for: areaId }, "Floor area (sqm)"), area, areaHint),
    h("div", { class: "field" }, h("label", { for: storeyId }, "Storey"), storey, storeyHint),
    submit,
    formError,
  );

  form.addEventListener("focusin", () => void startModels(), { once: true });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    formError.textContent = "";
    if (!chosen) {
      formError.textContent = "Choose a block from the list.";
      search.focus();
      return;
    }
    const floorArea = Number(area.value);
    const level = Number(storey.value);
    if (!area.value || !Number.isFinite(floorArea) || floorArea < 20 || floorArea > 300) {
      formError.textContent = "Enter a floor area between 20 and 300 square metres.";
      area.focus();
      return;
    }
    if (!Number.isInteger(level) || level < 1 || level > chosen.block_max_floor) {
      formError.textContent = `Enter a storey from 1 to ${chosen.block_max_floor}.`;
      storey.focus();
      return;
    }
    setParams({ block: chosen.address_key, type: typeSelect.value, area: String(floorArea), storey: String(level) });
    void estimate(chosen, typeSelect.value, floorArea, level);
  });

  // --- Estimate -------------------------------------------------------------------------
  let factorChartAbort: AbortController | null = null;
  async function estimate(block: Block, flatType: string, floorArea: number, level: number): Promise<void> {
    submit.disabled = true;
    result.replaceChildren(h("p", { class: "muted" }, "Estimating…"));
    try {
      const loaded = await startModels();
      const flat = { flatType, floorArea, storey: level };
      const raw = rawFeatures(block, flat, file, new Date());
      const [sales, quantiles] = await Promise.all([recentSales(meta, block.town), predict(loaded, [encode(raw, spec)])]);
      if (signal.aborted) return;
      const q = quantiles[0]!;
      const typical = typicalFlat(block.town, flatType, blocks.filter((b) => b.town === block.town), sales, file);
      const varied = variations(raw, typical);
      const medians = await predictMedian(loaded, [encode(raw, spec), ...varied.map((v) => encode(v.raw, spec))]);
      const base = medians[0]!;
      const factors = varied
        .map((v, i) => ({ label: v.group.label, detail: describe(v.group, raw, typical), value: base - medians[i + 1]! }))
        .filter((f) => Math.abs(f.value) >= 500)
        .sort((a, b) => Math.abs(b.value) - Math.abs(a.value))
        .slice(0, MAX_FACTORS);
      showResult(block, flat, raw.remaining_lease_months as number, q, factors, sales);
    } catch (error) {
      console.error(error);
      result.replaceChildren(notice("The estimate failed. Reload the page and try again.", true));
    } finally {
      submit.disabled = false;
    }
  }

  function showResult(
    block: Block,
    flat: { flatType: string; floorArea: number; storey: number },
    remaining: number,
    q: { p10: number; p50: number; p90: number },
    factors: { label: string; detail: string; value: number }[],
    sales: Sale[],
  ): void {
    factorChartAbort?.abort();
    const mid = storeyMid(flat.storey);
    const sameBlock = (s: Sale) => s.block === block.block && s.street_name === block.street_name;
    const comparables = sales
      .filter((s) => s.street_name === block.street_name)
      .sort((a, b) => Number(sameBlock(b)) - Number(sameBlock(a)) || Number(b.flat_type === flat.flatType) - Number(a.flat_type === flat.flatType) || b.month.localeCompare(a.month))
      .slice(0, MAX_COMPARABLES);
    const chart = h("div", { class: "chart", style: "min-height:8rem" });

    result.replaceChildren(
      h(
        "div",
        { class: "card" },
        h("h2", { style: "margin-top:0" }, "Estimated price"),
        h("p", { class: "estimate" }, fmt.money(q.p50)),
        h("p", { class: "range" }, `Likely range ${fmt.money(q.p10)} to ${fmt.money(q.p90)}`),
        h("p", { class: "notice" }, h("strong", {}, "Indicative only, not a valuation. "), "A statistical estimate from past resales; it knows nothing about the flat's condition, renovation or view. About 8 in 10 past sales fell inside the range."),
        h(
          "p",
          { class: "small muted" },
          `${fmt.flatType(flat.flatType)}, ${flat.floorArea} sqm, storeys ${mid - 1} to ${mid + 1}, at ${block.label}, ${fmt.title(block.town)}. ${leaseText(remaining)} of lease left. Priced for ${fmt.month(file.as_of)} with data up to ${fmt.month(meta.coverage.to)}.`,
        ),
      ),
      h("h3", {}, "What moves this estimate"),
      factors.length
        ? h("div", {}, chart, table(
            `Change in the estimate from each factor, against a typical ${fmt.flatType(flat.flatType).toLowerCase()} flat in ${fmt.title(block.town)}`,
            [
              { label: "Factor", value: (f: (typeof factors)[number]) => f.label },
              { label: "This flat", value: (f) => f.detail },
              { label: "Effect", value: (f) => fmt.signedMoney(f.value), numeric: true },
            ],
            factors,
          ))
        : h("p", { class: "muted" }, "This flat is close to a typical one of its type in the town."),
      h(
        "p",
        { class: "small muted" },
        `Each factor is the change in the median estimate when that group of inputs is replaced by a typical ${fmt.flatType(flat.flatType).toLowerCase()} flat's in ${fmt.title(block.town)}. An approximation: factors overlap and do not add up to the price.`,
      ),
      h("h3", {}, "Recent sales nearby"),
      comparables.length
        ? table(
            `Up to ${MAX_COMPARABLES} sales since ${fmt.month(sales.length ? sales[sales.length - 1]!.month : file.as_of)} at ${block.label}, then elsewhere on ${fmt.title(block.street_name)}`,
            [
              { label: "Month", value: (s: Sale) => fmt.month(s.month) },
              { label: "Block", value: (s) => `${s.block}${sameBlock(s) ? " (this block)" : ""}` },
              { label: "Flat type", value: (s) => fmt.flatType(s.flat_type) },
              { label: "Storeys", value: (s) => `${s.storey_lo}–${s.storey_hi}` },
              { label: "Area (sqm)", value: (s) => String(s.floor_area_sqm), numeric: true },
              { label: "Price", value: (s) => fmt.money(s.resale_price), numeric: true },
            ],
            comparables,
          )
        : h("p", { class: "muted" }, `No resales on ${fmt.title(block.street_name)} in the last two years.`),
    );

    if (!factors.length) return;
    factorChartAbort = new AbortController();
    signal.addEventListener("abort", () => factorChartAbort?.abort(), { once: true });
    responsive(
      chart,
      (width) =>
        Plot.plot({
          ...plotOptions(width, 40 + factors.length * 34, "Bar chart of how each factor raises or lowers the estimate"),
          marginLeft: Math.min(200, width * 0.4),
          marginRight: 64,
          insetLeft: 56,
          // On a phone the bars' own labels carry the values; an axis would only collide.
          x: width < 500 ? { axis: null } : { label: null, grid: true, tickFormat: (v: number) => fmt.signedMoney(v), ticks: 5 },
          y: { label: null, domain: factors.map((f) => f.label) },
          marks: [
            Plot.barX(factors, { x: "value", y: "label", fill: (f: (typeof factors)[number]) => token(f.value >= 0 ? "diverging-low" : "diverging-high"), insetTop: 4, insetBottom: 4 }),
            Plot.ruleX([0], { stroke: token("text-2") }),
            // Labels sit just past the end of each bar, on its own side of zero.
            Plot.text(factors.filter((f) => f.value >= 0), { x: "value", y: "label", text: (f: (typeof factors)[number]) => fmt.signedMoney(f.value), dx: 4, textAnchor: "start", fill: token("text") }),
            Plot.text(factors.filter((f) => f.value < 0), { x: "value", y: "label", text: (f: (typeof factors)[number]) => fmt.signedMoney(f.value), dx: -4, textAnchor: "end", fill: token("text") }),
          ],
        }),
      factorChartAbort.signal,
    );
  }

  showBody(
    h("div", { class: "predict-layout" }, h("div", {}, form, modelStatus), result),
  );

  // A shared link (#/predict?block=...&type=...&area=...&storey=...) prices that flat at once.
  const shared = byKey.get(params.get("block") ?? "");
  if (shared) {
    pick(shared, params.get("type") ?? undefined);
    const sharedArea = Number(params.get("area"));
    const sharedStorey = Number(params.get("storey"));
    if (sharedArea) {
      area.value = String(sharedArea);
      areaEdited = true;
    }
    if (sharedStorey) storey.value = String(sharedStorey);
    if (sharedArea && sharedStorey) form.requestSubmit();
  }
}
