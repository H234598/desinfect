import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const require = createRequire(import.meta.url);
const moduleUrl = new URL("../../web/assets/javascripts/table.js", import.meta.url);
const api = require(fileURLToPath(moduleUrl));
const {
  compareValues,
  enhanceTable,
  filterRows,
  init,
  normalize,
  stableSortRows,
} = api;

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.attributes = new Map();
    this.children = [];
    this.listeners = new Map();
    this.parentNode = null;
    this.hidden = false;
    this.type = "";
    this.value = "";
  }

  get textContent() {
    return this.children
      .map((child) => (typeof child === "string" ? child : child.textContent))
      .join("");
  }

  set textContent(value) {
    this.replaceChildren(String(value));
  }

  set innerHTML(_value) {
    assert.fail("table enhancement must not use innerHTML");
  }

  get rows() {
    return this.children.filter((child) => child.tagName === "TR");
  }

  append(...children) {
    for (const child of children) {
      if (typeof child !== "string") {
        child.parentNode = this;
      }
      this.children.push(child);
    }
  }

  replaceChildren(...children) {
    for (const child of this.children) {
      if (typeof child !== "string") {
        child.parentNode = null;
      }
    }
    this.children = [];
    this.append(...children);
  }

  insertBefore(child, reference) {
    const index = this.children.indexOf(reference);
    assert.notEqual(index, -1);
    child.parentNode = this;
    this.children.splice(index, 0, child);
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.get(name) ?? null;
  }

  hasAttribute(name) {
    return this.attributes.has(name);
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  dispatch(type) {
    for (const listener of this.listeners.get(type) ?? []) {
      listener({ currentTarget: this, type });
    }
  }
}

class FakeDocument {
  constructor(tables = [], readyState = "complete") {
    this.tables = tables;
    this.readyState = readyState;
    this.listeners = new Map();
  }

  createElement(tagName) {
    return new FakeElement(tagName);
  }

  querySelectorAll(selector) {
    assert.equal(selector, "table[data-enhance-table]");
    return this.tables;
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  dispatch(type) {
    for (const listener of this.listeners.get(type) ?? []) {
      listener();
    }
  }
}

function element(tagName, text = "") {
  const node = new FakeElement(tagName);
  node.textContent = text;
  return node;
}

function makeTable({ kind = "corpus", headers, rows }) {
  const headerNodes = headers.map(({ label, column, type }) => {
    const header = element("th", label);
    header.setAttribute("data-column", column);
    header.setAttribute("data-sort-type", type);
    return header;
  });
  const headRow = element("tr");
  headRow.append(...headerNodes);
  const thead = element("thead");
  thead.append(headRow);
  const rowNodes = rows.map((values) => {
    const row = element("tr");
    row.cells = values.map((value) => element("td", value));
    row.append(...row.cells);
    return row;
  });
  const tbody = element("tbody");
  tbody.append(...rowNodes);
  const table = element("table");
  table.setAttribute("data-enhance-table", kind);
  table.tBodies = [tbody];
  table.querySelectorAll = (selector) => {
    assert.equal(selector, "thead th[data-column][data-sort-type]");
    return headerNodes;
  };
  table.append(thead, tbody);
  const region = element("div");
  region.append(table);
  return { headerNodes, region, rowNodes, table, tbody };
}

function descendants(root, tagName) {
  const matches = [];
  for (const child of root.children) {
    if (typeof child === "string") {
      continue;
    }
    if (child.tagName === tagName.toUpperCase()) {
      matches.push(child);
    }
    matches.push(...descendants(child, tagName));
  }
  return matches;
}

function rowTexts(rows) {
  return rows.map((row) => row.cells.map((cell) => cell.textContent));
}

test("text sorting normalizes German case and preserves equal-row order", () => {
  const rows = [
    { id: 1, cells: [element("td", "ärztlich")] },
    { id: 2, cells: [element("td", "Alpha")] },
    { id: 3, cells: [element("td", "ÄRZTLICH")] },
  ];

  assert.equal(normalize("  ÄRZTLICH  "), "ärztlich");
  assert.deepEqual(
    stableSortRows(rows, 0, "text", "ascending").map((row) => row.id),
    [2, 1, 3],
  );
});

test("CommonJS exports only the documented enhancement API", () => {
  assert.deepEqual(Object.keys(api).sort(), [
    "compareValues",
    "enhanceTable",
    "filterRows",
    "init",
    "normalize",
    "stableSortRows",
  ]);
});

test("Unicode compatibility symbols stay case-normalized and searchable", () => {
  const row = { cells: ["𝐀"], applicationArea: "" };

  assert.equal(normalize("𝐀"), "a");
  assert.deepEqual(filterRows([row], "a", ""), [row]);
});

test("numeric sorting keeps missing values last in both directions", () => {
  const rows = ["—", "10", "", "2"].map((value) => ({ cells: [element("td", value)] }));

  assert.deepEqual(
    stableSortRows(rows, 0, "number", "ascending").map((row) => row.cells[0].textContent),
    ["2", "10", "—", ""],
  );
  assert.deepEqual(
    stableSortRows(rows, 0, "number", "descending").map(
      (row) => row.cells[0].textContent,
    ),
    ["10", "2", "—", ""],
  );
});

test("confidence sorting is high, medium, low and reverses explicitly", () => {
  assert.ok(compareValues("high", "medium", "confidence", "ascending") < 0);
  assert.ok(compareValues("medium", "low", "confidence", "ascending") < 0);
  assert.ok(compareValues("high", "low", "confidence", "descending") > 0);
});

test("text and application-area filters combine over server row values", () => {
  const rows = [
    { cells: ["Hände", "Ethanol", "Bulletin 1"], applicationArea: "Hände" },
    { cells: ["Flächen", "Ethanol", "Bulletin 2"], applicationArea: "Flächen" },
    { cells: ["Hände", "Propanol", "Bulletin 3"], applicationArea: "Hände" },
  ];

  assert.deepEqual(filterRows(rows, "ethanol", "Hände"), [rows[0]]);
  assert.deepEqual(filterRows(rows, "BULLETIN 2", ""), [rows[1]]);
  assert.deepEqual(filterRows(rows, "", "Flächen"), [rows[1]]);
});

test("enhancement adds one native sort button and moves every existing row", () => {
  const fixture = makeTable({
    headers: [
      { label: "Titel", column: "title", type: "text" },
      { label: "Jahr", column: "year", type: "number" },
    ],
    rows: [
      ["Neu", "2021"],
      ["Alt", "2019"],
    ],
  });
  const document = new FakeDocument([fixture.table]);

  enhanceTable(fixture.table, document);

  const buttons = fixture.headerNodes.map((header) => descendants(header, "button"));
  assert.deepEqual(buttons.map((items) => items.length), [1, 1]);
  assert.ok(buttons.flat().every((button) => button.type === "button"));
  assert.ok(fixture.headerNodes.every((header) => !header.hasAttribute("aria-sort")));

  buttons[1][0].dispatch("click");
  assert.equal(fixture.headerNodes[1].getAttribute("aria-sort"), "ascending");
  assert.equal(fixture.headerNodes[0].hasAttribute("aria-sort"), false);
  assert.deepEqual(rowTexts(fixture.tbody.rows), [
    ["Alt", "2019"],
    ["Neu", "2021"],
  ]);
  assert.deepEqual(new Set(fixture.tbody.rows), new Set(fixture.rowNodes));

  buttons[0][0].dispatch("click");
  assert.equal(fixture.headerNodes[0].getAttribute("aria-sort"), "ascending");
  assert.equal(fixture.headerNodes[1].hasAttribute("aria-sort"), false);
  buttons[0][0].dispatch("click");
  assert.equal(fixture.headerNodes[0].getAttribute("aria-sort"), "descending");
});

test("instruction controls are accessible, combined, live, and idempotent", () => {
  const fixture = makeTable({
    kind: "instructions",
    headers: [
      { label: "Anwendung", column: "application_area", type: "text" },
      { label: "Titel", column: "title", type: "text" },
    ],
    rows: [
      ["Hände", "Ethanol"],
      ["Flächen", "Ethanol"],
      ["Hände", "Propanol"],
    ],
  });
  const document = new FakeDocument([fixture.table]);

  init(document);
  init(document);

  const labels = descendants(fixture.region, "label");
  const inputs = descendants(fixture.region, "input");
  const selects = descendants(fixture.region, "select");
  const statuses = descendants(fixture.region, "p").filter(
    (node) => node.getAttribute("role") === "status",
  );
  assert.equal(labels.length, 2);
  assert.equal(inputs.length, 1);
  assert.equal(selects.length, 1);
  assert.equal(statuses.length, 1);
  assert.equal(statuses[0].getAttribute("aria-live"), "polite");
  assert.equal(statuses[0].textContent, "3 von 3 Einträgen");
  assert.equal(fixture.region.children.at(-1), fixture.table);
  assert.deepEqual(
    descendants(selects[0], "option").map((option) => option.value),
    ["", "Flächen", "Hände"],
  );

  inputs[0].value = "ethanol";
  inputs[0].dispatch("input");
  selects[0].value = "Hände";
  selects[0].dispatch("change");
  assert.deepEqual(fixture.rowNodes.map((row) => row.hidden), [false, true, true]);
  assert.equal(statuses[0].textContent, "1 von 3 Einträgen");
  assert.ok(fixture.headerNodes.every((header) => descendants(header, "button").length === 1));
  assert.equal(inputs[0].listeners.get("input").length, 1);
  assert.equal(selects[0].listeners.get("change").length, 1);
});

test("browser UMD exposes API and uses both safe autostart paths without network", async () => {
  const source = await readFile(moduleUrl, "utf8");
  const autostartFixture = makeTable({
    headers: [{ label: "Titel", column: "title", type: "text" }],
    rows: [["Serverzeile"]],
  });
  const loadingDocument = new FakeDocument([autostartFixture.table], "loading");
  let loadingQueries = 0;
  loadingDocument.querySelectorAll = (selector) => {
    assert.equal(selector, "table[data-enhance-table]");
    loadingQueries += 1;
    return loadingDocument.tables;
  };
  const loadingContext = {
    document: loadingDocument,
    fetch() {
      throw new Error("network access is forbidden");
    },
  };
  loadingContext.globalThis = loadingContext;

  vm.runInNewContext(source, loadingContext);

  assert.equal(typeof loadingContext.DesinfectTable.init, "function");
  assert.equal(loadingDocument.listeners.get("DOMContentLoaded").length, 1);
  assert.equal(loadingQueries, 0);
  loadingDocument.dispatch("DOMContentLoaded");
  assert.equal(loadingQueries, 1);
  assert.equal(descendants(autostartFixture.headerNodes[0], "button").length, 1);

  const completeDocument = new FakeDocument([], "complete");
  let queries = 0;
  completeDocument.querySelectorAll = (selector) => {
    assert.equal(selector, "table[data-enhance-table]");
    queries += 1;
    return [];
  };
  const completeContext = { document: completeDocument };
  completeContext.globalThis = completeContext;
  vm.runInNewContext(source, completeContext);
  assert.equal(queries, 1);
});
