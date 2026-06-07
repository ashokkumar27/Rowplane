# PGMQ publishes Postgres images with the pgmq extension pre-installed.
# This project layers pgvector and pg_cron on top to match AGENTS.md.
FROM ghcr.io/pgmq/pg17-pgmq:v1.10.0

USER root
RUN apt-get update     && apt-get install -y --no-install-recommends       postgresql-17-cron       postgresql-17-pgvector     && rm -rf /var/lib/apt/lists/*

# pg_cron must be preloaded before CREATE EXTENSION pg_cron succeeds.
RUN echo "shared_preload_libraries = 'pg_cron'" >> /usr/share/postgresql/postgresql.conf.sample     && echo "cron.database_name = 'rowplane'" >> /usr/share/postgresql/postgresql.conf.sample
