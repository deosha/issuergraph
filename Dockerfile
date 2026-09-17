# Public site image. Demo-only by default: no PDFs, no pipeline database.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ISSUERGRAPH_DEMO_ONLY=1
WORKDIR /srv/issuergraph

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY issuergraph ./issuergraph
COPY scripts ./scripts
COPY sql ./sql
COPY static ./static

RUN useradd --system --no-create-home app
USER app
EXPOSE 8080

# Apply the pilot-form schema, then serve. Proxy headers are not trusted by
# uvicorn; the app counts ISSUERGRAPH_TRUSTED_PROXY_HOPS from the right of
# X-Forwarded-For instead, so a caller cannot choose its own rate-limit
# identity (see docs/DEPLOY.md).
CMD ["sh", "-c", "python -m scripts.migrate; exec uvicorn issuergraph.api:app --host 0.0.0.0 --port 8080"]
