# Single stage on slim (glibc): prebuilt wheels of aiohttp / multidict / yarl
# are glibc binaries and must not be copied into an alpine (musl) image.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --progress-bar off -r requirements.txt

COPY . .

CMD ["python", "-m", "app.main"]
