FROM python:3.12-alpine

# PORT is the port the app listens on inside the container (override at runtime).
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    PORT=5055

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY immich_dates.py .

# Run as an unprivileged user; /data holds one SQLite file per browser profile.
RUN adduser -S -u 10001 app && mkdir /data && chown app /data
USER app
VOLUME /data
EXPOSE ${PORT}

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os, urllib.request as u; u.urlopen('http://127.0.0.1:%s/api/stats' % os.environ['PORT'], timeout=3)"

# One worker only: scan progress is held in memory, so it must be a single process.
# Shell form via sh -c so ${PORT} is expanded at start-up.
CMD ["sh", "-c", "exec gunicorn --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:${PORT} immich_dates:app"]