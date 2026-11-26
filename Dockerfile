FROM python:3.11-slim

# OCR (optional feature), libraries Pillow needs to shape Arabic text, and the fonts used by the page and the PDF report
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr libraqm0 libfribidi0 libharfbuzz0b fonts-dejavu-core fonts-noto-core libreoffice-impress \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 appuser
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py engine.py report.py fixes.py tone.py autofix.py scoring.py textlayer.py ./
COPY templates ./templates
RUN mkdir -p fonts static/fonts \
 && cp /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf fonts/ \
 && (cp $(find /usr/share/fonts -name 'NotoNaskhArabic-Regular.ttf' | head -1) $(find /usr/share/fonts -name 'NotoNaskhArabic-Bold.ttf' | head -1) static/fonts/ || true) \
 \
 && for w in Regular Medium Bold ExtraBold; do cp $(find /usr/share/fonts -name "NotoKufiArabic-$w.ttf" | head -1) static/fonts/ || true; done \
 && cp /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf static/fonts/ \
 && chown -R appuser:appuser /app
USER appuser

ENV DATA_DIR=/tmp/arabic-checker PYTHONUNBUFFERED=1
EXPOSE 7860
# Render sets PORT; Hugging Face and local use 7860
CMD gunicorn -b 0.0.0.0:${PORT:-7860} -w 2 --threads 2 -t 180 app:app
