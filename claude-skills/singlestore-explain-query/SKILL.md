---
name: singlestore-explain-query
description: "Explain a SingleStore query and suggest how to make it faster."
argument-hint: "<sql> [in database]"
---

Use the singlestore MCP server's `explain_query` approach for this SQL: "$ARGUMENTS". Check the tables' columns, shard and sort keys and sizes, run EXPLAIN with run_sql, explain what it shows, then give an improved query (and any key change as a separate statement for the user to run), with a short reason for each change. Don't run statements that change data.
