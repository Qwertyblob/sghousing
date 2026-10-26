// Unit tests for the feature builder: node --test --experimental-strip-types (npm test).

import assert from "node:assert/strict";
import { test } from "node:test";
import { encode, monthIndex, monthOf, rawFeatures, roundHalfUp, storeyMid, type Block, type BlocksFile, type FeatureSpec } from "../src/predict/features.ts";

test("storeyMid maps a storey to the middle of its 3-storey range", () => {
  assert.deepEqual([1, 2, 3, 4, 10, 12, 13].map(storeyMid), [2, 2, 2, 5, 11, 11, 14]);
  assert.throws(() => storeyMid(0), RangeError);
  assert.throws(() => storeyMid(2.5), RangeError);
});

test("roundHalfUp rounds halves away from zero like Postgres", () => {
  assert.equal(roundHalfUp(5, 16, 3), 0.313); // 0.3125; Math.round(312.5) / 1000 agrees, toFixed does not
  assert.equal(roundHalfUp(3, 12, 2), 0.25);
  assert.equal(roundHalfUp(1, 8, 2), 0.13); // 0.125
  assert.equal(roundHalfUp(11, 11, 3), 1);
  assert.equal(roundHalfUp(589, 12, 2), 49.08);
});

test("monthIndex counts months from January 1990", () => {
  assert.equal(monthIndex("1990-01"), 0);
  assert.equal(monthIndex("2026-11"), 442);
});

test("monthOf uses Singapore time", () => {
  assert.equal(monthOf(new Date("2026-10-31T17:00:00Z")), "2026-11");
  assert.equal(monthOf(new Date("2026-10-31T15:00:00Z")), "2026-10");
});

const block: Block = {
  address_key: "1 TEST RD",
  block: "1",
  street_name: "TEST RD",
  label: "Blk 1 Test Rd",
  town: "BEDOK",
  planning_area: "BEDOK",
  region: "EAST REGION",
  lat: 1.33,
  lon: 103.93,
  lease_commence_year: 1980,
  remaining_lease_months: 650,
  block_max_floor: 12,
  block_year_completed: 1979,
  dist_mrt_m: 500,
  nearest_mrt_name: "BEDOK MRT STATION",
  n_mrt_exits_500m: 1,
  n_primary_schools_1km: 2,
  dist_nearest_primary_m: 300,
  dist_hawker_m: 200,
  dist_park_m: 400,
  dist_cbd_m: 11000,
  block_psm_24m: null,
  block_sales_24m: 2,
  flat_models: { "3 ROOM": "Improved" },
};
const file: BlocksFile = {
  as_of: "2026-11",
  town_rolling_columns: ["town_psm_3m", "town_psm_6m", "town_psm_12m"],
  town_rolling: { BEDOK: { "4 ROOM": [5000, 5100, null] } },
  town_flat_model: { BEDOK: { "4 ROOM": "Model A" } },
  columns: [],
  rows: [],
};

test("rawFeatures counts lease down after as_of and falls back to the town's flat model", () => {
  const flat = { flatType: "4 ROOM", floorArea: 92, storey: 14 };
  const now = rawFeatures(block, flat, file, new Date("2026-10-26T00:00:00Z"));
  assert.equal(now.remaining_lease_months, 650);
  assert.equal(now.flat_age_years, roundHalfUp((2026 - 1980) * 12 + 10, 12, 2));
  assert.equal(now.flat_model, "Model A");
  assert.equal(now.storey_mid, 14);
  assert.equal(now.rel_storey, 1); // capped at the top floor
  assert.equal(now.town_psm_12m, null);
  const later = rawFeatures(block, flat, file, new Date("2027-02-10T00:00:00Z"));
  assert.equal(later.remaining_lease_months, 647);
  assert.equal(later.month_index, now.month_index);
});

test("encode orders features, codes categories and turns gaps into NaN", () => {
  const spec: FeatureSpec = {
    model_version: "test",
    input: { name: "input", dtype: "float32", missing: "NaN" },
    features: [
      { name: "town", kind: "categorical" },
      { name: "flat_model", kind: "categorical" },
      { name: "floor_area_sqm", kind: "numeric" },
      { name: "town_psm_12m", kind: "numeric" },
    ],
    categories: { town: { BEDOK: 3 }, flat_model: { Improved: 0 } },
    outputs: {},
  };
  const vector = encode({ town: "BEDOK", flat_model: "Model A", floor_area_sqm: 92.5, town_psm_12m: null }, spec);
  assert.equal(vector[0], 3);
  assert.ok(Number.isNaN(vector[1]));
  assert.equal(vector[2], 92.5);
  assert.ok(Number.isNaN(vector[3]));
});
