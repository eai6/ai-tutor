# Console steps for the admin — granting the ECS deploy permissions

Account **968025288404**, region **us-east-1**.

The role in play is `AWSReservedSSO_PowerUserAccess_8feb467c35e04076`, which
means access is managed through **IAM Identity Center** (formerly AWS SSO), not
plain IAM.

> **Do not edit the `AWSReservedSSO_…` role in the IAM console.** It is machine-
> managed. Identity Center overwrites it on the next provisioning cycle and the
> change silently disappears. Everything below happens in **IAM Identity
> Center**, which is the only place the change sticks.

---

## Choose one route first

**Route A — new permission set (recommended).** Grants the extra IAM rights to
one person for one project. Nobody else's access widens.

**Route B — edit the existing `PowerUserAccess` permission set.** Fewer steps
and the requester's existing CLI profile keeps working untouched — but *everyone*
who uses PowerUserAccess in this account gets the extra IAM rights too. Only
pick this if that group is small and trusted.

Both end with the same policy attached. Route A is written out in full; Route B
is the same policy applied at step A4 to the existing set.

---

## Route A — create a dedicated permission set

### A1. Open IAM Identity Center

Sign in to the AWS console with an account that administers Identity Center
(the Organizations management account, or a delegated administrator).

Go to **IAM Identity Center** → in the left sidebar, **Permission sets**.

Check the region selector top-right matches the region Identity Center runs in —
it is often **not** `us-east-1`. If the permission sets list is empty, that is
usually why.

### A2. Create the permission set

**Create permission set** → **Custom permission set** → **Next**

### A3. Attach the base policy

Expand **AWS managed policies**, search `PowerUserAccess`, tick it.

This preserves everything the requester has today; the next step only adds the
missing IAM rights.

### A4. Add the inline policy

Expand **Inline policy** on the same screen and paste this in full, replacing
whatever is in the box:

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

**Next**.

### A5. Name it

- **Permission set name:** `AITutorDeploy`
- **Description:** `PowerUserAccess plus ECS PassRole and aitutor-* role management`
- **Session duration:** `8 hours` (a deploy session outliving the default hour
  saves repeated re-authentication)

**Next** → review → **Create**.

### A6. Assign it to the user

Left sidebar → **AWS accounts** → tick account **968025288404** → **Assign users
or groups**.

- **Users** tab → select the requester (`dchacha`) → **Next**
- Select the **`AITutorDeploy`** permission set → **Next**
- **Submit**

Provisioning takes under a minute.

### A7. Confirm it provisioned

Still under **AWS accounts**, expand account 968025288404. `AITutorDeploy`
should be listed against the user with status **Provisioned**.

If it shows a re-provision prompt, click **Provision** and wait for it to clear.

---

## Route B — edit the existing permission set instead

**IAM Identity Center** → **Permission sets** → **PowerUserAccess** →
**Inline policy** tab → **Edit** → paste the same JSON from step A4 → **Save
changes**.

Then, important: open the **AWS accounts** page, find account 968025288404, and
if it prompts that the permission set needs re-provisioning, click
**Provision**. Without that, the policy is saved but not yet live on the role.

No new assignment is needed — the requester keeps the same role, so their
existing CLI profile works unchanged after a re-login.

---

## Tell the requester to re-authenticate

The change only reaches their session after a fresh login:

```bash
aws sso logout
aws sso login --profile pixeldesignlabs
```

**Route A only:** because the role name changes, they must also re-run
`aws configure sso` and pick the new `AITutorDeploy` role, or add it as a second
profile.

They can confirm the grant landed with:

```bash
aws ecs register-task-definition \
  --family perm-check --requires-compatibilities FARGATE \
  --network-mode awsvpc --cpu 256 --memory 512 \
  --execution-role-arn arn:aws:iam::968025288404:role/ecsTaskExecutionRole \
  --container-definitions '[{"name":"p","image":"public.ecr.aws/docker/library/busybox:latest","essential":true}]'
```

An ARN back means it worked. `AccessDeniedException … iam:PassRole` means it did
not. Clean up afterwards with
`aws ecs deregister-task-definition --task-definition perm-check:1`.

---

## CLI alternative

For an admin who would rather not click. Get the permission set ARN from
`aws sso-admin list-permission-sets --instance-arn <instance-arn>`, then:

```bash
aws sso-admin put-inline-policy-to-permission-set \
  --instance-arn       "<identity-center-instance-arn>" \
  --permission-set-arn "<permission-set-arn>" \
  --inline-policy file://aitutor-ecs-policy.json

# Required — the policy is not live on the role until this runs:
aws sso-admin provision-permission-set \
  --instance-arn       "<identity-center-instance-arn>" \
  --permission-set-arn "<permission-set-arn>" \
  --target-id 968025288404 \
  --target-type AWS_ACCOUNT
```

Save the JSON from step A4 as `aitutor-ecs-policy.json` first.

---

## Two extras, worth doing in the same sitting

**1. GitHub Actions OIDC deploy role.** Not blocking — manual deploys work
without it — but needed before CI can deploy.

IAM console → **Identity providers** → **Add provider** → **OpenID Connect**:

- Provider URL: `https://token.actions.githubusercontent.com`
- Audience: `sts.amazonaws.com`

Then create a role trusting it, restricted to this repository:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::968025288404:oidc-provider/token.actions.githubusercontent.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
      },
      "StringLike": {
        "token.actions.githubusercontent.com:sub": "repo:eai6/ai-tutor:*"
      }
    }
  }]
}
```

The `sub` condition is what stops any other repository assuming this role — do
not loosen it to `*`.

**2. SES production access.** Not an IAM change. The account is sandboxed at 200
messages/day, which blocks real password-reset email.

SES console (us-east-1) → **Account dashboard** → **Request production access**.
Transactional mail, describe the use case (school tutoring platform, password
resets and notifications to enrolled teachers and students), give the website
and a bounce-handling note. Usually answered within a day.
