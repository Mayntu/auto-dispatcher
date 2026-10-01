-- Autodispatcher storage (CLAUDE.md §17). Applied by the recorder service on startup.
CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS train_state (
  ts timestamptz NOT NULL,
  sim_time double precision,
  train_id text,
  status text,
  segment_id text,
  station_id text,
  track_id text,
  km double precision,
  speed_kmh double precision,
  delay_s double precision,
  regime text,
  energy_kwh double precision
);
SELECT create_hypertable('train_state', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS train_state_train_ts ON train_state (train_id, ts DESC);

CREATE TABLE IF NOT EXISTS events (
  ts timestamptz NOT NULL,
  sim_time double precision,
  id uuid,
  type text,
  source text,
  payload jsonb
);
SELECT create_hypertable('events', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS events_type_ts ON events (type, ts DESC);

CREATE TABLE IF NOT EXISTS kpi (
  ts timestamptz NOT NULL,
  sim_time double precision,
  value double precision,
  category text,
  components jsonb
);
SELECT create_hypertable('kpi', 'ts', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS plans (
  version int PRIMARY KEY,
  created_at timestamptz,
  sim_time double precision,
  solver text,
  strategy text,
  index_value double precision,
  payload jsonb
);

CREATE TABLE IF NOT EXISTS variants (
  id text PRIMARY KEY,
  created_at timestamptz,
  base_plan_version int,
  strategy text,
  status text,
  index_value double precision,
  payload jsonb
);

CREATE TABLE IF NOT EXISTS incidents (
  id text PRIMARY KEY,
  type text,
  status text,
  payload jsonb,
  updated_at timestamptz
);

CREATE TABLE IF NOT EXISTS dc_log (
  ts timestamptz NOT NULL,
  sim_time double precision,
  actor text,
  command jsonb,
  ok boolean,
  reason text
);

CREATE TABLE IF NOT EXISTS settings (
  key text PRIMARY KEY,
  value jsonb,
  updated_at timestamptz,
  updated_by text
);

SELECT add_retention_policy('train_state', INTERVAL '72 hours', if_not_exists => TRUE);
SELECT add_retention_policy('events', INTERVAL '72 hours', if_not_exists => TRUE);
SELECT add_retention_policy('kpi', INTERVAL '72 hours', if_not_exists => TRUE);
