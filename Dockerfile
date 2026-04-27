FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System deps for Pillow, numpy
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libjpeg-dev zlib1g-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/
RUN pip install --no-cache-dir \
    "fastapi>=0.115" \
    "uvicorn[standard]>=0.32" \
    "httpx>=0.28" \
    "aiosqlite>=0.20" \
    "pydantic>=2.9" \
    "pydantic-settings>=2.6" \
    "rapidfuzz>=3.10" \
    "imagehash>=4.3" \
    "Pillow>=11.0" \
    "numpy>=2.1" \
    "guessit>=3.8"

COPY bridge /app/bridge

EXPOSE 13000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:13000/health || exit 1

CMD ["uvicorn", "bridge.app.main:app", "--host", "0.0.0.0", "--port", "13000"]
