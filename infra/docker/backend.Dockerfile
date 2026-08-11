FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        fonts-wqy-zenhei \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 docweave \
    && useradd --uid 1000 --gid docweave --home-dir /app --shell /usr/sbin/nologin docweave

COPY backend/requirements.txt ./requirements.txt
RUN pip install \
        --no-cache-dir \
        --disable-pip-version-check \
        --default-timeout 120 \
        --retries 5 \
        -r requirements.txt

COPY --chown=docweave:docweave backend/app ./app

USER docweave

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
