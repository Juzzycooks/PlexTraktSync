FROM python:3.12-alpine

# Upgrade all Alpine packages to patch CVEs, add build deps and su-exec for entrypoint
RUN apk upgrade --no-cache && \
    apk add --no-cache gcc musl-dev libffi-dev openssl-dev su-exec

# Non-root user
RUN addgroup -S appuser && adduser -S -G appuser -h /app -s /sbin/nologin appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Remove build deps to shrink image
RUN apk del gcc musl-dev libffi-dev openssl-dev

COPY . .
RUN chmod +x entrypoint.sh

RUN mkdir -p /config && chown appuser:appuser /config

ENV CONFIG_DIR=/config
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:5000/', timeout=5)" || exit 1

# Start as root, entrypoint fixes /config ownership then drops to appuser
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "--access-logfile", "-", "app:app"]
