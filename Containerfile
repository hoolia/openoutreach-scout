# LinkedIn scout: OpenOutreach linkedin_cli at the pinned commit (upstream.env) plus the
# oo_scout adapter, headed Chromium under Xvfb. Runs as any UID in group 0 (restricted-v2).
FROM docker.io/library/python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS build

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /build
# upstream source, checksum-verified; only linkedin_cli and the licence are kept
COPY upstream.env ./
COPY scripts/fetch-upstream.sh scripts/
RUN sh scripts/fetch-upstream.sh /opt/upstream
# dependencies from the hashed lock file, then the adapter without deps
COPY requirements.lock pyproject.toml README.md ./
COPY src src
RUN python3 -m venv /opt/oo-scout \
 && /opt/oo-scout/bin/pip install --no-cache-dir --disable-pip-version-check --require-hashes -r requirements.lock \
 && /opt/oo-scout/bin/pip install --no-cache-dir --disable-pip-version-check --no-deps . \
 && /opt/oo-scout/bin/pip uninstall -y pip

FROM docker.io/library/python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

ARG VERSION=dev
LABEL org.opencontainers.image.title="openoutreach-scout" \
      org.opencontainers.image.description="Read-only LinkedIn scout on OpenOutreach linkedin_cli: human pacing, signals to ClickHouse." \
      org.opencontainers.image.source="https://github.com/hoolia/openoutreach-scout" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="GPL-3.0-only"

COPY --from=build /opt/oo-scout /opt/oo-scout
COPY --from=build /opt/upstream /opt/upstream
ENV PATH=/opt/oo-scout/bin:$PATH \
    PYTHONPATH=/opt/upstream \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
    HOME=/tmp \
    SCOUT_DATA_DIR=/data

# Chromium and its system libraries (the version the playwright wheel pins), Xvfb for a
# headed browser, x11vnc for the one-time human login, tini to reap Chromium children
RUN apt-get update \
 && apt-get install -y --no-install-recommends libatomic1 xvfb x11vnc tini \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /data \
 && chgrp -R 0 /opt/pw-browsers /data \
 && chmod -R g=u /opt/pw-browsers /data \
 && mkdir -p /licenses \
 && cp /opt/upstream/LICENCE.md /licenses/OpenOutreach-LICENCE.md \
 && cp /opt/upstream/COMMIT /licenses/OpenOutreach-COMMIT

COPY LICENSE.md /licenses/openoutreach-scout-LICENSE.md

COPY scripts/entrypoint.sh /usr/local/bin/oo-entrypoint
RUN chmod 0755 /usr/local/bin/oo-entrypoint

USER 1001
VOLUME ["/data"]
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/oo-entrypoint"]
CMD ["run"]
