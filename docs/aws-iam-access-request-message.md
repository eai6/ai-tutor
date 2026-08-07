# Message to send to the AWS account admin

Copy-paste ready. Replace `[NAME]` and adjust the sign-off.

---

**Subject:** IAM permission request — ECS deploy blocked on account 968025288404

Hi [NAME],

I'm deploying our AI Tutor app to ECS Fargate in account **968025288404** (us-east-1). Everything else I need works under my current SSO PowerUserAccess role, but ECS is completely blocked, because PowerUserAccess excludes all of `iam:*`.

The specific blocker is **`iam:PassRole`**. On Fargate, the task execution role is what pulls the image from ECR, writes container logs to CloudWatch, and resolves Secrets Manager references — a task definition can't omit it, and naming a role in one requires `PassRole`. The exact error:

```
An error occurred (AccessDeniedException) when calling the RegisterTaskDefinition
operation: User: arn:aws:sts::968025288404:assumed-role/
AWSReservedSSO_PowerUserAccess_8feb467c35e04076/dchacha is not authorized to
perform: iam:PassRole on resource:
arn:aws:iam::968025288404:role/ecsTaskExecutionRole
```

I checked this against the live account rather than assuming from the policy: `iam:ListRoles` works, `iam:GetRole` and `RegisterTaskDefinition` both return AccessDenied, and every non-IAM service I need (ECS, EC2, ELBv2, ECR, RDS, S3, Secrets Manager, CloudWatch Logs, WAFv2, SESv2) is reachable. Worth noting that `ecsTaskExecutionRole` and the ECS/RDS/ELB service-linked roles **already exist**, so nothing has to be created from scratch.

Could you attach the following policy to my permission set (`AWSReservedSSO_PowerUserAccess_8feb467c35e04076`)?

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

I've kept it deliberately narrow:

- The `iam:PassedToService` condition means these roles can only ever be handed to **ECS tasks** — not EC2, not Lambda, not anything else.
- Role management is confined to the **`aitutor-*` name prefix**, so it grants nothing at all over any role that already exists.

**If delegating role creation isn't acceptable**, that's fine — the alternative is that you create two roles by hand and I only need the first statement (`PassRole`). I've written up the exact trust policies and inline policies for that route; happy to send them over.

Two smaller things while you're in there, just to save a second round trip:

1. **A GitHub Actions OIDC provider and deploy role**, for when CI deploys to AWS. Not urgent — manual deploys work without it.
2. **SES production access** — we're currently sandboxed at 200 messages/day. That's a support request from the SES console rather than an IAM change, but it gates real transactional email (password resets, etc.).

Thanks,
Daniel
