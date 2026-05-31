FROM python:3.12-slim

LABEL maintainer="clio"
LABEL description="OBS Studio Prometheus Exporter"

WORKDIR /app

# Install Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy exporter script
COPY obs_exporter.py /app/obs_exporter.py

# Expose metrics port
EXPOSE 9121

# Default env vars (override at runtime)
ENV OBS_HOST=10.50.0.4
ENV OBS_PORT=4455
ENV EXPORTER_PORT=9121
ENV POLL_INTERVAL=10
ENV RECONNECT_DELAY=5

# Password: mount as env var or volume
#   docker run -e OBS_PASSWORD=xxx ...
#   docker run -v /path/to/obs_pc_ws_pass:/home/clio/.openclaw/secrets/obs_pc_ws_pass:ro ...

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:9121/metrics')" || exit 1

ENTRYPOINT ["python3", "-u", "obs_exporter.py"]
