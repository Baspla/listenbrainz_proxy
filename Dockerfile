# Dockerfile
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System-Dependencies nach Bedarf (z.B. ca-certificates)
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*

# Abhängigkeitsdateien zuerst kopieren (besseres Layer-Caching)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App-Code
COPY . .

# Uvicorn auf allen Interfaces binden
EXPOSE 8000
CMD ["uvicorn", "proxy:app", "--host", "0.0.0.0", "--port", "8000"]
