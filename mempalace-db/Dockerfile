FROM apache/age:release_PG16_1.6.0
# The base tag hasn't been rebuilt since 2025-09 (AGE has no newer PG16 release), so pull in Debian security
# updates and the latest PostgreSQL 16 minor (on-disk compatible within 16.x) at build time. Rebuild the image
# periodically; `docker compose pull` alone never refreshes it.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends postgresql-16-pgvector \
 && rm -rf /var/lib/apt/lists/*
