FROM python:3.11-slim

ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=80

WORKDIR /app

COPY requirements.txt .
RUN pip install \
    --no-cache-dir \
    --index-url "${PIP_INDEX_URL}" \
    --timeout 60 \
    --retries 10 \
    -r requirements.txt

COPY app ./app
COPY run.py .
COPY worker.py .

EXPOSE 80

CMD ["gunicorn", "--bind", "0.0.0.0:80", "--workers", "1", "--threads", "2", "--timeout", "30", "--access-logfile", "-", "--error-logfile", "-", "run:app"]
