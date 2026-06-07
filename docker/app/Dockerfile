FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1     PYTHONDONTWRITEBYTECODE=1     PYTHONPATH=/app/src

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY db ./db
COPY docs ./docs
COPY examples ./examples

RUN pip install --no-cache-dir -e .

CMD ["python", "-B", "examples/postgres_showcase.py"]
