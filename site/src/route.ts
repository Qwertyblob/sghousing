// Hash routes: "#/trends?town=BEDOK". Pages keep their filters in the query string so a view
// can be shared or bookmarked.

/** "#/trends?town=BEDOK" -> ["trends", {town: "BEDOK"}]. */
export function parseHash(hash: string): [string, URLSearchParams] {
  const [path = "", query = ""] = hash.replace(/^#\/?/, "").split("?");
  return [path, new URLSearchParams(query)];
}

/** Rewrite the current route's query string without adding a history entry or re-rendering
 * (replaceState fires no hashchange). */
export function setParams(params: Record<string, string>): void {
  const [path] = parseHash(location.hash);
  const query = new URLSearchParams(params).toString();
  history.replaceState(history.state, "", `#/${path}${query ? `?${query}` : ""}`);
}
