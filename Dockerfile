FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py tool_calling_engine.py token_auto_refresh.py get_token.py bench.py ./

ENV PORT=8000 PYTHONUNBUFFERED=1
EXPOSE 8000

USER nobody

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" || exit 1

CMD ["python", "server.py"]
