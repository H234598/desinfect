(function (root, factory) {
  "use strict";

  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else if (root) {
    root.DesinfectTable = api;
  }

  if (typeof document !== "undefined") {
    const start = () => api.init(document);
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", start, { once: true });
    } else {
      start();
    }
  }
})(typeof globalThis === "undefined" ? this : globalThis, function () {
  "use strict";

  const collator = new Intl.Collator("de", { sensitivity: "base", usage: "sort" });
  const confidenceOrder = new Map([
    ["high", 0],
    ["medium", 1],
    ["low", 2],
  ]);
  const enhancedTables = new WeakSet();

  function normalize(value) {
    return String(value ?? "").normalize("NFKC").trim().toLocaleLowerCase("de");
  }

  function isMissing(value) {
    const normalized = normalize(value);
    return normalized === "" || normalized === "—";
  }

  function compareValues(left, right, type, direction) {
    const leftMissing = isMissing(left);
    const rightMissing = isMissing(right);
    if (leftMissing || rightMissing) {
      if (leftMissing && rightMissing) {
        return 0;
      }
      return leftMissing ? 1 : -1;
    }

    let comparison;
    if (type === "number") {
      comparison = Number(normalize(left).replace(",", ".")) - Number(
        normalize(right).replace(",", "."),
      );
    } else if (type === "confidence") {
      comparison =
        (confidenceOrder.get(normalize(left)) ?? confidenceOrder.size) -
        (confidenceOrder.get(normalize(right)) ?? confidenceOrder.size);
    } else {
      comparison = collator.compare(normalize(left), normalize(right));
    }
    return direction === "descending" ? -comparison : comparison;
  }

  function cellText(cell) {
    return typeof cell === "object" && cell !== null && "textContent" in cell
      ? cell.textContent
      : cell;
  }

  function stableSortRows(rows, column, type, direction) {
    return Array.from(rows)
      .map((row, index) => ({ index, row }))
      .sort((left, right) => {
        const comparison = compareValues(
          cellText(left.row.cells[column]),
          cellText(right.row.cells[column]),
          type,
          direction,
        );
        return comparison || left.index - right.index;
      })
      .map(({ row }) => row);
  }

  function filterRows(rows, query, applicationArea) {
    const wantedText = normalize(query);
    const wantedArea = normalize(applicationArea);
    return Array.from(rows).filter((row) => {
      const cells = Array.from(row.cells ?? []).filter((cell) => cell.hidden !== true);
      const matchesText =
        wantedText === "" ||
        cells.some((cell) => normalize(cellText(cell)).includes(wantedText));
      const matchesArea =
        wantedArea === "" || normalize(row.applicationArea) === wantedArea;
      return matchesText && matchesArea;
    });
  }

  function enhanceTable(table, documentObject) {
    if (enhancedTables.has(table)) {
      return table;
    }
    const headers = Array.from(
      table.querySelectorAll("thead th[data-column][data-sort-type]"),
    );
    const tbody = table.tBodies?.[0];
    if (headers.length === 0 || !tbody || !table.parentNode) {
      return table;
    }

    for (const [column, header] of headers.entries()) {
      const button = documentObject.createElement("button");
      button.type = "button";
      button.textContent = header.textContent;
      button.addEventListener("click", () => {
        const direction =
          header.getAttribute("aria-sort") === "ascending" ? "descending" : "ascending";
        for (const otherHeader of headers) {
          otherHeader.removeAttribute("aria-sort");
        }
        header.setAttribute("aria-sort", direction);
        tbody.replaceChildren(
          ...stableSortRows(
            tbody.rows,
            column,
            header.getAttribute("data-sort-type"),
            direction,
          ),
        );
      });
      header.replaceChildren(button);
    }

    const rows = Array.from(tbody.rows);
    const applicationAreaColumn = headers.findIndex(
      (header) => header.getAttribute("data-column") === "application_area",
    );
    const rowViews = rows.map((row) => ({
      applicationArea:
        applicationAreaColumn === -1 ? "" : cellText(row.cells[applicationAreaColumn]),
      cells: Array.from(row.cells),
      row,
    }));
    const controls = documentObject.createElement("div");
    const searchLabel = documentObject.createElement("label");
    searchLabel.textContent = "Tabelle filtern";
    const search = documentObject.createElement("input");
    search.type = "search";
    searchLabel.append(search);
    controls.append(searchLabel);

    let areaSelect = null;
    if (
      table.getAttribute("data-enhance-table") === "instructions" &&
      applicationAreaColumn !== -1
    ) {
      const areaLabel = documentObject.createElement("label");
      areaLabel.textContent = "Anwendungskontext";
      areaSelect = documentObject.createElement("select");
      const allAreas = documentObject.createElement("option");
      allAreas.value = "";
      allAreas.textContent = "Alle Anwendungskontexte";
      areaSelect.append(allAreas);
      const areas = [...new Set(rowViews.map((row) => row.applicationArea))].sort((left, right) =>
        compareValues(left, right, "text", "ascending"),
      );
      for (const area of areas) {
        const option = documentObject.createElement("option");
        option.value = area;
        option.textContent = area;
        areaSelect.append(option);
      }
      areaLabel.append(areaSelect);
      controls.append(areaLabel);
    }

    const status = documentObject.createElement("p");
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    controls.append(status);

    const updateVisibility = () => {
      const visibleRows = new Set(filterRows(rowViews, search.value, areaSelect?.value ?? ""));
      for (const row of rowViews) {
        row.row.hidden = !visibleRows.has(row);
      }
      status.textContent = `${visibleRows.size} von ${rows.length} Einträgen`;
    };
    search.addEventListener("input", updateVisibility);
    areaSelect?.addEventListener("change", updateVisibility);
    updateVisibility();

    table.parentNode.insertBefore(controls, table);
    enhancedTables.add(table);
    return table;
  }

  function init(documentObject) {
    for (const table of documentObject.querySelectorAll("table[data-enhance-table]")) {
      enhanceTable(table, documentObject);
    }
  }

  return {
    compareValues,
    enhanceTable,
    filterRows,
    init,
    normalize,
    stableSortRows,
  };
});
