# AI Tutor on one EC2 instance (Path A) — CloudFormation

Two templates stand up the AWS side of a Path A deployment in any account:

| Template | Stack | Region | Creates |
| --- | --- | --- | --- |
| `aitutor-ec2.yaml` | `<prefix>-prod` | Where the server runs | Instance (first-boot setup included), Elastic IP, security group, instance role, backup bucket, DNS A record, optional SES identity with DKIM and DMARC records, SNS alert topic, 4 alarms, Route 53 health check, daily EBS snapshots, the deploy SSM document, and optionally GitHub's OIDC provider and a deploy role |
| `aitutor-budget.yaml` | `<prefix>-budget` | `us-east-1` | Monthly AWS spend budget with email alerts |

The budget is separate because CloudFormation cannot create `AWS::Budgets::Budget`
in every region (`af-south-1` is one), and an unknown resource type fails the
whole template even behind a false condition. Budgets are account-wide anyway.

What the templates do **not** do: build the image, fill in `.env`, restore data
or request SES production access. Those stay in the deployment plan's runbook
(phases 3 to 5), because they need a person to check each step.

## Before the first deploy

1. **Secrets exist.** The LLM API keys are already in Secrets Manager; the
   template only grants the instance role read access to the ARNs you pass.
2. **Pin the AMI.** Resolve the current Ubuntu 24.04 image once and write the
   id into the parameters file:

   ```bash
   aws ssm get-parameter --region "$AWS_REGION" \
     --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
     --query Parameter.Value --output text
   ```

   Never let it float. A new AMI id on a stack update **replaces the instance**,
   and Postgres lives on its root volume. The instance carries
   `UpdateReplacePolicy: Retain` as a backstop, but the change set will still
   try.
3. **Pick a subnet** in an Availability Zone that offers the instance type:

   ```bash
   aws ec2 describe-instance-type-offerings --location-type availability-zone \
     --filters Name=instance-type,Values=m7i-flex.xlarge --query 'InstanceTypeOfferings[].Location'
   ```
4. **Copy the parameter files** to `params/<account-name>.json` and
   `params/<account-name>-budget.json` from the `*.example.json` files here,
   and fill them in. `params/` is gitignored: the repo is public, and these
   files name accounts, secrets and people.
   - `HostedZoneId` empty means DNS is managed elsewhere. Create the A record
     by hand from the `PublicIp` output.
   - `CreateSesIdentity` is `false` when the domain is already an SES identity.
     The template would otherwise fail on the duplicate.
   - `BudgetScope=project-tag` needs `Project` activated as a cost allocation
     tag (Billing → Cost allocation tags). Without that, the budget reads $0
     and never fires. Use `account` if the account runs nothing else.

## Deploy

Always through a change set, so the plan is read before anything is created or
replaced.

```bash
export AWS_PROFILE=<admin profile> AWS_REGION=<region>
STACK=aitutor-prod PARAMS=params/<account-name>.json

aws cloudformation create-change-set --stack-name "$STACK" --change-set-name initial \
  --change-set-type CREATE --template-body file://aitutor-ec2.yaml \
  --parameters "file://$PARAMS" --capabilities CAPABILITY_NAMED_IAM \
  --tags Key=Project,Value=ai-tutor
aws cloudformation wait change-set-create-complete --stack-name "$STACK" --change-set-name initial
aws cloudformation describe-change-set --stack-name "$STACK" --change-set-name initial \
  --query 'Changes[].ResourceChange.[Action,LogicalResourceId,ResourceType,Replacement]' --output table

aws cloudformation execute-change-set --stack-name "$STACK" --change-set-name initial
aws cloudformation wait stack-create-complete --stack-name "$STACK"
aws cloudformation update-termination-protection --enable-termination-protection --stack-name "$STACK"
aws cloudformation describe-stacks --stack-name "$STACK" --query 'Stacks[0].Outputs' --output table
```

The budget stack is the same sequence with `--region us-east-1`,
`--template-body file://aitutor-budget.yaml`, its own parameters file, and no
`--capabilities`.

After the stack completes:

1. **Confirm the SNS subscription** from the email AWS sends to `AlertEmail`.
   Until then no alarm reaches anyone.
2. **Check first-boot setup.** Open a shell with the `SessionCommand` output,
   then run `sudo cat /var/lib/aitutor-bootstrap.done`. It prints a time once
   setup finished, usually 5 to 10 minutes after launch. The full log is
   `/var/log/aitutor-bootstrap.log`. The instance reboots once at the end if
   the kernel was upgraded.
3. **Wait for DNS:** `dig +short <DomainName>` must return the `PublicIp`
   output before Caddy first starts. Otherwise Let's Encrypt fails and backs off.
4. The **site-down alarm** fires until the app is up in phase 5. That is expected.
   It exists only in `us-east-1` stacks, because Route 53 publishes health-check
   metrics nowhere else. Elsewhere, create it by hand in `us-east-1`.

## Continuous deployment

Accounts map to **GitHub environments**, not branches. Every account runs the
same code from one deploy branch (`aws_deployment`), and CI builds each
commit's image once, as `ghcr.io/<owner>/<repo>:aws-<first 12 of the SHA>`.
An account differs only in its environment's variables.
`.github/workflows/deploy-ec2.yml` explains the triggers; in short:

| To | Do |
| --- | --- |
| Build an image without deploying | Push to `aws_deployment`, or push a `deploy-ec2/build/<label>` tag |
| Deploy every push to one account | Set the **repository** variable `EC2_AUTO_DEPLOY_ENVIRONMENT` to that environment |
| Deploy a commit to any account | `git tag deploy-ec2/<environment>/<label> <sha> && git push origin <that tag>` |
| Redeploy or roll forward by hand | `aws ssm send-command --document-name <DeployDocumentName> --instance-ids <InstanceId> --parameters CommitSha=<full sha>` |

A deploy runs `deploy/compose/deploy.sh` on the server over SSM. It pulls the
image, checks for pending migrations, backs up to S3, switches, and waits for
`/health/`. A failed start rolls back by itself only when no migration ran.
After a migration, it alerts and leaves the restore to a person, because
Django does not reverse migrations.

### One-time setup per account

1. Deploy the stack with `GitHubRepository` and `GitHubEnvironment` set. The
   template creates the OIDC provider (set `CreateGitHubOidcProvider=false` if
   `aws iam list-open-id-connect-providers` already lists
   `token.actions.githubusercontent.com`) and a deploy role. That role can do
   one thing: run the `<prefix>-deploy` SSM document on this server.
2. In GitHub (**needs repository admin**), create the environment and set its
   variables from the stack outputs:

   | Variable | Value |
   | --- | --- |
   | `AWS_REGION` | Stack region |
   | `AWS_DEPLOY_ROLE_ARN` | `DeployRoleArn` output |
   | `EC2_INSTANCE_ID` | `InstanceId` output |
   | `SSM_DEPLOY_DOCUMENT` | `DeployDocumentName` output |
   | `SITE_HOSTNAME` | `DomainName` parameter |

   Then give it protection rules: a required reviewer, and deployment
   branches and tags limited to `aws_deployment` and `deploy-ec2/*`. The
   deploy role trusts only jobs running in this environment, so these rules
   gate every deploy.
3. The first deploy is by hand (runbook phases 3 to 5), because `.env` and the
   data restore must exist before `deploy.sh` can run. CI takes over after that.

### Gotchas

- **Manual runs need the default branch.** GitHub shows "Run workflow" only
  for workflow files on the default branch (`main`), and a push to `main`
  deploys the Azure production stack. Use a `deploy-ec2/...` tag until the
  workflow is on `main`.
- **A new GHCR package is private.** Servers pull without credentials, so a
  fork's first image fails every deploy with `denied` until the package is
  made public. The workflow checks this and says so. A fork also sets
  `IMAGE_REPO=ghcr.io/<owner>/<repo>` in the server's `.env`.
- **`[skip ci]` skips every workflow.** While another workflow also runs on
  pushes to `aws_deployment`, a commit that must not trigger it carries
  `[skip ci]`, and then this one does not build either.
- **Files under `infra/`, `docs/` and `memory/`, and `*.md` files, do not trigger a build.**
  Template changes go through a CloudFormation change set, by hand.

## Updating

Change the template or parameters, then repeat the sequence with
`--change-set-type UPDATE` and a new change set name. Read the `Replacement`
column before executing: **`True` on `Server` means stop.** Two changes interrupt
the server without replacing it: `InstanceType` and the first-boot script each
stop and start it. Editing the first-boot script never re-runs it on an
existing instance.

## Deleting

The instance has API termination protection, and the stack should too, so a
delete is deliberate. The backup bucket and the instance are retained on delete
(`DeletionPolicy: Retain`). Empty and remove them by hand once the data is no
longer needed.
