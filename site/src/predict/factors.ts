// Approximate contributions to an estimate: how much the median estimate changes when one
// group of inputs is set to that of a typical flat of the same type in the same town. Not
// SHAP: groups interact, so the parts do not add up to the difference from the typical flat.

import { roundHalfUp, storeyMid, type Block, type BlocksFile, type RawFeatures } from "./features.ts";

export interface Group {
  label: string;
  fields: string[];
}

export const GROUPS: Group[] = [
  { label: "Floor area", fields: ["floor_area_sqm"] },
  { label: "Storey", fields: ["storey_mid", "rel_storey"] },
  { label: "Remaining lease", fields: ["remaining_lease_months", "flat_age_years"] },
  { label: "Block height and age", fields: ["block_max_floor", "block_year_completed"] },
  { label: "Flat model", fields: ["flat_model"] },
  { label: "MRT access", fields: ["dist_mrt_m", "n_mrt_exits_500m"] },
  { label: "Primary schools", fields: ["n_primary_schools_1km", "dist_nearest_primary_m"] },
  { label: "Hawker centre and park", fields: ["dist_hawker_m", "dist_park_m"] },
  { label: "Position in the town", fields: ["lat", "lon", "dist_cbd_m", "planning_area"] },
  { label: "Recent prices at the block", fields: ["block_psm_24m", "block_sales_24m"] },
];

const NUMERIC_BLOCK_FIELDS = [
  "lat",
  "lon",
  "dist_mrt_m",
  "n_mrt_exits_500m",
  "n_primary_schools_1km",
  "dist_nearest_primary_m",
  "dist_hawker_m",
  "dist_park_m",
  "dist_cbd_m",
  "block_max_floor",
  "block_year_completed",
  "remaining_lease_months",
  "block_psm_24m",
  "block_sales_24m",
] as const;

export function median(values: number[]): number | null {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = sorted.length >> 1;
  return sorted.length % 2 ? sorted[mid]! : (sorted[mid - 1]! + sorted[mid]!) / 2;
}

function mostCommon(values: string[]): string | null {
  const counts = new Map<string, number>();
  for (const v of values) counts.set(v, (counts.get(v) ?? 0) + 1);
  let best: string | null = null;
  for (const [value, n] of counts) if (best === null || n > counts.get(best)!) best = value;
  return best;
}

/** Inputs of a typical flat of `flatType` in the block's town: block medians over the town's
 * blocks, floor area and storey from its recent sales of that type. */
export function typicalFlat(
  town: string,
  flatType: string,
  townBlocks: Block[],
  recent: { floor_area_sqm: number; storey_lo: number; storey_hi: number; flat_type: string }[],
  file: BlocksFile,
): RawFeatures {
  const typical: RawFeatures = {};
  for (const field of NUMERIC_BLOCK_FIELDS) {
    typical[field] = median(townBlocks.map((b) => b[field]).filter((v): v is number => v !== null));
  }
  const remaining = typical.remaining_lease_months as number | null;
  typical.flat_age_years = remaining === null ? null : roundHalfUp(Math.max(0, 99 * 12 - Math.round(remaining)), 12, 2);
  typical.planning_area = mostCommon(townBlocks.map((b) => b.planning_area));
  typical.flat_model = file.town_flat_model[town]?.[flatType] ?? null;
  const sales = recent.filter((s) => s.flat_type === flatType);
  typical.floor_area_sqm = median(sales.map((s) => s.floor_area_sqm));
  const storey = median(sales.map((s) => storeyMid(s.storey_lo)));
  typical.storey_mid = storey === null ? null : Math.round(storey);
  const maxFloor = typical.block_max_floor as number | null;
  typical.rel_storey =
    storey === null || maxFloor === null ? null : roundHalfUp(Math.min(Math.round(storey), Math.round(maxFloor)), Math.round(maxFloor), 3);
  return typical;
}

/** `raw` with each group in turn replaced by the typical flat's values. */
export function variations(raw: RawFeatures, typical: RawFeatures): { group: Group; raw: RawFeatures }[] {
  return GROUPS.filter((g) => g.fields.some((f) => typical[f] !== undefined && typical[f] !== null)).map((group) => {
    const changed: RawFeatures = { ...raw };
    for (const field of group.fields) changed[field] = typical[field] ?? raw[field] ?? null;
    return { group, raw: changed };
  });
}

/** One group's text: this flat's value against the typical one. */
export function describe(group: Group, raw: RawFeatures, typical: RawFeatures): string {
  const n = (field: string) => raw[field] as number | null;
  const t = (field: string) => typical[field] as number | null;
  const m = (v: number | null) => (v === null ? "–" : `${Math.round(v).toLocaleString("en-SG")} m`);
  const years = (v: number | null) => (v === null ? "–" : `${Math.floor(v / 12)} years`);
  switch (group.label) {
    case "Floor area":
      return `${n("floor_area_sqm")} sqm (typical ${t("floor_area_sqm")?.toFixed(0) ?? "–"} sqm)`;
    case "Storey":
      return `storeys around ${n("storey_mid")} (typical ${t("storey_mid") ?? "–"})`;
    case "Remaining lease":
      return `${years(n("remaining_lease_months"))} left (typical ${years(t("remaining_lease_months"))})`;
    case "Block height and age":
      return `${n("block_max_floor")} storeys, built ${n("block_year_completed")}`;
    case "Flat model":
      return `${raw.flat_model ?? "unknown"} (most common: ${typical.flat_model ?? "–"})`;
    case "MRT access":
      return `${m(n("dist_mrt_m"))} to an MRT exit (typical ${m(t("dist_mrt_m"))})`;
    case "Primary schools":
      return `${n("n_primary_schools_1km")} within 1 km (typical ${t("n_primary_schools_1km") ?? "–"})`;
    case "Hawker centre and park":
      return `hawker centre ${m(n("dist_hawker_m"))}, park ${m(n("dist_park_m"))}`;
    case "Position in the town":
      return `${m(n("dist_cbd_m"))} from the city centre (typical ${m(t("dist_cbd_m"))})`;
    case "Recent prices at the block":
      return n("block_psm_24m") === null
        ? "too few recent sales at the block"
        : `S$${Math.round(n("block_psm_24m")!).toLocaleString("en-SG")} per sqm (town blocks ${t("block_psm_24m") === null ? "–" : `S$${Math.round(t("block_psm_24m")!).toLocaleString("en-SG")}`})`;
    default:
      return "";
  }
}
