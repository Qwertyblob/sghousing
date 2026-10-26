// The model input for one flat, built from blocks.json exactly as the Python CLI builds it
// from the database (pipeline/train/predict.py build_features + encode_one), in the order
// and encoding model/features.json defines. Pure: no DOM, no imports, so Node can run it in
// the parity test (tests/test_site_features.py).

export interface FeatureSpec {
  model_version: string;
  input: { name: string; dtype: "float32"; missing: "NaN" };
  features: { name: string; kind: "categorical" | "numeric" }[];
  categories: Record<string, Record<string, number>>;
  outputs: Record<string, string>;
}

export interface BlocksFile {
  as_of: string;
  town_rolling_columns: string[];
  town_rolling: Record<string, Record<string, (number | null)[]>>;
  town_flat_model: Record<string, Record<string, string>>;
  columns: string[];
  rows: unknown[][];
}

export interface Block {
  address_key: string;
  block: string;
  street_name: string;
  label: string;
  town: string;
  planning_area: string;
  region: string;
  lat: number;
  lon: number;
  lease_commence_year: number;
  remaining_lease_months: number;
  block_max_floor: number;
  block_year_completed: number;
  dist_mrt_m: number;
  nearest_mrt_name: string;
  n_mrt_exits_500m: number;
  n_primary_schools_1km: number;
  dist_nearest_primary_m: number;
  dist_hawker_m: number;
  dist_park_m: number;
  dist_cbd_m: number;
  block_psm_24m: number | null;
  block_sales_24m: number;
  flat_models: Record<string, string>;
}

export interface Flat {
  flatType: string;
  floorArea: number;
  storey: number;
}

export type RawFeatures = Record<string, string | number | null>;

const LEASE_YEARS = 99;
const STOREY_RANGE = 3;

export function blockRows(file: BlocksFile): Block[] {
  return file.rows.map((row) => {
    const block: Record<string, unknown> = {};
    file.columns.forEach((column, i) => (block[column] = row[i]));
    return block as unknown as Block;
  });
}

/** Months since January 1990 of "YYYY-MM" (as marts.resale_features.month_index). */
export function monthIndex(month: string): number {
  const [year, mon] = month.split("-").map(Number) as [number, number];
  return (year - 1990) * 12 + mon - 1;
}

/** "YYYY-MM" of a date, in Singapore time. */
export function monthOf(date: Date): string {
  const sgt = new Date(date.getTime() + 8 * 3600 * 1000);
  return `${sgt.getUTCFullYear()}-${String(sgt.getUTCMonth() + 1).padStart(2, "0")}`;
}

/** numerator / denominator (non-negative integers) rounded half up to `places`, as
 * Postgres round(numeric) and the CLI's sql_round do; exact, in integer arithmetic. */
export function roundHalfUp(numerator: number, denominator: number, places: number): number {
  const scale = 10 ** places;
  return Math.floor((2 * numerator * scale + denominator) / (2 * denominator)) / scale;
}

/** Middle of the 3-storey range containing `storey` (10 -> "10 TO 12" -> 11). */
export function storeyMid(storey: number): number {
  if (!Number.isInteger(storey) || storey < 1) throw new RangeError(`storey must be a whole number from 1, got ${storey}`);
  const low = Math.floor((storey - 1) / STOREY_RANGE) * STOREY_RANGE + 1;
  return low + Math.floor(STOREY_RANGE / 2);
}

/**
 * The model's raw inputs for a flat in `block`, priced in the month after the latest data
 * (`as_of`). Remaining lease is HDB's reported figure carried to `as_of` by the export, then
 * counted down to `today` if today is later; flat age is counted to the same month.
 */
export function rawFeatures(block: Block, flat: Flat, file: BlocksFile, today: Date): RawFeatures {
  const priced = file.as_of;
  const elapsed = Math.max(0, monthIndex(monthOf(today)) - monthIndex(priced));
  const leaseMonth = monthIndex(priced) + elapsed;
  // Months from January of the lease start year, as staging counts flat age.
  const ageMonths = Math.max(0, leaseMonth - monthIndex(`${block.lease_commence_year}-01`));
  const remaining = Math.min(Math.max(block.remaining_lease_months - elapsed, 0), LEASE_YEARS * 12);
  const mid = storeyMid(flat.storey);
  const rolling = file.town_rolling[block.town]?.[flat.flatType] ?? [];
  const raw: RawFeatures = {
    town: block.town,
    flat_type: flat.flatType,
    flat_model: block.flat_models[flat.flatType] ?? file.town_flat_model[block.town]?.[flat.flatType] ?? null,
    planning_area: block.planning_area,
    floor_area_sqm: flat.floorArea,
    storey_mid: mid,
    rel_storey: roundHalfUp(Math.min(mid, block.block_max_floor), block.block_max_floor, 3),
    remaining_lease_months: remaining,
    flat_age_years: roundHalfUp(ageMonths, 12, 2),
    month_index: monthIndex(priced),
    block_psm_24m: block.block_psm_24m,
    block_sales_24m: block.block_sales_24m,
  };
  for (const name of [
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
  ] as const) {
    raw[name] = block[name];
  }
  file.town_rolling_columns.forEach((name, i) => (raw[name] = rolling[i] ?? null));
  return raw;
}

/** Inputs in features.json order as float32; missing values and categories the model never
 * saw become NaN, as in training. */
export function encode(raw: RawFeatures, spec: FeatureSpec): Float32Array {
  return Float32Array.from(spec.features, ({ name, kind }) => {
    const value = raw[name];
    if (value === null || value === undefined) return Number.NaN;
    if (kind === "categorical") return spec.categories[name]?.[String(value)] ?? Number.NaN;
    return typeof value === "number" ? value : Number.NaN;
  });
}
