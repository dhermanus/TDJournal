# TDJournal — single-image build.
#
# Stage 1 builds the CRA bundle; stage 2 runs the API, which also serves that
# bundle (FRONTEND_DIR). One image, one port, one origin — so the browser needs
# no CORS configuration and the auth cookie is same-site by construction.
#
# Local development is unaffected: setup.bat, launch.bat and `npm start` use
# this repository directly and never enter a container.

# ── build the frontend ────────────────────────────────────────────────────────
FROM node:24-bookworm-slim AS frontend

WORKDIR /build

# Only the manifest first: a source change must not invalidate the dependency
# install, which is the slow part.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund --loglevel=error

COPY frontend/ ./

# REACT_APP_API_URL="" bakes a relative API base into the bundle. The API serves
# it from the same origin, so there is no second host to point at.
ENV CI=true \
    REACT_APP_API_URL= \
    GENERATE_SOURCEMAP=false
RUN npm run build


# ── run the API + serve the bundle ────────────────────────────────────────────
FROM python:3.11-slim-bookworm AS runtime

# tzdata is a declared dependency (MT5 broker-server time), and the image must
# never be surprised by a missing IANA database.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./

# The bundle is copied out of stage 1 rather than baked next to the code, so a
# stale build directory in the working tree can never shadow it.
COPY --from=frontend /build/build /opt/tdjournal-frontend
ENV FRONTEND_DIR=/opt/tdjournal-frontend \
    DATABASE_PATH=/data/trading_journal.db \
    UPLOAD_DIR=/data/uploads \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data/uploads /data/backups \
    && adduser --disabled-password --no-create-home --gecos "" tdjournal \
    && chown -R tdjournal:tdjournal /data /opt/tdjournal-frontend

USER tdjournal
WORKDIR /app
EXPOSE 8010

# `/` is not gated by auth (it carries no journal data), so this healthcheck
# works before anyone has signed in.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request as u,sys; sys.exit(0 if u.urlopen('http://127.0.0.1:8010/',timeout=4).status==200 else 1)"

# --reload is a development convenience and assumes a writable tree; production
# runs plain uvicorn with one worker, the same way launch.bat does without reload.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8010"]
