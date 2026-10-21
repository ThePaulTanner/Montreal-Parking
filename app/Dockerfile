FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY etl.py server.py hash_password.py ./
COPY parking ./parking
COPY static ./static
EXPOSE 8000
# One worker only: the daily data refresh runs as a thread inside this process.
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
