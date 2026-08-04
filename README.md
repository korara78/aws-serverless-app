# AWS Serverless Bitcoin + Fiat Ledger API

[![CI/CD](https://github.com/korara78/aws-serverless-app/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/korara78/aws-serverless-app/actions/workflows/ci-cd.yml)

A simulated brokerage-style ledger (API Gateway → Lambda → DynamoDB), built with
AWS SAM: fiat + BTC balances, buy/sell transactions against a live Coinbase price,
and real ledger integrity — idempotent writes, atomic balance updates, no negative
balances under concurrency, an append-only audit trail, and an endpoint that
independently reconciles the cached balance against the transaction log itself.
Same CI/CD pipeline this project started with (build → local container smoke test
→ automated tests → deploy → a real smoke test against the live endpoint), with
the business logic layer replaced.

**Live endpoint:** `https://ect844i8lg.execute-api.us-east-1.amazonaws.com/Prod/`

```bash
curl -X POST https://ect844i8lg.execute-api.us-east-1.amazonaws.com/Prod/accounts
# {"account_id": "...", "usd_balance": "31842.50", "btc_balance": "1.13...", ...}

curl -X POST https://ect844i8lg.execute-api.us-east-1.amazonaws.com/Prod/accounts/<id>/transactions \
  -d '{"transaction_id": "<client-generated-uuid>", "type": "BUY", "usd_amount": "500"}'
# {"status": "EXECUTED", "btc_amount": "0.0079...", "btc_price_at_execution": "62876.95", ...}
```

This redeploys on every push to `main` — what's live is whatever `main` currently
says it should be, not a stale snapshot.

## Pipeline

![Build-time pipeline with pytest execution detail: a code push triggers GitHub Actions, which spins up a temporary, disposable runner. Inside it, the pytest suite runs unit tests against mocked DynamoDB and integration tests against a throwaway Docker DynamoDB Local container, both destroyed when the run ends. Only after pytest passes does deploy happen, touching the real, separate deployed infrastructure (API Gateway → Lambda → DynamoDB), followed by a post-deploy smoke test.](guides-assets/build-time-pipeline-pytest-detail.png)

Deploys authenticate via a GitHub OIDC-trusted IAM role (`bootstrap/github-oidc.yaml`)
scoped to this repo's `main` branch — no AWS access keys stored anywhere.

## The ledger

![Run-time flow for a Bitcoin purchase: a user buying BTC sends a single request through API Gateway to a Lambda function, which checks balance, checks for a duplicate transaction, and atomically updates DynamoDB. A response routes back confirming the trade or explaining why it didn't go through — one request in, one response out, no pipeline involved.](guides-assets/btc-purchase-runtime-flow.png)

**Data model:** an `Accounts` table (cached `usd_balance`/`btc_balance`, for fast
reads) and an append-only `Transactions` table (the actual source of truth — see
"Core business rules" below). Account creation writes both atomically: an
`Accounts` row plus a synthetic `SEED` transaction representing the starting
balance as a ledger entry, not an untracked field — so there is exactly one place
balances can ever be reconstructed from.

**Endpoints:**

```
POST /accounts                              create an account (seeded balances + a SEED ledger row, atomically)
GET  /accounts/{account_id}                 current cached balances
GET  /accounts/{account_id}/balance/verify  recompute balance from transaction history, report match/mismatch
POST /accounts/{account_id}/transactions    submit a buy or sell (idempotent on client-generated transaction_id)
GET  /accounts/{account_id}/transactions    full transaction history
GET  /price                                 current BTC/USD price (cached proxy to Coinbase)
```

**Core business rules:**

- **Idempotency.** Every transaction request carries a client-generated
  `transaction_id`; the ledger write is conditioned on
  `attribute_not_exists(transaction_id)`. A retried/duplicate request is rejected,
  never re-executed.
- **No negative balances under concurrency.** The funds check and the balance
  update are the same atomic `TransactWriteItems` call, not a separate
  read-then-write — two simultaneous requests can't both pass the check and
  overdraw the account. Proven, not just asserted: `tests/integration` fires two
  real concurrent `BUY` requests against real DynamoDB Local and confirms exactly
  one succeeds.
- **Atomicity.** The ledger row and the balance update land together or not at
  all — a partial write (transaction logged but balance unchanged, or the
  reverse) is exactly what this design prevents.
- **Reconciliation.** `GET .../balance/verify` independently sums the entire
  transaction log (starting from the `SEED` row) and compares it to the cached
  balance — the cache is a convenience, the ledger is the source of truth.
- **Append-only.** No endpoint updates or deletes a `Transactions` row, ever.

## Repo layout

```
├── template.yaml              SAM template: Accounts + Transactions tables (GSI
│                               on account_id), Lambda, API Gateway routes
├── src/app.py                 ledger handler (accounts, transactions, price feed)
├── tests/
│   ├── unit/                  moto-mocked DynamoDB, no Docker, sub-second —
│   │                           idempotency, atomicity, insufficient-funds,
│   │                           reconciliation, price-feed failure handling
│   ├── integration/           real amazon/dynamodb-local — full lifecycle,
│   │                           duplicate-transaction, and a real concurrent-
│   │                           requests race against actual DynamoDB
│   └── smoke/                 hits the live, deployed API Gateway endpoint
├── events/, env.local-invoke.json   inputs for the sam local invoke smoke check
├── Makefile                   single source of truth for every command below,
│                               shared by local dev and CI so they can't drift
├── bootstrap/github-oidc.yaml  one-time OIDC provider + scoped deploy role
├── guides/                     the project journey, step by step (see below)
└── .github/workflows/ci-cd.yml
```

## Running it locally

```bash
pip install aws-sam-cli
pip install -r requirements-dev.txt

make test-unit          # fast, mocked, no Docker
make test-integration   # real DynamoDB Local in Docker
make test-local-invoke  # sam local invoke through the actual Lambda/Docker runtime
make deploy              # requires your own AWS credentials + samconfig.toml
```

## What actually broke

Two separate rounds, for two separate reasons — the original CRUD scaffold's
*deploy pipeline* (OIDC/IAM), and this project's *ledger migration* (local
dev/testing tooling). Both are real issues, found by reading actual logs/events
rather than guessing, documented in full in
[Guide 6](guides/06-troubleshooting-log.md):

- **Deploy pipeline (4 issues):** missing `id-token: write`, GitHub's OIDC `sub`
  claim embedding immutable org/repo IDs, a missing permission on the Serverless
  transform, missing S3 tagging/delete permissions on SAM's own bootstrap bucket.
- **Ledger migration (3 issues):** a `boto3.resource().meta.client` footgun that
  silently corrupts `TransactWriteItems` payloads, `sam local invoke --env-vars`
  only being able to override environment variables already declared in the
  template, and DynamoDB Local partitioning data by AWS access key (so the smoke
  check's manually-created table and the invoked Lambda's own default credentials
  were silently looking at two different, empty databases).

## Guides

The `guides/` folder documents the project end to end, as six sequential
guides:

1. [Project overview and architecture](guides/01-project-overview.md)
2. [Environment setup](guides/02-environment-setup.md)
3. [Testing strategy](guides/03-testing-strategy.md) — why four different test layers exist and what each one actually proves
4. [CI/CD pipeline](guides/04-cicd-pipeline.md)
5. [GitHub OIDC deploy setup](guides/05-github-oidc-deploy-setup.md) — how to reproduce the OIDC bootstrap in a new AWS account
6. [Troubleshooting log](guides/06-troubleshooting-log.md) — every real issue hit, with cause and fix
