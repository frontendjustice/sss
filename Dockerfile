FROM python:3.12-slim

# Tesseract reads printed scans for free; handwriting still goes to Gemini.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY quizengine ./quizengine
COPY webapp ./webapp

ENV DATA_DIR=/app/data PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["sh", "-c", "uvicorn webapp.server:app --host 0.0.0.0 --port ${PORT:-8000}"]
