FROM python:3.12-slim-bookworm

ARG WATERMARK_EXTRAS=video
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WORK_DIR=/work \
    TMPDIR=/work \
    HF_HOME=/home/app/.cache/huggingface \
    OMP_NUM_THREADS=2 \
    OPENBLAS_NUM_THREADS=2

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home app \
    && mkdir -p /work /app /home/app/.cache/huggingface \
    && chown -R app:app /work /app /home/app/.cache

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
COPY api_service/requirements.txt ./api_service/requirements.txt
RUN python -m pip install ".[${WATERMARK_EXTRAS}]" -r api_service/requirements.txt
COPY api_service/ ./api_service/

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"
CMD ["uvicorn", "api_service.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers"]
