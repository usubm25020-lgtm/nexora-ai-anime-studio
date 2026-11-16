
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y \
    fonts-dejavu-core \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-5000} --workers 1 --threads 4 --timeout 300"]
