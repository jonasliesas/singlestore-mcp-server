---
name: singlestore-create-pipeline
description: "Create a SingleStore pipeline from an S3 path, Kafka topic or other source."
argument-hint: "<source> [into table] [database]"
---

Create a SingleStore pipeline for: "$ARGUMENTS". 1) Inspect the source format and propose the target table DDL (shard and sort key) if the table doesn't exist. 2) Write the CREATE PIPELINE (use pipeline_source_file() where useful), show it, and wait for the user's OK before creating anything. 3) After creating it, run test_pipeline with a small LIMIT, start it and open the pipeline monitor.
