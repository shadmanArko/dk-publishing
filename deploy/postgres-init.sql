-- Runs once, when the Postgres volume is first created. The app database is created by the image
-- (POSTGRES_DB); Dagster keeps its run history in its own database, never in the app's.
CREATE DATABASE dagster;
