FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# On Cloud Run the memory bank lives in Firestore (STATE_BACKEND=auto detects
# it), so nothing here needs to survive a revision. The directory exists for
# local runs of the same image.
RUN mkdir -p /app/data

EXPOSE 8080

# JSON form so uvicorn is PID 1 and receives SIGTERM directly: Cloud Run gives a
# container 10 seconds to shut down, and a shell wrapper swallows the signal.
# A small entrypoint resolves $PORT, which Cloud Run injects at runtime.
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
