FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY akwarr ./akwarr

RUN pip install --upgrade pip && pip install .

RUN useradd -m -u 1000 akwarr && mkdir -p /config /media/Movie/Arabic /media/Serries/Arabic /media/Download/akwarr-staging && chown -R akwarr:akwarr /config /media

USER akwarr

EXPOSE 7879 8990

CMD ["akwarr-radarr"]
