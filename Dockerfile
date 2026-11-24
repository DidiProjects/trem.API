FROM python:3.14-slim

WORKDIR /app

LABEL app=trem-api
LABEL project=trem
LABEL maintainer=diego
LABEL version=1.0

# Runtime libraries only: PyMuPDF's wheel bundles MuPDF, and cairosvg loads libcairo2.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# CPU-only torch before the requirements, so openai-whisper finds it installed. From PyPI it
# comes with the CUDA libraries (cudnn, cublas, triton...) for a server with no NVIDIA GPU.
# Same version as the test job in deploy.yml.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --timeout 120 --retries 10 --index-url https://download.pytorch.org/whl/cpu torch==2.15.0

COPY requirements.txt .

# Sem --no-cache-dir: ele anulava o cache mount acima, refazendo todo o download
# a cada build. --timeout/--retries cobrem links lentos (torch sozinho são 122 MB).
# openai-whisper requires triton on Linux, but only calls it on CUDA tensors (timing.py):
# on CPU it is ~1 GB never imported, removed in the same layer so it leaves the image.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --timeout 120 --retries 10 -r requirements.txt \
    && pip uninstall -y triton

COPY . .

RUN mkdir -p /tmp/uploads

EXPOSE 3002

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:3002/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3002"]
