// The three quantile models in the browser with ONNX Runtime Web (WebAssembly). Loaded only
// by the Predict page: onnxruntime-web and the models are downloaded on first use.

import type { InferenceSession } from "onnxruntime-web";
import { DATA_URL } from "../data.ts";
import type { FeatureSpec } from "./features.ts";

export interface Models {
  sessions: Record<string, InferenceSession>;
  spec: FeatureSpec;
}

export interface Quantiles {
  p10: number;
  p50: number;
  p90: number;
}

type Progress = (loaded: number, total: number | null) => void;

async function download(url: string, onBytes: (n: number) => void): Promise<{ bytes: Uint8Array; size: number | null }> {
  const response = await fetch(url);
  if (!response.ok || !response.body) throw new Error(`${url}: HTTP ${response.status}`);
  // Content-Length counts bytes on the wire. A host that compresses the file again
  // (Content-Encoding) hands the stream decoded bytes, which would overrun that total, so
  // the size is then unknown rather than in different units from the bytes counted.
  const encoding = response.headers.get("content-encoding");
  const size = encoding && encoding !== "identity" ? null : Number(response.headers.get("content-length")) || null;
  const chunks: Uint8Array[] = [];
  const reader = response.body.getReader();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    onBytes(value.length);
  }
  const bytes = new Uint8Array(chunks.reduce((n, c) => n + c.length, 0));
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.length;
  }
  return { bytes, size };
}

/** The models are published gzipped; a host that already decoded them sends plain bytes. */
async function gunzip(bytes: Uint8Array): Promise<Uint8Array> {
  if (bytes[0] !== 0x1f || bytes[1] !== 0x8b) return bytes;
  const stream = new Blob([bytes as BlobPart]).stream().pipeThrough(new DecompressionStream("gzip"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

/** Download and start every model in `spec.outputs`, reporting compressed bytes received.
 * Not cancellable: the models are kept for the rest of the visit. */
export async function loadModels(spec: FeatureSpec, onProgress: Progress): Promise<Models> {
  const ort = await import("onnxruntime-web/wasm");
  // GitHub Pages cannot send the headers that multi-threaded WebAssembly needs; the proxy
  // runs the WebAssembly in a worker so compiling it and the models never blocks the page.
  ort.env.wasm.numThreads = 1;
  ort.env.wasm.proxy = true;
  const sizes = new Map<string, number | null>();
  let loaded = 0;
  const report = () => {
    const known = [...sizes.values()];
    const total = known.length === Object.keys(spec.outputs).length && known.every((s) => s !== null)
      ? known.reduce<number>((n, s) => n + (s ?? 0), 0)
      : null;
    onProgress(loaded, total);
  };
  const entries = await Promise.all(
    Object.entries(spec.outputs).map(async ([name, file]) => {
      const { bytes } = await download(`${DATA_URL}model/${file}.gz`, (n) => {
        loaded += n;
        report();
      }).then((result) => {
        sizes.set(name, result.size);
        report();
        return result;
      });
      const session = await ort.InferenceSession.create(await gunzip(bytes), {
        executionProviders: ["wasm"],
        graphOptimizationLevel: "all",
      });
      return [name, session] as const;
    }),
  );
  return { sessions: Object.fromEntries(entries), spec };
}

/** Log-price predictions of one model for `rows` (each in features.json order). */
async function run(models: Models, name: string, rows: Float32Array[]): Promise<Float32Array> {
  const ort = await import("onnxruntime-web/wasm");
  const width = models.spec.features.length;
  const data = new Float32Array(rows.length * width);
  rows.forEach((row, i) => data.set(row, i * width));
  const session = models.sessions[name]!;
  const output = await session.run({ [models.spec.input.name]: new ort.Tensor("float32", data, [rows.length, width]) });
  return output[session.outputNames[0]!]!.data as Float32Array;
}

/** Prices for `rows`, sorted so p10 <= p50 <= p90 (as the CLI's ordered_quantiles). */
export async function predict(models: Models, rows: Float32Array[]): Promise<Quantiles[]> {
  const [p10, p50, p90] = await Promise.all(["p10", "p50", "p90"].map((name) => run(models, name, rows)));
  return rows.map((_, i) => {
    const [low, mid, high] = [p10![i]!, p50![i]!, p90![i]!].map(Math.exp).sort((a, b) => a - b) as [number, number, number];
    return { p10: low, p50: mid, p90: high };
  });
}

/** Median prices only, for comparing variations of one flat. */
export async function predictMedian(models: Models, rows: Float32Array[]): Promise<number[]> {
  return Array.from(await run(models, "p50", rows), Math.exp);
}
