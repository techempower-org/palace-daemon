FROM python:3.12-slim

# curl is needed for HEALTHCHECK only
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Per SECURITY.md: do not run palace-daemon as root. UID/GID 1000 lines up
# with the typical desktop user that owns ~/.mempalace/palace on the host,
# so the bind-mount at /palace is writable without extra chown gymnastics.
# Override at run time with `docker run --user UID:GID` (or PALACE_UID /
# PALACE_GID in docker-compose) if your host user differs.
RUN groupadd --system --gid 1000 palace \
    && useradd --system --uid 1000 --gid palace --home-dir /app --shell /usr/sbin/nologin palace

WORKDIR /app

# Install deps as a separate layer so rebuilds after source changes are fast.
# chromadb ships pre-built wheels for linux/amd64 and linux/arm64 — no
# build-essential needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
# messages.py is present in v1.5.0+ (PR #4); safe to glob so this works on
# main too
COPY *.py ./

# Palace directory — mount your palace here at runtime.
# The palace is never baked into the image; it is always external state.
VOLUME ["/palace"]

ENV PALACE_PATH=/palace \
    PALACE_HOST=0.0.0.0 \
    PALACE_PORT=8085

# /app holds source and is the palace user's HOME (where the lock file
# lives under ~/.cache/palace-daemon). Must be writable by palace.
RUN chown -R palace:palace /app

EXPOSE 8085

USER palace

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:${PALACE_PORT}/health || exit 1

# --manual bypasses the INVOCATION_ID guard that prevents accidental non-systemd starts.
# The PR branch (pre-v1.5.0) does not have this flag yet; remove it if the build fails.
ENTRYPOINT ["python", "main.py", "--manual"]
