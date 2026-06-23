FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY src /app/src

RUN apt-get update && \
    apt-get install -y --no-install-recommends nodejs fonts-dejavu-core && \
    rm -rf /var/lib/apt/lists/*

RUN python -m pip install --upgrade pip && \
    python -m pip install .

EXPOSE 8000

CMD ["python", "-m", "wb_marks_app"]
