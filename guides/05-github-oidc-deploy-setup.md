# Guide 5: GitHub OIDC Deploy Setup

**Last updated:** 2026-08-03

This guide covers how CI deploys to AWS without ever storing an AWS access
key in GitHub, and how to reproduce this setup from scratch in a new AWS
account.

## Why OIDC instead of access keys

Long-lived IAM access keys stored as GitHub secrets are a real, ongoing
liability — they don't expire on their own, they work from anywhere once
leaked, and revoking them means finding every place they were used.
GitHub's OIDC provider support lets a workflow request a short-lived,
cryptographically signed identity token instead, which AWS can verify and
trust *without any secret ever being stored on either side*. The only
things that need to be configured are: an OIDC identity provider resource
in IAM (once per AWS account), and an IAM role that trusts tokens matching
specific claims (once per repo/branch that needs deploy access).

## What's already set up

`bootstrap/github-oidc.yaml` is a CloudFormation template creating both
pieces:

- `GitHubOidcProvider` — the account's trust relationship with
  `token.actions.githubusercontent.com`. One of these can serve every repo
  in the account; this template creates it since this was a brand-new
  account with none yet.
- `DeployRole` — trusted only by tokens where the `sub` claim exactly
  matches `repo:korara78@<orgId>/aws-serverless-app@<repoId>:ref:refs/heads/main`,
  and scoped to a permission set limited to this app's own resource names
  (`aws-serverless-app-*`) plus two AWS-owned, account-agnostic resources
  every `sam deploy` needs regardless of which app it's deploying (see
  [Guide 6](06-troubleshooting-log.md) for exactly why those two are
  needed — they were not obvious up front).

This stack has already been deployed to the account backing the live
endpoint, and the resulting role ARN is stored as the `AWS_DEPLOY_ROLE_ARN`
GitHub secret, with `AWS_REGION` alongside it. The steps below are for
reproducing this in a *different* AWS account (e.g. if this project were
forked, or moved to a new account).

## Reproducing this from scratch

**1. Get AWS credentials configured locally**, using a temporary IAM user
with sufficient permissions to create the OIDC provider, an IAM role, and
its policy (`AdministratorAccess` is fine for this one-time bootstrap;
narrow it or delete the user afterward). See
[Guide 2](02-environment-setup.md) for the general `aws configure --profile`
pattern. Never paste access keys into a chat session or commit them
anywhere.

**2. Look up the immutable GitHub org/repo IDs.** This is the step that's
easy to skip and then get wrong — see
[Guide 6](06-troubleshooting-log.md#2-github-oidc-sub-claim-format) for why
it matters:

```bash
gh api users/<org> --jq .id
gh api repos/<org>/<repo> --jq .id
```

**3. Deploy the bootstrap stack**, overriding the org/repo/ID parameters if
you're pointing this at a different repo than the default:

```bash
aws cloudformation deploy \
  --template-file bootstrap/github-oidc.yaml \
  --stack-name aws-serverless-app-github-oidc \
  --capabilities CAPABILITY_NAMED_IAM \
  --profile <your-profile> \
  --region us-east-1 \
  --parameter-overrides GitHubOrg=<org> RepoName=<repo> GitHubOrgId=<id> RepoId=<id>
```

**4. Get the role ARN** from the stack output:

```bash
aws cloudformation describe-stacks \
  --stack-name aws-serverless-app-github-oidc \
  --profile <your-profile> --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='DeployRoleArn'].OutputValue" --output text
```

**5. Set the two GitHub repo secrets:**

```bash
gh secret set AWS_DEPLOY_ROLE_ARN --repo <org>/<repo> --body "<role-arn-from-step-4>"
gh secret set AWS_REGION --repo <org>/<repo> --body "<region>"
```

**6. Push to `main`** and watch the Actions run — `gh run watch <run-id> --exit-status`.

## Updating an already-deployed bootstrap stack

The deploy role's permissions aren't fixed forever — adding new AWS
resources to `template.yaml` (the frontend's S3 bucket and CloudFront
distribution, for example) means the deploy role needs new permissions to
create/manage them too, or CI's own `sam deploy` step fails with
`AccessDenied` the moment it tries to touch a resource type it was never
granted. Updating the already-deployed stack uses the *exact same command*
as step 3 above — `aws cloudformation deploy` detects the stack already
exists and creates an update changeset instead of a new stack, so there's
no separate "update" command to remember:

```bash
aws cloudformation deploy \
  --template-file bootstrap/github-oidc.yaml \
  --stack-name aws-serverless-app-github-oidc \
  --capabilities CAPABILITY_NAMED_IAM \
  --profile <your-profile> \
  --region us-east-1
```

This step genuinely can't be done by CI itself — the whole point of scoping
the deploy role tightly is that it can't grant itself new permissions,
including permission to modify its own policy. It has to be run manually,
with admin (or at least IAM-role-editing) credentials, whenever
`bootstrap/github-oidc.yaml`'s policy changes. The GitHub secrets
(`AWS_DEPLOY_ROLE_ARN`, `AWS_REGION`) don't need touching for a policy-only
update — the role's ARN doesn't change, only what it's allowed to do.

## If the deploy role's bootstrap S3 stack ever gets stuck

`sam deploy --resolve-s3` creates its own tiny managed stack
(`aws-sam-cli-managed-default`) the first time it runs, to hold a bucket
for uploaded artifacts. If a permissions gap causes that stack's changeset
or resource creation to fail partway, CloudFormation leaves it in
`REVIEW_IN_PROGRESS` or `ROLLBACK_FAILED`, and SAM CLI will then refuse to
reuse it on the next attempt at all ("not in a healthy state... likely not
created by the AWS SAM CLI"). The fix is to delete that stack once the
underlying permission gap is actually fixed, so the next deploy recreates
it cleanly:

```bash
aws cloudformation delete-stack --stack-name aws-sam-cli-managed-default --profile <profile> --region <region>
aws cloudformation wait stack-delete-complete --stack-name aws-sam-cli-managed-default --profile <profile> --region <region>
```

This is safe as long as the stack never got past changeset creation (no
real resources exist yet), or — if it did create a bucket before failing —
that bucket is confirmed empty first. See
[Guide 6](06-troubleshooting-log.md) for the two times this actually
happened while setting this project up.
