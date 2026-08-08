"""Private media bucket and the container registry.

The bucket is private with no public access and no website config. Media is
served *through Django* at ``/media/<path>`` by
``apps/media_library/s3_media.py`` — never a bucket URL and never a presigned
link — because school networks allowlist our own domain and nothing else.
Opening this bucket up would quietly break that guarantee.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pulumi_aws as aws


@dataclass
class Storage:
    media_bucket: aws.s3.BucketV2
    ecr_repo: aws.ecr.Repository
    ops_bucket: aws.s3.BucketV2


def create_storage(prefix: str, account_id: str, tags: dict) -> Storage:
    # Bucket names are globally unique across all of AWS, so the account id is
    # appended to keep this deterministic rather than random.
    bucket = aws.s3.BucketV2(
        f"{prefix}-media",
        bucket=f"{prefix}-media-{account_id}",
        tags={**tags, "Name": f"{prefix}-media"},
    )

    aws.s3.BucketPublicAccessBlock(
        f"{prefix}-media-pab",
        bucket=bucket.id,
        block_public_acls=True,
        block_public_policy=True,
        ignore_public_acls=True,
        restrict_public_buckets=True,
    )

    aws.s3.BucketServerSideEncryptionConfigurationV2(
        f"{prefix}-media-sse",
        bucket=bucket.id,
        rules=[
            aws.s3.BucketServerSideEncryptionConfigurationV2RuleArgs(
                apply_server_side_encryption_by_default=aws.s3.BucketServerSideEncryptionConfigurationV2RuleApplyServerSideEncryptionByDefaultArgs(
                    sse_algorithm="AES256"
                ),
                bucket_key_enabled=True,
            )
        ],
    )

    aws.s3.BucketLifecycleConfigurationV2(
        f"{prefix}-media-lifecycle",
        bucket=bucket.id,
        rules=[
            aws.s3.BucketLifecycleConfigurationV2RuleArgs(
                id="abort-incomplete-multipart",
                status="Enabled",
                filter=aws.s3.BucketLifecycleConfigurationV2RuleFilterArgs(prefix=""),
                abort_incomplete_multipart_upload=aws.s3.BucketLifecycleConfigurationV2RuleAbortIncompleteMultipartUploadArgs(
                    days_after_initiation=7
                ),
            )
        ],
    )

    # ── Ops bucket ─────────────────────────────────────────────────────────
    # Database dumps and other operational payloads. Deliberately SEPARATE from
    # the media bucket, and the reason matters more than the cost of a second
    # bucket:
    #
    # apps/media_library/s3_media.py::serve_media streams objects out of the
    # media bucket at /media/<path>, and that route has no auth gate. An object
    # dropped in there is reachable over the internet by anyone who can guess
    # the key. A production database dump is the last thing that should live
    # one URL guess away — the whole point of restoring from inside the VPC is
    # that the data never becomes internet-reachable.
    #
    # Nothing in the application knows this bucket exists. It is only ever read
    # by a one-off ECS task.
    ops_bucket = aws.s3.BucketV2(
        f"{prefix}-ops",
        bucket=f"{prefix}-ops-{account_id}",
        tags={**tags, "Name": f"{prefix}-ops"},
    )

    aws.s3.BucketPublicAccessBlock(
        f"{prefix}-ops-pab",
        bucket=ops_bucket.id,
        block_public_acls=True,
        block_public_policy=True,
        ignore_public_acls=True,
        restrict_public_buckets=True,
    )

    aws.s3.BucketServerSideEncryptionConfigurationV2(
        f"{prefix}-ops-sse",
        bucket=ops_bucket.id,
        rules=[
            aws.s3.BucketServerSideEncryptionConfigurationV2RuleArgs(
                apply_server_side_encryption_by_default=aws.s3.BucketServerSideEncryptionConfigurationV2RuleApplyServerSideEncryptionByDefaultArgs(
                    sse_algorithm="AES256"
                ),
                bucket_key_enabled=True,
            )
        ],
    )

    aws.s3.BucketLifecycleConfigurationV2(
        f"{prefix}-ops-lifecycle",
        bucket=ops_bucket.id,
        rules=[
            # A dump that someone forgets to delete deletes itself. Student
            # records should not accumulate in a bucket because a restore was
            # interrupted and nobody came back to tidy up.
            aws.s3.BucketLifecycleConfigurationV2RuleArgs(
                id="expire-ops-payloads",
                status="Enabled",
                filter=aws.s3.BucketLifecycleConfigurationV2RuleFilterArgs(prefix=""),
                expiration=aws.s3.BucketLifecycleConfigurationV2RuleExpirationArgs(days=7),
            ),
            aws.s3.BucketLifecycleConfigurationV2RuleArgs(
                id="abort-incomplete-multipart",
                status="Enabled",
                filter=aws.s3.BucketLifecycleConfigurationV2RuleFilterArgs(prefix=""),
                abort_incomplete_multipart_upload=aws.s3.BucketLifecycleConfigurationV2RuleAbortIncompleteMultipartUploadArgs(
                    days_after_initiation=1
                ),
            ),
        ],
    )

    # ECR. The lifecycle policy replaces Azure's `acr purge` CI step outright —
    # that registry reached 2.3 TB (~$180/mo) before pruning was added, and a
    # declarative rule removes the failure mode instead of scripting around it.
    repo = aws.ecr.Repository(
        f"{prefix}-repo",
        name="aitutor",
        image_tag_mutability="MUTABLE",  # `latest` has to be re-pointable
        image_scanning_configuration=aws.ecr.RepositoryImageScanningConfigurationArgs(
            scan_on_push=True
        ),
        force_delete=True,
        tags={**tags, "Name": f"{prefix}-repo"},
    )

    aws.ecr.LifecyclePolicy(
        f"{prefix}-repo-lifecycle",
        repository=repo.name,
        policy=json.dumps(
            {
                "rules": [
                    {
                        "rulePriority": 1,
                        "description": "Keep the 20 most recent images; older ones are rollback targets we no longer need",
                        "selection": {
                            "tagStatus": "any",
                            "countType": "imageCountMoreThan",
                            "countNumber": 20,
                        },
                        "action": {"type": "expire"},
                    }
                ]
            }
        ),
    )

    return Storage(media_bucket=bucket, ecr_repo=repo, ops_bucket=ops_bucket)
