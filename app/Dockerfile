FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY etl.py server.py ./
EXPOSE 8000
CMD ["python", "server.py"]
