FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
# RA-1442: cmake + build-essential + libopenblas added for optional face-recognition
# extra (dlib compiles from source). Kept lightweight — only installs if
# ENABLE_FACE_AUTH=1 at build time.
ARG ENABLE_FACE_AUTH=0
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && if [ "$ENABLE_FACE_AUTH" = "1" ]; then \
         apt-get install -y --no-install-recommends \
           cmake \
           build-essential \
           libopenblas-dev \
           liblapack-dev \
           libx11-dev \
           libgtk-3-dev \
           python3-dev \
           ; \
       fi \
    && rm -rf /var/lib/apt/lists/*

# Install Poetry — version 2.x required because pyproject.toml uses
# package-mode=false (Poetry 2.x feature) and poetry.lock is 2.x format.
# RA-1101: pinned to a specific 2.x to avoid silent upgrades.
RUN pip install poetry==2.3.4

# Copy dependency files
COPY pyproject.toml poetry.lock ./

# Install dependencies (no dev deps, no virtualenv).
# RA-1442: face extra pulls face-recognition + dlib (compiled from source).
# Set ENABLE_FACE_AUTH=1 at build time to include it. Default off to keep
# image small and build fast.
RUN poetry config virtualenvs.create false \
    && if [ "$ENABLE_FACE_AUTH" = "1" ]; then \
         poetry install --only main --extras face --no-interaction --no-ansi; \
       else \
         poetry install --only main --no-interaction --no-ansi; \
       fi

# Copy source
COPY . .

# Create workspace and data directories
RUN mkdir -p /app/workspace /app/data

ENV APPROVED_DIRECTORY=/app/workspace
ENV DATABASE_URL=sqlite:////app/data/bot.db

ENTRYPOINT ["python", "-m", "src.main"]
