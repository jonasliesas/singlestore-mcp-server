// SAS function check for the SQL Editor's SAS tabs (PROC SQL / FedSQL / FedSQL in CAS): which functions a
// statement calls, and what the function matrix (sql_editor_sas_functions, data/sas_functions.json) says about
// them - not available in the tab's dialect, or not pushed down to SingleStore. Pure functions, no DOM:
// tests/test_sas_guide.py runs them in node. Same rules as singlestore_mcp/sas_functions.py.
(function (root) {
  "use strict";

  const LABELS = { procsql: "PROC SQL", fedsql: "FedSQL", cas: "FedSQL in CAS" };
  const NOT_FUNCS = new Set(["IN", "EXISTS", "AS", "FROM", "JOIN", "ON", "AND", "OR", "NOT", "WHEN", "THEN", "ELSE",
    "VALUES", "USING", "TO", "SELECT", "WHERE", "HAVING", "BY", "OVER", "TABLE", "INTO", "ALL", "ANY", "SOME",
    "CONNECTION", "OPTIONS"]);

  // Function tokens a statement calls; strings and comments don't count ("…" is a string in PROC SQL only).
  function functionsIn(sql, lang) {
    let text = String(sql ?? "").replace(/\/\*[\s\S]*?\*\/|--[^\n]*|'(?:[^']|'')*'/g, " ");
    text = text.replace(/"(?:[^"]|"")*"/g, lang === "procsql" ? " " : "x");
    const found = [];
    if (/\bcount\s*\(\s*distinct\b/i.test(text)) found.push("COUNT(DISTINCT)");
    for (const m of text.matchAll(/\b([A-Za-z_][A-Za-z0-9_]*)\s*\(/g)) {
      const name = m[1].toUpperCase();
      if (!NOT_FUNCS.has(name) && !found.includes(name)) found.push(name);
    }
    if (text.includes("||")) found.push("||");
    return found;
  }

  // [{function, issue: "unsupported" | "no_pushdown", alternative, sqlFunctionsAll}] for one statement.
  // ``singlestore``: the statement reads a SingleStore library / caslib (only then does pushdown matter).
  function check(sql, lang, funcs, singlestore) {
    if (!funcs || !LABELS[lang]) return [];
    const out = [];
    for (const tok of functionsIn(sql, lang)) {
      const fn = funcs[tok];
      if (!fn) continue;
      const e = fn[lang] ?? {};
      if (e.s === false) out.push({ function: tok, issue: "unsupported", alternative: fn.alternative || "" });
      else if (singlestore && e.p === "no") {
        out.push({ function: tok, issue: "no_pushdown", alternative: fn.alternative || "",
          sqlFunctionsAll: lang === "procsql" && fn.access_map === "all" });
      }
    }
    return out;
  }

  // One message for the editor's "Check the language" bar, or null.
  function message(findings, lang) {
    if (!findings.length) return null;
    const label = LABELS[lang] ?? lang;
    const parts = [];
    const bad = findings.filter((f) => f.issue === "unsupported");
    const slow = findings.filter((f) => f.issue === "no_pushdown");
    const alt = (f) => (f.alternative ? ` (${f.alternative.replace(/\.$/, "")})` : "");
    if (bad.length) parts.push(`${bad.map((f) => f.function + alt(f)).join(", ")}: not available in ${label}.`);
    if (slow.length) {
      parts.push(`${slow.map((f) => f.function + alt(f)).join(", ")}: not pushed down to SingleStore - SAS reads the rows and does `
        + `${slow.length > 1 ? "these" : "it"} itself${slow.some((f) => f.sqlFunctionsAll) ? " (SQL_FUNCTIONS=ALL on the library would pass some of them)" : ""}.`);
    }
    return parts.join(" ");
  }

  const api = { functionsIn, check, message };
  root.SasFuncs = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
