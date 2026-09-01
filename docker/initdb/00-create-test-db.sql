-- Disposable test database for the pytest suite.
--
-- tests/conftest.py rebuilds this schema from the Alembic migrations on every
-- test run, so it never needs manual care. These scripts only run when the
-- Postgres volume is initialized for the FIRST time; on existing volumes run
-- the equivalent command manually:
--   docker compose exec -T db psql -U ferreteria -d ferreteria \
--     -c 'CREATE DATABASE ferreteria_test OWNER ferreteria;'
CREATE DATABASE ferreteria_test OWNER ferreteria;

\connect ferreteria_test
CREATE EXTENSION IF NOT EXISTS vector;
