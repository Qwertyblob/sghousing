-- Extensions and schemas. Idempotent: safe to run any number of times.
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE SCHEMA IF NOT EXISTS raw;      -- source data exactly as downloaded, all text
CREATE SCHEMA IF NOT EXISTS staging;  -- cleaned and typed
CREATE SCHEMA IF NOT EXISTS marts;    -- model-ready and site-ready tables
