// Everything the SQL Editor app uses from CodeMirror, exposed as globalThis.CM.
// Rebuild with:  cd scripts/build_codemirror && npm install && npm run build
export { basicSetup } from "codemirror";
export { EditorView, keymap, placeholder } from "@codemirror/view";
export { EditorState, Compartment, Prec } from "@codemirror/state";
export { indentWithTab } from "@codemirror/commands";
export { sql, MySQL, SQLDialect, keywordCompletionSource, schemaCompletionSource } from "@codemirror/lang-sql";
export { autocompletion, completeFromList, ifNotIn, startCompletion } from "@codemirror/autocomplete";
export { HighlightStyle, syntaxHighlighting, syntaxTree } from "@codemirror/language";
export { tags } from "@lezer/highlight";
