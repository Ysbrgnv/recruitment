FROM python:3.12-slim-bookworm AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 LOG_DIR=/tmp/logs LOG_TO_FILE=false
WORKDIR /app
COPY requirements*.txt ./
RUN pip install --no-cache-dir -r requirements-server.txt && useradd -u 10001 -m app
COPY . .
USER 10001:10001
ENTRYPOINT ["python","docker/container_entry.py"]
CMD ["web"]
FROM app AS test
USER root
RUN pip install --no-cache-dir -r requirements-test.txt
USER 10001:10001
FROM app AS maintenance
USER root
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
 && mkdir -p /usr/share/postgresql-common/pgdg \
 && curl --fail --silent --show-error https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update && apt-get install -y --no-install-recommends postgresql-client-16 \
 && rm -rf /var/lib/apt/lists/*
USER 10001:10001



