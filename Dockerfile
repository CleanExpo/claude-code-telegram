FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Poetry
RUN pip install poetry==1.8.3

# Copy dependency files
COPY pyproject.toml poetry.lock ./

# Install dependencies (no dev deps, no virtualenv)
RUN poetry config virtualenvs.create false \
    && poetry install --only main --no-interaction --no-ansi

# Copy source
COPY . .

# Create workspace and data directories
RUN mkdir -p /app/workspace /app/data

ENV APPROVED_DIRECTORY=/app/workspace
ENV DATABASE_URL=sqlite:////app/data/bot.db

ENTRYPOINT ["python", "-m", "src.main"]
