FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    DATA_DIR=/data \
    SHARE_DIR=/share

# curl + ssh/scp are handy inside user deploy scripts
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /data /share \
    && chmod 0777 /data /share

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY examples ./examples

# The compose file runs the container as PUID:PGID (matching the Samba share).
USER 1000:1000
VOLUME ["/data"]

HEALTHCHECK --interval=5m --timeout=15s --start-period=10m --retries=2 \
    CMD ["python", "-m", "app", "health"]

ENTRYPOINT ["python", "-m", "app"]
CMD ["run"]
