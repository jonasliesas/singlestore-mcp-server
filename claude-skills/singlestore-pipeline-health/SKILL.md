---
name: singlestore-pipeline-health
description: "Check all SingleStore pipelines for errors, stalls and lag."
argument-hint: "[database]"
---

Check the health of the SingleStore pipelines (database: "$ARGUMENTS", or all databases if empty): state, latest batches, recent errors, files not yet loaded and Kafka lag. Summarize per pipeline (OK / needs attention, and why), suggest fixes, and open the pipeline monitor app.
