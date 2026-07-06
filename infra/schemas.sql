-- FraudGraph BigQuery DDL - dataset `fraudgraph` and its seven tables.
-- Run with the default project set:  bq query --use_legacy_sql=false < infra/schemas.sql

CREATE SCHEMA IF NOT EXISTS fraudgraph;

-- Note: no NOT NULL constraints — Parquet load jobs present all fields as
-- NULLABLE; non-nullness is guaranteed by the generator and checked in its
-- sanity suite instead.
CREATE TABLE IF NOT EXISTS fraudgraph.accounts (
  account_id  STRING,
  created_at  TIMESTAMP,
  geo         STRING,
  base_risk   FLOAT64,
  is_mule_gt  BOOL      -- ground truth; exists only because data is synthetic
);

CREATE TABLE IF NOT EXISTS fraudgraph.transactions (
  txn_id       STRING,
  ts           TIMESTAMP,
  src_account  STRING,
  dst_account  STRING,
  amount       FLOAT64,
  channel      STRING,
  device_id    STRING,
  is_fraud_gt  BOOL     -- ground truth; exists only because data is synthetic
)
PARTITION BY DATE(ts);

CREATE TABLE IF NOT EXISTS fraudgraph.ring_assignments (
  account_id  STRING,
  ring_id     STRING,
  method      STRING,   -- 'louvain' (detected) | 'ground_truth' (generator)
  run_id      STRING
);

CREATE TABLE IF NOT EXISTS fraudgraph.ring_features (
  ring_id       STRING,
  n_accounts    INT64,
  n_txns        INT64,
  total_amount  FLOAT64,
  density       FLOAT64,
  cyclicity     FLOAT64,
  fanin_ratio   FLOAT64,
  burstiness    FLOAT64,
  pattern_label STRING,
  risk_score    FLOAT64
);

CREATE TABLE IF NOT EXISTS fraudgraph.benchmarks (
  run_id            STRING,
  engine            STRING,   -- 'cpu' | 'gpu'
  operation         STRING,   -- load_parquet | etl_features | graph_build | wcc | louvain | pagerank
  n_edges           INT64,
  wall_seconds      FLOAT64,  -- NULL when status is a DNF
  warm              BOOL,
  hardware          STRING,
  library_versions  STRING,
  status            STRING    -- 'ok' | 'DNF >N min' (runs that hit the time cap stay in the record)
);

CREATE TABLE IF NOT EXISTS fraudgraph.case_files (
  ring_id            STRING,
  created_at         TIMESTAMP,
  pattern            STRING,
  narrative_md       STRING,
  evidence_txn_ids   ARRAY<STRING>,
  recommended_action STRING,   -- freeze | enhanced_monitoring | dismiss
  model              STRING,
  status             STRING    -- draft | approved | rejected | escalated
);

CREATE TABLE IF NOT EXISTS fraudgraph.analyst_decisions (
  ring_id     STRING,
  decision    STRING,   -- approve | reject | escalate
  note        STRING,
  decided_at  TIMESTAMP
);
