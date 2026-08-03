# Guide 2: Environment Setup

**Last updated:** 2026-08-03

## Prerequisites

| Tool | Why | Notes |
|---|---|---|
| Docker | Runs `amazon/dynamodb-local` for integration tests, and the actual Lambda container for `sam local invoke` | Docker Desktop with WSL2 integration enabled works fine; no special config needed beyond `docker ps` succeeding |
| Python 3.12+ | Runs the Lambda code and the pytest suite | Lambda's own runtime is Python 3.12; using the same version locally avoids version-skew surprises |
| AWS SAM CLI | Builds and locally invokes the Lambda, drives `sam deploy` | Not part of the AWS CLI — separate install |
| AWS CLI | Talks to CloudFormation/S3/IAM directly (used for the one-time OIDC bootstrap, and for inspecting stack state) | |
| An AWS account | Something has to host the deployed stack | See [Guide 5](05-github-oidc-deploy-setup.md) for setting up deploy access without long-lived keys |

This project is developed in WSL2/Ubuntu, matching CI's `ubuntu-latest`
runner — keeping local and CI on the same OS avoids an entire category of
"works on my machine" issues.

## Install

```bash
pip install aws-sam-cli
pip install -r requirements-dev.txt   # pytest, moto, boto3, requests
# AWS CLI: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html
```

Docker just needs to be running and reachable from your shell
(`docker ps` should succeed with no errors).

**Image versioning:** both `Makefile` and `tests/integration/conftest.py`
pin `amazon/dynamodb-local` to a specific tag (`3.3.1`) rather than
`latest`. An unpinned `latest` tag means CI and local runs can silently
pick up a different image on different days — if AWS ever changes
DynamoDB Local's behavior, tests could start failing (or start passing
when they shouldn't) with no actual code change to point to. Bump the tag
deliberately in both places when you want a newer version, rather than
getting one automatically.

## Verify

```bash
sam --version
aws --version
docker ps
python3 -m pytest --version
```

## Running things locally

Every command below is a `make` target — see the [`Makefile`](../Makefile).
Local dev and CI call the exact same targets, so there's no separate
"local instructions" to fall out of sync with what CI actually runs.

```bash
make test-unit          # moto-mocked DynamoDB, no Docker, sub-second
make test-integration   # spins up a real amazon/dynamodb-local container
make test               # both of the above
make test-local-invoke  # sam build + sam local invoke through a real
                         # Docker-networked DynamoDB Local
make deploy              # sam deploy — requires your own configured AWS
                          # credentials (see Guide 5 for the CI path, which
                          # uses OIDC instead of local credentials)
```

## AWS credentials for local use

The `Makefile`'s `deploy` target and any direct `aws`/`sam` commands you run
locally need your own AWS credentials configured — **not** pasted into a
chat session or committed anywhere. Use a named profile:

```bash
aws configure --profile <profile-name>
```

and pass `--profile <profile-name>` on any `aws`/`sam` command, or export
`AWS_PROFILE=<profile-name>` for the session. CI never uses local
credentials at all — see [Guide 5](05-github-oidc-deploy-setup.md) for how
the pipeline authenticates instead.
