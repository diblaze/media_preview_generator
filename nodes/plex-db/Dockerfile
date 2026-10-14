# The Plex database node (formerly the Plex marker agent): the app's database half, to run beside Plex.
#
# Build from the REPOSITORY ROOT (it needs the app's source, which is what it runs):
#   docker build -f nodes/plex-db/Dockerfile -t plex-marker-agent:dev .
#
# It runs the node server (media_preview_generator/nodes/server/) with only the Plex database role on. It carries no
# ffmpeg, no GPU drivers and none of the app's media libraries: the app's wheel is installed with --no-deps and only
# the handful of libraries in requirements.lock.txt comes with it.
#
# Both stages pin the base by digest, not by tag: `python:3.12-slim-bookworm` is republished whenever Debian or
# CPython gets a patch, so the tag alone makes two builds of one release tag different images. This digest is the
# multi-arch index, so linux/amd64 and linux/arm64 both still resolve. To move it on:
#   docker pull python:3.12-slim-bookworm && docker image inspect --format '{{index .RepoDigests 0}}' python:3.12-slim-bookworm

FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS builder
WORKDIR /build
ENV PIP_BREAK_SYSTEM_PACKAGES=1 PIP_DISABLE_PIP_VERSION_CHECK=1

# Dependency wheels first: this layer is keyed on the lock alone, so a source edit never rebuilds them.
# requirements.lock.txt is the whole closure at one version each, with hashes; --require-hashes makes pip refuse
# anything it wasn't told to expect, so two builds of one release tag get byte-identical dependencies.
# requirements.txt stays the hand-written input (and the file tests/nodes/test_plex_db_role.py drift-checks against
# the app's pyproject.toml); regenerate the lock from it with:
#   docker run --rm -v "$PWD/nodes/plex-db:/w" -w /w python:3.12-slim-bookworm \
#     sh -c 'pip install pip-tools && pip-compile --generate-hashes --strip-extras --no-header \
#              --output-file=requirements.lock.txt requirements.txt'
COPY nodes/plex-db/requirements.lock.txt ./
RUN pip wheel --wheel-dir=/wheels --no-cache-dir --require-hashes -r requirements.lock.txt

# The app wheel, without its dependencies: the node server's Plex database role imports markers/publishers,
# loudness's database modules and library_health/plex_rows.py only (their imports of the server layer are
# function-local, so none of it loads here).
# setuptools-scm needs a version; CI passes the release's, a local build gets the fallback.
COPY pyproject.toml ./
COPY media_preview_generator/ ./media_preview_generator/
ARG SETUPTOOLS_SCM_PRETEND_VERSION="0.0.0.dev0"
RUN SETUPTOOLS_SCM_PRETEND_VERSION_FOR_MEDIA_PREVIEW_GENERATOR="$SETUPTOOLS_SCM_PRETEND_VERSION" \
      pip wheel --wheel-dir=/wheels --no-cache-dir --no-deps .

FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
LABEL org.opencontainers.image.title="Plex database node" \
      org.opencontainers.image.description="Writes intro and credits markers, chapter thumbnails and loudness into Plex's database, and runs Library Health's counts, for Media Preview Generator running on another machine" \
      org.opencontainers.image.source="https://github.com/stevezau/media_preview_generator"
# NODE_PORT is deliberately not set here: the port is ${NODE_PORT:-${AGENT_PORT:-9494}}, so a setup from the Plex
# marker agent days that moved AGENT_PORT keeps listening where its published mapping expects.
ENV PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NODE_ROLES=plex_db \
    CONFIG_DIR=/config \
    PLEX_CONFIG_DIR="/plex/Library/Application Support/Plex Media Server"

COPY --from=builder /wheels /tmp/wheels
# --no-deps: the wheel folder already holds the lock's full closure, and the app wheel's own dependency list
# (ffmpeg-adjacent libraries it never imports here) must not pull anything in. ci.yml's `marker-agent-image` job
# boots this image and calls it, so a new module-scope import on the role's path breaks a PR, not a release.
# /config is the node's own folder (its id, and loudness's undo record): sticky and writable by anyone, because the
# container runs as whichever PUID owns Plex's database, and a fresh named volume copies these permissions.
RUN pip install --no-cache-dir --no-index --no-deps /tmp/wheels/*.whl && rm -rf /tmp/wheels \
    && install -d -m 1777 /config

WORKDIR /app

# Non-root by default. Plex's database is owned by the user Plex runs as, so run the container as that user
# (compose: `user: "${PUID}:${PGID}"`); the image itself needs no privileges of its own.
USER 1000:1000
# The default port only. NODE_PORT (or AGENT_PORT) moves what gunicorn binds, and EXPOSE can't follow an environment
# variable — so a changed port also needs the published mapping changed (docker-compose.yml derives both from it).
EXPOSE 9494

# One worker: the app's "one writer at a time" guarantee is a lock inside one process (see nodes/server/plex_db.py).
# Threads let a read-back and the Edit tab's checks overlap without a second process touching the database.
CMD ["sh", "-c", "exec gunicorn media_preview_generator.nodes.server.wsgi:app --bind 0.0.0.0:${NODE_PORT:-${AGENT_PORT:-9494}} --workers 1 --threads 4 --timeout 180 --access-logfile -"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD exec python3 -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + (os.environ.get('NODE_PORT') or os.environ.get('AGENT_PORT') or '9494') + '/v1/health', timeout=5)"
