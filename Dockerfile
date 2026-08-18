FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY static/ static/

ENV AUTONOMYGATE_STORAGE=dynamo \
    AUTONOMYGATE_AGENT=bedrock \
    PYTHONUNBUFFERED=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import urllib.request,sys; \
      r=urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4); \
      sys.exit(0 if r.status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
