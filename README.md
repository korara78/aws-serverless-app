# AWS Serverless CRUD API

[![CI/CD](https://github.com/korara78/aws-serverless-app/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/korara78/aws-serverless-app/actions/workflows/ci-cd.yml)

A small serverless CRUD API (API Gateway → Lambda → DynamoDB), built with AWS SAM,
that exists to demonstrate a complete, working CI/CD pipeline: build → local
container smoke test → automated tests → deploy → a real smoke test against the
live endpoint — with no long-lived AWS credentials anywhere in the pipeline.

**Live endpoint:** `https://ect844i8lg.execute-api.us-east-1.amazonaws.com/Prod/`

```bash
curl -X POST https://ect844i8lg.execute-api.us-east-1.amazonaws.com/Prod/items \
  -d '{"name": "Widget"}'
# {"id": "...", "name": "Widget"}
```

This redeploys on every push to `main` — what's live is whatever `main` currently
says it should be, not a stale snapshot.

## Pipeline

```
 GitHub Actions
       |
       v
 Docker / SAM CLI ---- sam local invoke against a Dockerized DynamoDB
       |                (proves the packaged Lambda artifact actually runs)
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

Deploys authenticate via a GitHub OIDC-trusted IAM role (`bootstrap/github-oidc.yaml`)
scoped to this repo's `main` branch — no AWS access keys stored anywhere.

## Repo layout

```
├── template.yaml              SAM template: DynamoDB table, Lambda, API Gateway routes
├── src/app.py                 CRUD handler (create/get/list/update/delete)
├── tests/
│   ├── unit/                  moto-mocked DynamoDB, no Docker, sub-second
│   ├── integration/           spins up a real amazon/dynamodb-local container
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

## What actually broke getting the deploy working

The pipeline itself (build, local invoke, unit/integration tests) worked on the
first real run. Getting the *deploy* step green took four distinct, real IAM/OIDC
issues, found only by reading CloudTrail events and CloudFormation stack events
directly — each one's surface error pointed at the wrong layer:

1. **Missing `permissions: id-token: write`** on the workflow job — without it,
   `configure-aws-credentials` fails with a generic "could not load credentials,"
   which looks like a bad role ARN but is actually no OIDC token ever being requested.
2. **GitHub embeds immutable org/repo IDs in the OIDC `sub` claim** —
   `repo:org@orgId/repo@repoId:ref:...`, not the plain `repo:org/repo:ref:...` most
   examples show. A trust policy written with just the names silently never
   matches; confirmed by reading the actual `sub` claim off a CloudTrail
   `AssumeRoleWithWebIdentity` `AccessDenied` event.
3. **`sam deploy --resolve-s3`'s own bootstrap stack uses the SAM transform
   internally**, so the deploy role needs `cloudformation:CreateChangeSet` on
   `arn:aws:cloudformation:*:aws:transform/Serverless-2016-10-31` before it can
   even create *that* stack, let alone the app's.
4. **That bootstrap stack's S3 bucket needs `s3:TagResource` to be created and
   `s3:DeleteBucket` to roll back on failure** — missing either wedges the stack
   in `REVIEW_IN_PROGRESS`/`ROLLBACK_FAILED`, and SAM CLI then refuses to reuse it.

All four are fixed in `bootstrap/github-oidc.yaml`'s policy, with comments
explaining why each statement exists. The full write-up, with the actual
CloudTrail/CloudFormation commands used to diagnose each one, is in
[Guide 6](guides/06-troubleshooting-log.md).

## Guides

The `guides/` folder documents the project end to end, as six sequential
guides:

1. [Project overview and architecture](guides/01-project-overview.md)
2. [Environment setup](guides/02-environment-setup.md)
3. [Testing strategy](guides/03-testing-strategy.md) — why four different test layers exist and what each one actually proves
4. [CI/CD pipeline](guides/04-cicd-pipeline.md)
5. [GitHub OIDC deploy setup](guides/05-github-oidc-deploy-setup.md) — how to reproduce the OIDC bootstrap in a new AWS account
6. [Troubleshooting log](guides/06-troubleshooting-log.md) — every real issue hit getting the deploy working, with cause and fix
