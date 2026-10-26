// Loading the exported data in site/public/data (see site/src/schemas for every format).

export interface Columnar {
  columns: string[];
  rows: unknown[][];
}

export interface Source {
  source: string;
  dataset_id: string;
  name: string;
  url: string;
  last_updated: string | null;
  retrieved: string;
}

export interface Meta {
  coverage: { from: string; to: string; transactions: number };
  last_refresh: string;
  priced_for: string;
  model: {
    version: string;
    trained_on: string;
    data_window: Record<string, string>;
    test?: { sales: number; mae: number; mape: number; r2: number; interval_80_coverage: number };
  };
  towns: { town: string; file: string }[];
  sources: Source[];
  licence: { name: string; url: string };
  geocoder: { name: string; url: string };
  disclaimer: string;
}

export interface Overview {
  from: string;
  to: string;
  national: Columnar & { from: string };
  by_flat_type: Columnar;
}

export interface TownMonth extends Columnar {
  from: string;
}

export interface LeaseDecay extends Columnar {
  from: string;
  to: string;
  min_sales: number;
}

export interface RecentSales extends Columnar {
  town: string;
  from: string;
  to: string;
}

export interface AreaProperties {
  planning_area: string;
  region: string;
  sales_12m: number;
  median_psm_12m: number | null;
  sales_prev_12m: number;
  median_psm_prev_12m: number | null;
  yoy_change: number | null;
  trend: [number, number, number][];
}

export interface PlanningAreas {
  type: "FeatureCollection";
  simplify_tolerance_m: number;
  trend_columns: string[];
  features: {
    type: "Feature";
    geometry: { type: "Polygon" | "MultiPolygon"; coordinates: unknown };
    properties: AreaProperties;
  }[];
}

export const DATA_URL = `${import.meta.env.BASE_URL}data/`;

const cache = new Map<string, Promise<unknown>>();

/** Fetch and parse one JSON file under data/, once per page load. */
export function loadJson<T>(path: string): Promise<T> {
  let pending = cache.get(path);
  if (!pending) {
    pending = fetch(DATA_URL + path).then((response) => {
      if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
      return response.json();
    });
    pending.catch(() => cache.delete(path));
    cache.set(path, pending);
  }
  return pending as Promise<T>;
}

export const loadMeta = () => loadJson<Meta>("meta.json");

/** Rows of a columnar table as objects keyed by column name. */
export function records<T>(table: Columnar): T[] {
  const { columns } = table;
  return table.rows.map((row) => {
    const record: Record<string, unknown> = {};
    columns.forEach((column, i) => (record[column] = row[i]));
    return record as T;
  });
}

/** "2026-10" -> Date at the start of that month (UTC). */
export function parseMonth(month: string): Date {
  const [year, mon] = month.split("-").map(Number) as [number, number];
  return new Date(Date.UTC(year, mon - 1, 1));
}
