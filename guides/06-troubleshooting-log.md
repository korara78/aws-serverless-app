# Guide 6: Troubleshooting Log — What Actually Broke Getting the Deploy Working

**Last updated:** 2026-08-03

The build, the `sam local invoke` smoke check, and the full unit +
integration pytest suite all passed on the very first real CI run. Getting
the *deploy* step from red to green took four distinct, real issues, found
in this order, each one only after the previous one was actually fixed and
pushed. None of them were guessed at — each was traced through the actual
GitHub Actions log, AWS CloudTrail, or CloudFormation stack events before
being fixed. Keeping this log rather than cleaning it up afterward, because
this — vague error, find the actual cause, fix the specific thing, verify,
move on — is what setting up cloud CI/CD actually looks like.

---

### 1. Missing `id-token: write` permission

**Symptom:**
```
Error: Credentials could not be loaded, please check your action inputs: Could not load credentials from any providers
```
right at the `Configure AWS credentials` step, despite the role ARN and
region secrets both being set correctly.

**Cause:** `aws-actions/configure-aws-credentials` needs to request a
GitHub-signed OIDC token to exchange for AWS credentials, and GitHub only
issues that token to a job that's explicitly granted `id-token: write`.
Without it, there's no token to exchange at all — the error message just
doesn't say that directly, so it reads like a role/secret configuration
problem instead of a missing-permission one.

**Fix:**
```yaml
jobs:
  build-and-test:
    permissions:
      id-token: write
      contents: read
```

---

### 2. GitHub OIDC sub claim format

**Symptom:**
```
Could not assume role with OIDC: Not authorized to perform sts:AssumeRoleWithWebIdentity
```
— despite the trust policy's `sub` condition looking correct
(`repo:korara78/aws-serverless-app:ref:refs/heads/main`), and both the
OIDC provider and the role visibly existing with the right ARNs.

**Cause:** found by reading the actual failed request directly, rather
than re-guessing the trust policy:
```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=AssumeRoleWithWebIdentity \
  --max-results 5
```
The event's `userIdentity.userName` was
`repo:korara78@79677336/aws-serverless-app@1320920972:ref:refs/heads/main`
— GitHub embeds immutable organization/repository IDs in the `sub` claim
alongside the human-readable names, not just the plain
`repo:org/repo:ref:...` format shown in most quick-start examples. A trust
policy written with just the plain names has a condition that simply never
matches anything a real token will ever contain.

**Fix:** look up the real IDs and match on the full format:
```bash
gh api users/korara78 --jq .id        # 79677336
gh api repos/korara78/aws-serverless-app --jq .id   # 1320920972
```
```yaml
StringLike:
  token.actions.githubusercontent.com:sub: "repo:korara78@79677336/aws-serverless-app@1320920972:ref:refs/heads/main"
```

---

### 3. Missing permission for the Serverless transform

**Symptom:** OIDC assumption now succeeded, but `sam deploy` failed with:
```
Error: Failed to create managed resources: Waiter ChangeSetCreateComplete failed: ... matched expected path: "FAILED"
```
Digging into the actual changeset (`aws cloudformation list-change-sets
--stack-name aws-sam-cli-managed-default`) showed the real reason:
```
User: .../aws-serverless-app-github-actions-deploy is not authorized to perform:
cloudformation:CreateChangeSet on resource:
arn:aws:cloudformation:us-east-1:aws:transform/Serverless-2016-10-31
```

**Cause:** this looked like an app-specific permission gap, but it's
actually about SAM CLI's own `--resolve-s3` bootstrap stack
(`aws-sam-cli-managed-default`, which creates the S3 bucket deployment
artifacts get uploaded to) — that bootstrap template itself uses
`Transform: AWS::Serverless-2016-10-31`, so the deploy role needs rights to
that transform before it can even create *this* stack, separately from
whatever `template.yaml`'s own transform usage requires.

**Fix:** grant `cloudformation:CreateChangeSet` on the transform's own
(AWS-owned, account-agnostic) ARN:
```yaml
Resource:
  - "arn:aws:cloudformation:*:aws:transform/Serverless-2016-10-31"
```

---

### 4. Missing S3 tagging/delete permissions, and a wedged stack

**Symptom:** past the transform fix, `sam deploy` failed again:
```
Error: Failed to create managed resources: Waiter StackCreateComplete failed:
... matched expected path: "ROLLBACK_FAILED"
```
`aws cloudformation describe-stack-events --stack-name aws-sam-cli-managed-default`
showed two separate `AccessDenied` errors in sequence: bucket creation
failed on `s3:TagResource`, and the automatic rollback that followed then
*also* failed, on `s3:DeleteBucket` — leaving the stack stuck in
`ROLLBACK_FAILED` with an orphaned (but empty) S3 bucket.

**Cause:** the original policy granted `CreateBucket`/`PutObject`/etc. but
not the tagging or deletion actions SAM CLI's bootstrap template actually
uses.

**Fix:** add both, then clean up the wedged stack once the permissions
were actually correct (see
[Guide 5](05-github-oidc-deploy-setup.md#if-the-deploy-roles-bootstrap-s3-stack-ever-gets-stuck)
for the general recovery steps):
```yaml
- s3:TagResource
- s3:UntagResource
- s3:PutBucketTagging
- s3:DeleteBucket
```
```bash
aws cloudformation delete-stack --stack-name aws-sam-cli-managed-default --profile <profile> --region us-east-1
aws cloudformation wait stack-delete-complete --stack-name aws-sam-cli-managed-default --profile <profile> --region us-east-1
```

This actually happened *twice* — once after fix #3 revealed fix #4's gap,
and the stack needed clearing both times before the next `sam deploy`
attempt could proceed.

---

## The pattern across all of this

Every one of these had a surface error pointing at the wrong layer: a
missing-permission error that read like a credentials error, an
`AccessDenied` that read like a typo'd ARN, a waiter-timeout message that
buried the actual `AccessDenied` two API calls deep. The fix each time was
the same: stop guessing at the workflow YAML, and go read the actual
CloudTrail event or CloudFormation stack event describing exactly which
principal was denied exactly which action on exactly which resource. Four
fixes later, pushed and verified with `gh run watch --exit-status`, the
pipeline went fully green end to end — build, local invoke, tests, OIDC
auth, deploy, and a live smoke test against the real endpoint.
