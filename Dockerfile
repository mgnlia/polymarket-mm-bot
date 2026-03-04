FROM python:3.11-slim

WORKDIR /app

# Install uv
RUN pip install uv

# Copy dependency files
COPY pyproject.toml .
COPY .env.example .

# Install dependencies
RUN uv pip install --system -e .

# Copy source
COPY *.py ./

# Expose API port
EXPOSE 8000

# Default: run the API server (which starts the bot internally)
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
