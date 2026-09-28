FROM python:3.12-slim-bookworm

# tzdata is required: ZoneInfo("Asia/Jakarta") reads /usr/share/zoneinfo, and the
# slim base image does not ship a timezone database.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so code edits do not invalidate the pip layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY feeds.json ./

# Run as a non-root user. APP_UID must match the uid of the host user that owns
# ./data, otherwise the bind mount is not writable inside the container.
# docker-compose.yml passes the host uid via build args and `user:`.
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --gid "${APP_GID}" appuser \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/data \
    && chown -R "${APP_UID}:${APP_GID}" /app
USER appuser

# The image has no exposed port: the bot uses long polling, so nothing needs to
# be reachable from the internet.
CMD ["python", "-m", "app.main"]
