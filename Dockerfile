FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY app.py mail.py los_runtime.py los_migrate.py los_worker.py gunicorn.conf.py ./
COPY templates ./templates
COPY static ./static
RUN groupadd --gid 10001 losverkauf && useradd --uid 10001 --gid 10001 --no-create-home losverkauf \
    && mkdir -p /data/maps /data/mails && chown -R 10001:10001 /data && chmod 700 /data /data/maps /data/mails
RUN python -c "import os,json;from pathlib import Path;os.umask(0o077);Path(\"/data/maps/bezirke.geojson\").write_text(json.dumps({\"type\":\"FeatureCollection\",\"features\":[]}))" \
    && chown 10001:10001 /data/maps/bezirke.geojson
USER 10001:10001
EXPOSE 8000
CMD ["gunicorn", "--config", "gunicorn.conf.py", "app:app"]

FROM runtime AS tests
USER 0
COPY requirements-test.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements-test.lock
USER 10001:10001
COPY tests ./tests
CMD ["python", "-m", "pytest", "-q"]

FROM runtime AS final
