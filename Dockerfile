FROM python:3.12-slim
RUN useradd --create-home --uid 10001 app
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV APP_DATA_DIR=/data PYTHONUNBUFFERED=1
RUN mkdir -p /data && chown app:app /data
USER app
EXPOSE 8000
# One process, because the background worker runs inside it. Render sets PORT (default 10000).
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips '*'"]
