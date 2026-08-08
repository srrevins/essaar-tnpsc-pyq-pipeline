FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=7860

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-tam \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt pipeline.py app.py ./
COPY config ./config
COPY data ./data
COPY dataset ./dataset

RUN pip install --no-cache-dir --requirement requirements.txt

EXPOSE 7860
CMD ["python", "app.py"]
