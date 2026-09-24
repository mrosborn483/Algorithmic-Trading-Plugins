FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY papertrader ./papertrader

RUN useradd --create-home trader && mkdir -p /app/data && chown trader /app/data
USER trader

# config.yaml is mounted at runtime; the journal lives in /app/data (a volume)
ENTRYPOINT ["python", "-m", "papertrader", "-c", "/app/config.yaml"]
CMD ["run"]
