// Node parity check for the exported ONNX models.
//
// Reads cases.json (written by `make evaluate`: test rows and the Python onnxruntime
// outputs), runs the same rows through onnxruntime-node and compares predicted prices.
// Exits 1 if any relative difference reaches MAX_RELATIVE_DIFF.
//
// Usage: node parity.mjs [path/to/cases.json]

import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import ort from "onnxruntime-node";

const MAX_RELATIVE_DIFF = 1e-4;

const here = dirname(fileURLToPath(import.meta.url));
const casesPath = process.argv[2] ?? join(here, "cases.json");
const cases = JSON.parse(await readFile(casesPath, "utf8"));
const spec = JSON.parse(await readFile(join(cases.model_dir, "features.json"), "utf8"));

const nRows = cases.inputs.length;
const nCols = cases.features.length;
const data = new Float32Array(nRows * nCols);
cases.inputs.forEach((row, i) => {
  row.forEach((value, j) => {
    data[i * nCols + j] = value === null ? NaN : value;
  });
});
const input = new ort.Tensor("float32", data, [nRows, nCols]);

const report = { model_version: cases.model_version, rows: nRows, max_relative_diff: {} };
let worst = 0;
for (const [name, file] of Object.entries(spec.outputs)) {
  const session = await ort.InferenceSession.create(join(cases.model_dir, file));
  const result = await session.run({ [cases.input_name]: input });
  const logPrice = result[session.outputNames[0]].data;
  const expected = cases.outputs[name];
  if (logPrice.length !== expected.length) {
    throw new Error(`${name}: ${logPrice.length} predictions, expected ${expected.length}`);
  }
  let maxDiff = 0;
  for (let i = 0; i < expected.length; i++) {
    const want = Math.exp(expected[i]);
    const diff = Math.abs(Math.exp(logPrice[i]) - want) / want;
    maxDiff = Math.max(maxDiff, diff);
  }
  report.max_relative_diff[name] = maxDiff;
  worst = Math.max(worst, maxDiff);
}
report.passed = worst < MAX_RELATIVE_DIFF;
console.log(JSON.stringify(report));
process.exit(report.passed ? 0 : 1);
