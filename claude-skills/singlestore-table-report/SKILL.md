---
name: singlestore-table-report
description: "Report on a SingleStore table: size, storage, keys and data profile."
argument-hint: "<table> [database]"
---

Give a report on the SingleStore table "$ARGUMENTS" (names are case-sensitive): row count, storage type and size, shard and sort keys, columns with types, and a short data profile using cheap queries. Point out anything unusual. Then open it in the schema explorer.
