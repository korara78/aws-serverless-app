# Guide 1: Project Overview and Architecture

**Last updated:** 2026-08-03

## What this project is

A simulated brokerage-style Bitcoin + fiat ledger — API Gateway → Lambda →
DynamoDB, built with AWS SAM. It started as a deliberately trivial `Items`
CRUD API whose only job was to prove the pipeline around it worked; the
business logic layer was later replaced with a real ledger (fiat balance,
BTC balance, buy/sell against a live price, full transaction integrity)
while keeping the exact same SAM + Lambda + DynamoDB + API Gateway stack
and CI/CD pipeline. What the project demonstrates is both things at once: a
complete, working path from git push to verified live deployment, *and*
correctness properties (idempotency, atomicity, no negative balances under
real concurrency) that only matter once the application underneath is
actually worth getting right.

## Why this project

Three things are easy to claim and hard to actually prove in a portfolio
project: that the tests are real (not testing a mock of a mock), that the
deploy is real (not "should work"), and that the business logic is
correct under conditions that are annoying to set up (real concurrency,
real external dependencies, real partial-failure scenarios). This project
is built to make all three checkable, not just asserted:

- **Real tests, not just mocks.** Unit tests use `moto` (fully mocked,
  fast), but integration tests run against a real `amazon/dynamodb-local`
  container over a real network connection — genuine DynamoDB engine
  behavior, not a Python dict pretending to be one. One integration test
  fires two real, simultaneously-submitted `BUY` requests at the same
  account and confirms the atomic conditional write actually prevents an
  overdraft — something a single-threaded mock can't meaningfully test at
  all (see [Guide 3](03-testing-strategy.md)).
- **Real deploy, not just a green checkmark.** The pipeline's last step is a
  smoke test that hits the actual, live, just-deployed API Gateway endpoint
  over HTTPS — not the local stand-in.
- **Real safety, not just "it worked once."** Deploys authenticate via a
  GitHub OIDC-trusted IAM role scoped to this repo's `main` branch — no AWS
  access keys stored in GitHub at all, and the role's own permissions are
  scoped to this app's resource names rather than broad account access.

## Architecture

```
 GitHub Actions
       |
       v
 Docker / SAM CLI ---- sam local invoke against a Dockerized DynamoDB
       |                (proves the packaged Lambda artifact actually runs,
       |                 not just that the source code imports cleanly)
       v
 Pytest suite --------- unit (moto-mocked) + integration (real DynamoDB Local)
       |
       | deploy only if all of the above passes
       v
 Serverless Backend
 (API Gateway -> Lambda -> DynamoDB)
       |
       | hits the live, deployed endpoint
       v
 Post-deploy smoke test
```

Each box maps directly to a file in this repo:

| Box | File(s) |
|---|---|
| GitHub Actions | `.github/workflows/ci-cd.yml` |
| Docker / SAM CLI local invoke | `Makefile`'s `test-local-invoke` target, `events/get-account.json`, `env.local-invoke.json` |
| Pytest suite | `tests/unit/`, `tests/integration/` |
| Serverless Backend | `template.yaml`, `src/app.py`, `frontend/index.html` |
| Post-deploy smoke test | `tests/smoke/test_smoke.py` |

## What the API actually does

![Run-time flow for a Bitcoin purchase: a user buying BTC sends a single request through API Gateway to a Lambda function, which checks balance, checks for a duplicate transaction, and atomically updates DynamoDB. A response routes back confirming the trade or explaining why it didn't go through — one request in, one response out. Nothing else in the system reacts afterward; there's no separate process watching for database changes once the write happens.](../guides-assets/btc-purchase-runtime-flow.png)

Worth contrasting deliberately with the build-time pipeline diagram above:
that one is a multi-stage pipeline that only runs on a code push. This is
the opposite — a single Lambda execution, triggered by a user action, no
pipeline involved at all. The two diagrams answer two different questions
("what happens when code changes" vs. "what happens when a user trades"),
and conflating them is an easy way to misdescribe how a serverless API
actually behaves.

Six routes, one Lambda function, two DynamoDB tables (`Accounts`, and an
append-only `Transactions` table with a `account_id`+`executed_at` GSI for
per-account history queries):

| Method | Path | Behavior |
|---|---|---|
| `POST` | `/accounts` | Create an account — writes a seeded `Accounts` row and a synthetic `SEED` `Transactions` row atomically |
| `GET` | `/accounts/{id}` | Current cached balances, `404` if unknown |
| `GET` | `/accounts/{id}/balance/verify` | Recompute the balance from the full transaction log and compare to the cache |
| `POST` | `/accounts/{id}/transactions` | Submit a `BUY`/`SELL`; idempotent on client-generated `transaction_id`, atomic against the balance update |
| `GET` | `/accounts/{id}/transactions` | Full transaction history, chronological |
| `GET` | `/price` | Current BTC/USD price, proxied and short-TTL-cached from Coinbase |

The full business-rule reasoning (idempotency via conditional writes,
atomicity via `TransactWriteItems`, why a `SEED` ledger row exists instead
of an untracked starting-balance field, why two REJECTED_* statuses behave
differently under the hood) lives as comments directly in `src/app.py` —
it's short enough to read directly rather than needing a separate
walkthrough.

## The dashboard (frontend)

`frontend/index.html` — a single, self-contained static page (no build
step, no framework) served via CloudFront from a private S3 bucket
(`FrontendBucket`/`FrontendDistribution` in `template.yaml`), same AWS
account and same deploy pipeline as the API, not a separate hosting
service. CloudFront reaches the bucket via Origin Access Control (OAC),
not the legacy "S3 static website hosting" mode, so the bucket itself
stays fully private — `FrontendBucketPolicy` grants read access only to
requests actually coming from this specific distribution.

**"Simulated login," concretely:** there's no password, no server-side
session, and no auth endpoint. Loading the page with no `account_id` in
`localStorage` shows a "Create demo account" button, which just calls
`POST /accounts` — a real API call creating a real `Accounts` row — and
stores the returned `account_id` client-side. Returning later with that ID
still in `localStorage` skips straight to the dashboard. This satisfies the
spec's own scope boundary directly: no real user auth/security is built,
because there's no real money or real user data to protect.

**CORS:** the dashboard and the API are different origins (CloudFront
domain vs. API Gateway domain), so cross-origin `fetch()` calls need the
API to explicitly allow it — `Globals.Api.Cors.AllowOrigin` in
`template.yaml` is scoped to the exact CloudFront domain via
`!GetAtt FrontendDistribution.DomainName`, not `"*"`.

**Design:** paper/ink/bottle-green/brick color palette, Space Grotesk for
UI text, JetBrains Mono for every numeric value (prices, balances,
transaction amounts) — deliberately distinguishing "a number that means
money" from ordinary UI text at a glance.

## Guide series

1. **Project overview and architecture** (this guide)
2. [Environment setup](02-environment-setup.md)
3. [Testing strategy](03-testing-strategy.md)
4. [CI/CD pipeline](04-cicd-pipeline.md)
5. [GitHub OIDC deploy setup](05-github-oidc-deploy-setup.md)
6. [Troubleshooting log](06-troubleshooting-log.md)
