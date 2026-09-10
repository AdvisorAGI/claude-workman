-- Additive Workman computer-use schema. File JSONL is the source of truth;
-- this is the optional ingest target. The migrate wrapper refuses DROP/TRUNCATE/DELETE.

CREATE SCHEMA IF NOT EXISTS wl;

CREATE TABLE IF NOT EXISTS wl.schema_version (
  v int PRIMARY KEY, applied_at timestamptz DEFAULT now(), note text);

CREATE TABLE IF NOT EXISTS wl.devices (
  id smallserial PRIMARY KEY, name text UNIQUE NOT NULL, machine_key text,
  first_seen timestamptz DEFAULT now());

CREATE TABLE IF NOT EXISTS wl.episodes (
  id bigserial PRIMARY KEY, episode_uid uuid UNIQUE NOT NULL,
  device_id smallint REFERENCES wl.devices, session_id text,
  task text NOT NULL, app text,
  started_at timestamptz NOT NULL, ended_at timestamptz,
  outcome text NOT NULL DEFAULT 'unknown'
    CHECK (outcome IN ('success','partial','failed','aborted','unknown')),
  judged_by text, judge_note text,
  model text, rung smallint, effort text,
  n_steps int DEFAULT 0, duration_ms int, source text DEFAULT 'journal',
  created_at timestamptz DEFAULT now());
CREATE INDEX IF NOT EXISTS episodes_started_idx ON wl.episodes (started_at);
CREATE INDEX IF NOT EXISTS episodes_app_outcome_idx ON wl.episodes (app, outcome);

CREATE TABLE IF NOT EXISTS wl.steps (
  id bigserial PRIMARY KEY,
  episode_id bigint NOT NULL REFERENCES wl.episodes ON DELETE RESTRICT,
  seq int NOT NULL, ts timestamptz NOT NULL, tool text NOT NULL,
  args jsonb, result jsonb, ok boolean, error_kind text, latency_ms real,
  win_hash text, shot_sha256 text,
  UNIQUE (episode_id, seq));

CREATE TABLE IF NOT EXISTS wl.recipes (
  id bigserial PRIMARY KEY, key text UNIQUE NOT NULL,
  intent text NOT NULL, app text, steps text NOT NULL, notes text,
  outcome text, uses int DEFAULT 0, recalls int DEFAULT 0,
  last_recalled_at timestamptz, quality jsonb,
  device_id smallint REFERENCES wl.devices,
  version int DEFAULT 1, superseded_by bigint REFERENCES wl.recipes,
  created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now());

CREATE TABLE IF NOT EXISTS wl.lessons (
  id bigserial PRIMARY KEY, ts timestamptz, app text, lesson text NOT NULL,
  evidence text, source text, device_id smallint REFERENCES wl.devices,
  lesson_sha text UNIQUE);

CREATE TABLE IF NOT EXISTS wl.facts (
  id bigserial PRIMARY KEY, fact_id text UNIQUE NOT NULL,
  subject text NOT NULL, predicate text NOT NULL, object text NOT NULL,
  note text, source text, episode_uid text,
  valid_from timestamptz NOT NULL DEFAULT now(), valid_to timestamptz,
  created_at timestamptz DEFAULT now());
CREATE INDEX IF NOT EXISTS facts_subject_pred_idx ON wl.facts (subject, predicate, valid_to);

CREATE TABLE IF NOT EXISTS wl.eval_runs (
  id bigserial PRIMARY KEY, ts timestamptz DEFAULT now(), suite text NOT NULL,
  mode text, model text, rung smallint, n_tasks int, n_pass int, pass_rate real,
  mean_steps real, mean_latency_ms real, deny_hits int DEFAULT 0, git_sha text, notes text);

CREATE TABLE IF NOT EXISTS wl.eval_results (
  id bigserial PRIMARY KEY, run_id bigint NOT NULL REFERENCES wl.eval_runs,
  task_id text NOT NULL, passed boolean, steps int, latency_ms int,
  failure_kind text, transcript jsonb);

CREATE TABLE IF NOT EXISTS wl.ladder_runs (
  id bigserial PRIMARY KEY, ts timestamptz DEFAULT now(), lane text NOT NULL,
  host text, rung_requested smallint, rung_ran smallint, model text, reason text,
  ok boolean, duration_ms int, notified boolean DEFAULT false);

CREATE TABLE IF NOT EXISTS wl.renewals (
  id bigserial PRIMARY KEY, ts timestamptz DEFAULT now(), lane text, host text,
  old_session text, new_session text, new_name text, model text, effort text,
  reason text, context_pct real, handoff_sha text, ack_latency_s int,
  outcome text, signal text CHECK (signal IN ('good','bad','unknown')), note text);

CREATE TABLE IF NOT EXISTS wl.research (
  id bigserial PRIMARY KEY, ts timestamptz DEFAULT now(), vendor text NOT NULL,
  url text NOT NULL, retrieved_at timestamptz, content_sha text NOT NULL,
  title text, excerpt text, proposal_sha text, proposal text, target_surface text,
  status text NOT NULL DEFAULT 'proposed'
    CHECK (status IN ('fetched','unchanged','proposed','accepted','rejected','deferred','duplicate','auto_applied')),
  decided_by text, decided_at timestamptz, reason text,
  UNIQUE (url, content_sha, proposal_sha));

INSERT INTO wl.schema_version (v, note)
  VALUES (1, 'wl base + temporal facts')
  ON CONFLICT DO NOTHING;
