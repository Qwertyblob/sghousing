// Model inputs built by the site's feature builder, for the parity test
// (tests/test_site_features.py): node --experimental-strip-types scripts/vectors.ts
// <blocks.json> <features.json> <cases.json> <today ISO>. Prints one array per case, with
// null for NaN.

import { readFileSync } from "node:fs";
import { blockRows, encode, rawFeatures, type BlocksFile, type FeatureSpec } from "../src/predict/features.ts";

interface Case {
  address_key: string;
  flat_type: string;
  floor_area_sqm: number;
  storey: number;
}

const [blocksPath, specPath, casesPath, today] = process.argv.slice(2);
if (!blocksPath || !specPath || !casesPath || !today) throw new Error("usage: vectors.ts <blocks> <features> <cases> <today>");
const file = JSON.parse(readFileSync(blocksPath, "utf-8")) as BlocksFile;
const spec = JSON.parse(readFileSync(specPath, "utf-8")) as FeatureSpec;
const cases = JSON.parse(readFileSync(casesPath, "utf-8")) as Case[];
const blocks = new Map(blockRows(file).map((b) => [b.address_key, b]));
const vectors = cases.map((c) => {
  const block = blocks.get(c.address_key);
  if (!block) throw new Error(`no block ${c.address_key}`);
  const raw = rawFeatures(block, { flatType: c.flat_type, floorArea: c.floor_area_sqm, storey: c.storey }, file, new Date(today));
  return Array.from(encode(raw, spec), (v) => (Number.isNaN(v) ? null : v));
});
process.stdout.write(JSON.stringify(vectors));
