FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HERBIE_DB=/data/herbie.db

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY herbie ./herbie
COPY web ./web
COPY tests ./tests

# Fail the build if the acceptance suite doesn't pass.
RUN HERBIE_DB=:memory: python -m pytest -q

RUN useradd --create-home herbie && mkdir -p /data && chown herbie /data
USER herbie
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
CMD ["uvicorn", "herbie.main:app", "--host", "0.0.0.0", "--port", "8000"]
