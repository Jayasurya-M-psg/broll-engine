FROM python:3.11-slim-bookworm

# Install system dependencies


RUN sed -i \
    -e 's|https\?://ftp.debian.org/debian-security|https://security.debian.org/debian-security|g' \
    -e 's|https\?://deb.debian.org/debian-security|https://security.debian.org/debian-security|g' \
    -e 's|https\?://deb.debian.org/debian|https://ftp.debian.org/debian|g' \
    /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        git \
        build-essential \
        espeak-ng \
    && rm -rf /var/lib/apt/lists/*


WORKDIR /app

# Install Python dependencies
COPY requirements.txt .

RUN pip install --no-cache-dir "setuptools<81" wheel

# Install CPU-only torch first (avoids 2 GB of CUDA packages — device=cpu in .env)
RUN pip install --no-cache-dir \
    torch==2.3.1+cpu \
    torchaudio==2.3.1+cpu \
    --index-url https://download.pytorch.org/whl/cpu

RUN pip install --no-cache-dir --no-build-isolation -r requirements.txt

# Download spaCy model
RUN python -m spacy download en_core_web_sm || true

# Copy application code
COPY app/ ./app/
COPY batch_runner.py .

# Non-root user for security
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD curl -f http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
