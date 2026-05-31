FROM python:3.12-slim

LABEL maintainer="clio" \
      description="Prometheus exporter for Shure QLXD wireless receivers"

WORKDIR /app

COPY requirements-shure.txt requirements.txt
# If built from tarball, requirements.txt is used instead
RUN pip install --no-cache-dir -r requirements.txt

COPY shure_exporter.py .

EXPOSE 9122

USER nobody

ENTRYPOINT ["python", "-u", "shure_exporter.py"]
