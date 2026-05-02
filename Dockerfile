# PyTorch CUDA runtime base. Provides:
#   - python 3.10
#   - cuda 12.1 + cudnn
#   - torch 2.x with cuda support
# About 3 GB pulled, but model load + matmul on the A4000 is the
# performance unlock that justifies it. CPU-only environments override
# DOCKER_RUNTIME=runc in compose; the same image runs (slower) on CPU.
FROM pytorch/pytorch:2.2.0-cuda12.1-cudnn8-runtime

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System deps for Pillow + curl health probe. The CUDA base image
# includes most of what numpy/torch need.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg-dev zlib1g-dev curl \
    && rm -rf /var/lib/apt/lists/*

# Bridge runtime deps. torch + transformers come from the base image
# wheels (already installed) plus the upgrade pin in pyproject.toml's
# [embedding] extra.
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
    "guessit>=3.8" \
    "transformers>=4.40"

COPY bridge /app/bridge

EXPOSE 13000

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -f http://localhost:13000/health || exit 1

CMD ["uvicorn", "bridge.app.main:app", "--host", "0.0.0.0", "--port", "13000"]
