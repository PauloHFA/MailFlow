FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations

RUN pip install --no-cache-dir .

RUN groupadd --system mailflow \
    && useradd --system --gid mailflow --create-home --home-dir /home/mailflow mailflow \
    && chown -R mailflow:mailflow /app

USER mailflow

EXPOSE 8000

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
