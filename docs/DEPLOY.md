# Deploying IssuerGraph

One FastAPI process serves everything: the landing page, the demo, the pilot
form and — optionally — the live product. There is no separate frontend build,
no bundler and no node runtime; the static files are served as written.

## The two deployment shapes

**Public site (recommended for anything internet-facing).** Set
`ISSUERGRAPH_DEMO_ONLY=1`. The process then serves `/`, `/demo` and `/pilot`,
and returns 404 for `/app`. The demo reads `static/demo/snapshot.json` and the
page images beside it, so the pipeline, the PDFs and the document database are
not deployed at all. The only database this shape touches is the one holding
pilot requests.

**Full workspace.** Leave `ISSUERGRAPH_DEMO_ONLY` unset and point
`ISSUERGRAPH_DSN` at the pipeline's database. `/app` then serves the live
product over real data, including PDF downloads and server-rendered page
highlighting.

Both shapes need a database for the pilot form. It can be a small one: only
`pilot_request` and `pilot_submission_log` are used.

## Run it

```bash
uv venv -p 3.13 .venv
uv pip install --python .venv/bin/python fastapi 'uvicorn[standard]' 'psycopg[binary]' \
    pydantic pymupdf python-dateutil pytest

createdb issuergraph
psql -d issuergraph -f sql/schema.sql          # includes every migration

cp .env.example .env                           # then edit; every value is optional
set -a && . ./.env && set +a

.venv/bin/uvicorn issuergraph.api:app --host 0.0.0.0 --port 8080
```

For a demo-only host, the schema can be just the pilot tables:

```bash
psql -d issuergraph -f sql/006_pilot_requests.sql
```

## Behind a proxy

Run uvicorn with `--proxy-headers --forwarded-allow-ips='*'` so the rate limiter
sees the real client address. It hashes that address with
`ISSUERGRAPH_RATE_SALT` and stores only the hash; set the salt to any random
string in production so hashes are not comparable across deployments.

Terminate TLS at the proxy. Nothing in the app requires sticky sessions,
websockets or background workers, so any number of processes can run behind a
load balancer.

## Refreshing the demo

The snapshot is committed, so a deployment never builds it. Re-export it from a
machine that has the pipeline database:

```bash
.venv/bin/python -m issuergraph.pipeline      # if the data has changed
.venv/bin/python -m scripts.export_demo       # writes snapshot.json + page images
.venv/bin/python -m pytest tests/test_site.py -q
git add static/demo && git commit
```

The exporter verifies every anchor against the live page text before writing, so
a snapshot that would display evidence it cannot prove fails the export rather
than reaching the site. `git diff --stat static/demo` shows exactly what changed
about the demo.

## Static assets and caching

`/static/*` and `/demo/pages/*` are plain files and can be fronted by a CDN. The
page images change only when the snapshot is re-exported. The HTML pages are
served with `Cache-Control: no-cache` so a redeploy is visible immediately.

## What to watch

- `pilot_request` — new rows are the point of the site.
- 503s from `POST /api/pilot` — the form is telling visitors that storage is
  down, which is correct behaviour and an alert-worthy event.
- 429s from the same route — throttling, expected in small numbers.
- `GET /api/demo/overview` failing with 503 means the snapshot file is missing
  from the deployed image.

## Backups

The pilot database is the only state a public deployment creates. The snapshot,
the schema and everything else live in git.

## AWS (the current public deployment)

Account 173639292018, region ap-south-1, CLI profile `issuergraph-deploy`. Two
CloudFormation stacks under `infra/`:

- `issuergraph-ecr` — the image repository. Separate because App Runner cannot
  create a service until an image exists.
- `issuergraph-site` — VPC with two private subnets, RDS Postgres
  (`issuergraph-pilot`, db.t4g.micro, deletion-protected, 7-day backups),
  three Secrets Manager secrets (`issuergraph/db-credentials`,
  `issuergraph/dsn`, `issuergraph/rate-salt`, all generated — nobody types a
  password), and the App Runner service `issuergraph-site` reading the DSN and
  salt as runtime secrets. Health check is `GET /healthz`.

The database is private; the container applies `sql/006_pilot_requests.sql`
itself at startup (`scripts/migrate.py`, idempotent, non-fatal) because nothing
else can reach it.

Release a new build:

```bash
export AWS_PROFILE=issuergraph-deploy AWS_REGION=ap-south-1
TAG=$(git rev-parse --short HEAD)
URI=173639292018.dkr.ecr.ap-south-1.amazonaws.com/issuergraph

docker build --platform linux/amd64 -t $URI:$TAG .
aws ecr get-login-password | docker login --username AWS --password-stdin ${URI%%/*}
docker push $URI:$TAG
aws cloudformation deploy --stack-name issuergraph-site --template-file infra/site.yaml \
    --capabilities CAPABILITY_NAMED_IAM --parameter-overrides ImageTag=$TAG
```

Changing the tag is what triggers an App Runner deployment (auto-deploy is
off). Read leads with `aws apprunner` logs or by querying `pilot_request`
from inside the VPC; the instance is not publicly reachable.
