# Dockerfile
FROM python:alpine3.13

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apk update && apk add --no-cache ca-certificates

# Abhängigkeitsdateien zuerst kopieren (besseres Layer-Caching)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App-Code
COPY . .

# Uvicorn auf allen Interfaces binden
EXPOSE 8000
CMD ["uvicorn", "proxy:app", "--host", "0.0.0.0", "--port", "8000"]
