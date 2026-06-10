FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml .
RUN uv pip install --system --no-cache .

COPY . .

# Import worker/main.py to start the WS listener thread before Celery accepts tasks
CMD ["celery", "-A", "worker.celery_app.celery_app", "worker", \
     "--loglevel=info", "--queues=trades,ai,periodic", \
     "--import=worker.main"]
