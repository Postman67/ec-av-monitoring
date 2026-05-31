FROM python:3.12-slim
WORKDIR /app
COPY requirements-companion.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY companion_exporter.py .
EXPOSE 9123
ENTRYPOINT ["python3", "-u", "companion_exporter.py"]
