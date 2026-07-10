FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y \
    gcc \
    libffi-dev \
    libcairo2-dev \
    libpango1.0-dev \
    libgdk-pixbuf2.0-dev \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    PORT=8000

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 3. Revert to Debian's 'adduser' syntax.
RUN adduser --system --group app && \
    chown -R app:app /srv

USER app

EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${PORT}/', timeout=5)"

CMD ["python", "./app.py"]