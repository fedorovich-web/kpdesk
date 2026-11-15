# syntax=docker/dockerfile:1
FROM node:24-bookworm-slim@sha256:dc26ae92c9fb15cf6b1d63e20f0944b2efe0f115b20e00be48fb23fceb061c54 AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.14-alpine3.24@sha256:5862d971d1f0ae26b258cefa4e893556b1d3aba81bd2ff30888704ad378bab67 AS dependencies
WORKDIR /build/backend
RUN apk add --no-cache g++=15.2.0-r5 \
    && python -m pip install --no-cache-dir uv==0.11.23
COPY backend/pyproject.toml backend/uv.lock ./
COPY backend/src/ ./src/
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
RUN uv sync --locked --no-dev --no-editable --no-python-downloads --config-settings-package scikit-learn:compile-args=-j2

FROM python:3.14-alpine3.24@sha256:5862d971d1f0ae26b258cefa4e893556b1d3aba81bd2ff30888704ad378bab67 AS runtime
RUN python -c "import platform, urllib.request; urllib.request.urlretrieve('https://dl-cdn.alpinelinux.org/alpine/edge/main/' + platform.machine() + '/sqlite-libs-3.54.1-r0.apk', '/tmp/sqlite-libs.apk')" \
    && apk add --no-cache ca-certificates=20261020-r0 font-dejavu=2.37-r6 \
        libstdc++=15.2.0-r5 libgomp=15.2.0-r5 zlib=1.4-r0 \
        /tmp/sqlite-libs.apk \
    && rm /tmp/sqlite-libs.apk \
    && addgroup -g 10001 kpdesk && adduser -u 10001 -G kpdesk -D -H kpdesk
RUN python -m pip uninstall --yes pip
COPY --from=dependencies /opt/venv /opt/venv
COPY --from=frontend /build/frontend/dist/ /app/frontend/dist/
# The application never needs executable-based user/group privilege changes.
RUN find / -xdev -type f -perm /6000 -exec chmod a-s {} +
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    KP_DESK_FRONTEND_DIR=/app/frontend/dist \
    KP_DESK_PDF_FONT_REGULAR=/usr/share/fonts/dejavu/DejaVuSans.ttf \
    KP_DESK_PDF_FONT_BOLD=/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf
WORKDIR /app
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "kpdesk.container_entrypoint", "--healthcheck"]
CMD ["python", "-m", "kpdesk.container_entrypoint"]

# Operator image has migration source; the app never receives owner credentials.
FROM runtime AS provisioner
COPY backend/migrations/versions/ /app/operations/migrations/versions/
COPY infra/production/alembic/ /app/operations/alembic/
COPY infra/production/migrations.sha256.json /app/operations/migrations.sha256.json
COPY backend/scripts/__init__.py backend/scripts/provision_production.py backend/scripts/upgrade_billing.py /app/operations/scripts/
ENV PYTHONPATH=/app/operations
CMD ["python", "-m", "scripts.provision_production", "--new-instance-or-verify"]

FROM runtime AS application
COPY backend/scripts/__init__.py backend/scripts/provision_production.py backend/scripts/start_production.py backend/scripts/seed_production_secrets.py backend/scripts/reconcile_lava_reversal.py /app/operations/scripts/
ENV PYTHONPATH=/app/operations
CMD ["python", "-m", "scripts.start_production"]
