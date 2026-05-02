# syntax=docker/dockerfile:1.7

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
# Install third-party dependencies before app source so code edits do not
# invalidate the expensive dependency download layer.
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

COPY pyproject.toml README.md ./
COPY config ./config
COPY src ./src

# Register the local package without reinstalling third-party dependencies.
RUN --mount=type=cache,target=/root/.cache/pip pip install --no-deps --no-build-isolation -e .

ENV BUG_DATABASE_URL="sqlite:////app/data/buyer_universe.db"
ENV BUG_CACHE_DIR="/app/data/cache"

EXPOSE 8000

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
