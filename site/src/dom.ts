// Small DOM helpers: elements, tables and controls with their accessibility wiring.

type Child = Node | string | null | undefined | false;

/** Create an element with attributes and children; `on*` function attributes become listeners. */
export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Record<string, unknown> = {},
  ...children: (Child | Child[])[]
): HTMLElementTagNameMap[K] {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (name.startsWith("on") && typeof value === "function") {
      element.addEventListener(name.slice(2).toLowerCase(), value as EventListener);
    } else if (name === "class") {
      element.className = String(value);
    } else if (value === true) {
      element.setAttribute(name, "");
    } else {
      element.setAttribute(name, String(value));
    }
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    element.append(child);
  }
  return element;
}

let ids = 0;
export const uid = (prefix: string): string => `${prefix}-${++ids}`;

export interface TableColumn<T> {
  label: string;
  value: (row: T) => string;
  numeric?: boolean;
}

export function table<T>(caption: string, columns: TableColumn<T>[], rows: T[]): HTMLElement {
  return h(
    "div",
    { class: "table-wrap", tabindex: 0, role: "region", "aria-label": caption },
    h(
      "table",
      {},
      h("caption", {}, caption),
      h(
        "thead",
        {},
        h("tr", {}, columns.map((c) => h("th", { scope: "col", class: c.numeric ? "num" : undefined }, c.label))),
      ),
      h(
        "tbody",
        {},
        rows.map((row) =>
          h("tr", {}, columns.map((c) => h("td", { class: c.numeric ? "num" : undefined }, c.value(row)))),
        ),
      ),
    ),
  );
}

/** A table behind a "Show data table" disclosure: the accessible view of a chart. */
export function dataTable<T>(caption: string, columns: TableColumn<T>[], rows: T[]): HTMLElement {
  return h("details", {}, h("summary", {}, "Show data table"), table(caption, columns, rows));
}

export function select(
  label: string,
  options: { value: string; label: string }[],
  value: string,
  onChange: (value: string) => void,
): HTMLElement {
  const id = uid("select");
  const element = h(
    "select",
    { id, onchange: (e: Event) => onChange((e.target as HTMLSelectElement).value) },
    options.map((o) => h("option", { value: o.value, selected: o.value === value }, o.label)),
  );
  return h("div", { class: "field" }, h("label", { for: id }, label), element);
}

/** A group of toggle buttons where exactly one is pressed. */
export function segmented(
  label: string,
  options: { value: string; label: string }[],
  value: string,
  onChange: (value: string) => void,
): HTMLElement {
  const buttons = options.map((o) =>
    h(
      "button",
      {
        type: "button",
        "aria-pressed": String(o.value === value),
        onclick: () => {
          for (const b of buttons) b.setAttribute("aria-pressed", String(b === buttonFor(o.value)));
          onChange(o.value);
        },
        "data-value": o.value,
      },
      o.label,
    ),
  );
  const buttonFor = (v: string) => buttons.find((b) => b.dataset.value === v);
  return h(
    "div",
    { class: "field" },
    h("fieldset", { class: "segmented" }, h("legend", {}, label), buttons),
  );
}

export function notice(text: string, error = false): HTMLElement {
  return h("p", { class: error ? "notice error" : "notice", role: error ? "alert" : undefined }, text);
}

/** Show a page's heading and introduction at once, with "Loading…" under them, and return the
 * function that swaps that line for the page body when its data has arrived. The heading is
 * never re-rendered, so it is painted (and counted as the largest paint) without waiting. */
export function startPage(root: HTMLElement, title: string, lead: string): (...body: (Child | Child[])[]) => void {
  const loading = h("p", { class: "loading" }, "Loading…");
  root.replaceChildren(h("h1", {}, title), h("p", { class: "lead" }, lead), loading);
  return (...body) => {
    const nodes = body.flat().filter((c): c is Node | string => c !== null && c !== undefined && c !== false);
    loading.replaceWith(...nodes);
  };
}
