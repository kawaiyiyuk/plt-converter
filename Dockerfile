FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=80

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.py .
COPY worker.py .

EXPOSE 80

CMD ["gunicorn", "--bind", "0.0.0.0:80", "--workers", "1", "--threads", "2", "--timeout", "30", "--access-logfile", "-", "--error-logfile", "-", "run:app"]
