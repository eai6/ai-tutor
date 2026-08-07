# AWS IAM access request — AI Tutor ECS deployment

**Account:** `968025288404`  **Region:** `us-east-1`
**Requesting principal:** `arn:aws:iam::968025288404:role/AWSReservedSSO_PowerUserAccess_8feb467c35e04076` (SSO user `dchacha`)
**Date raised:** 2026-08-07

## What is blocked

Deploying the AI Tutor Django app to ECS Fargate. The `PowerUserAccess` managed
policy excludes all of `iam:*` except `ListRoles` and service-linked-role
creation, so the deploy cannot proceed.

Verified against the live account rather than assumed:

| Probe | Result |
| --- | --- |
| `iam:ListRoles` | OK |
| `iam:GetRole` on `ecsTaskExecutionRole` | **AccessDenied** |
| `ecs:RegisterTaskDefinition` with an execution role | **AccessDenied on `iam:PassRole`** |
| ECS, EC2, ELBv2, ECR, RDS, S3, Secrets Manager, CloudWatch Logs, WAFv2, SESv2 | All OK |

The blocking error, verbatim:

```text
An error occurred (AccessDeniedException) when calling the RegisterTaskDefinition
operation: User: arn:aws:sts::968025288404:assumed-role/
AWSReservedSSO_PowerUserAccess_8feb467c35e04076/dchacha is not authorized to
perform: iam:PassRole on resource:
arn:aws:iam::968025288404:role/ecsTaskExecutionRole
```

**Why this specific permission is unavoidable:** on Fargate the task execution
role is what pulls the image from ECR, writes container logs to CloudWatch, and
resolves Secrets Manager references. A task definition cannot omit it and still
do any of those. Registering a task definition that names a role requires
`iam:PassRole` for that role.

Good news: `ecsTaskExecutionRole`, `AWSServiceRoleForECS`,
`AWSServiceRoleForRDS`, and `AWSServiceRoleForElasticLoadBalancing` **already
exist**, so no service-linked roles need creating.

## Option A — preferred: scoped IAM policy on the SSO permission set

Attach this to the `PowerUserAccess` permission set (or an additional policy on
it). Everything is namespaced to `aitutor-*` except the pass on the existing,
already-present execution role.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PassRolesToEcsTasksOnly",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": [
        "arn:aws:iam::968025288404:role/ecsTaskExecutionRole",
        "arn:aws:iam::968025288404:role/aitutor-*"
      ],
      "Condition": {
        "StringEquals": {
          "iam:PassedToService": "ecs-tasks.amazonaws.com"
        }
      }
    },
    {
      "Sid": "ManageAitutorScopedRoles",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:GetRole",
        "iam:TagRole",
        "iam:UntagRole",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:GetRolePolicy",
        "iam:ListRolePolicies",
        "iam:ListAttachedRolePolicies",
        "iam:UpdateAssumeRolePolicy"
      ],
      "Resource": "arn:aws:iam::968025288404:role/aitutor-*"
    }
  ]
}
```

The `iam:PassedToService` condition means these roles can only ever be handed to
ECS tasks — they cannot be passed to EC2, Lambda, or anything else. The second
statement is confined to a name prefix, so it grants nothing over existing roles.

## Option B — if role creation cannot be delegated

An admin creates the two roles by hand, and the only grant needed is the
`PassRolesToEcsTasksOnly` statement above. Infrastructure code then references
them by ARN rather than managing them.

**Role 1 — `aitutor-task-execution`** (or reuse the existing
`ecsTaskExecutionRole`, which is likely already correct)

- Trust policy: `ecs-tasks.amazonaws.com` may assume it
- Attach the AWS-managed `AmazonECSTaskExecutionRolePolicy`
- Plus `secretsmanager:GetSecretValue` on `arn:aws:secretsmanager:us-east-1:968025288404:secret:aitutor/*`

**Role 2 — `aitutor-task`** (the application's own runtime identity)

- Trust policy: `ecs-tasks.amazonaws.com` may assume it
- Inline policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "MediaBucket",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::aitutor-media-*",
        "arn:aws:s3:::aitutor-media-*/*"
      ]
    },
    {
      "Sid": "TransactionalEmail",
      "Effect": "Allow",
      "Action": ["ses:SendEmail", "ses:SendRawEmail"],
      "Resource": "*"
    },
    {
      "Sid": "DispatchMaterialProcessingTasks",
      "Effect": "Allow",
      "Action": ["ecs:RunTask", "ecs:DescribeTasks"],
      "Resource": "arn:aws:ecs:us-east-1:968025288404:task-definition/aitutor-*"
    },
    {
      "Sid": "PassRolesToTheTasksItStarts",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": [
        "arn:aws:iam::968025288404:role/aitutor-task",
        "arn:aws:iam::968025288404:role/aitutor-task-execution"
      ],
      "Condition": {
        "StringEquals": { "iam:PassedToService": "ecs-tasks.amazonaws.com" }
      }
    }
  ]
}
```

The last statement exists because the web container starts the
material-processing task itself (`apps/dashboard/job_dispatch.py`), which means
it passes roles in turn.

## Also needed, but not blocking yet

**GitHub Actions OIDC deploy role.** Only required once CI deploys to AWS;
manual `pulumi up` runs work without it. Needs an IAM OIDC provider for
`token.actions.githubusercontent.com` plus a role trusting this repository. Raise
it with the same admin to avoid a second round trip.

**SES production access.** Confirmed sandbox: `ProductionAccessEnabled: false`,
200 messages/day. Not an IAM matter — it is a support request from the SES
console, usually approved inside a day. Worth filing early since it gates real
email.

## What proceeds without any of this

VPC, subnets, NAT, security groups, S3, ECR, RDS, Secrets Manager, CloudWatch log
groups, the ALB, and the WAF Web ACL all deploy under the current permissions.
Only ECS task definitions and services are blocked. Build the foundation first
and add ECS when the grant lands.

One caveat: RDS **enhanced monitoring** needs `iam:PassRole` on
`rds-monitoring-role` (which already exists), so leave enhanced monitoring off
until the grant arrives. Standard CloudWatch metrics are unaffected.
