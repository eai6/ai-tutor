"""ALB, target group, and the WAF Web ACL.

Two settings here are easy to miss and expensive to discover late:

* **Idle timeout 120s.** The ALB default is 60. Gunicorn runs with
  ``--timeout 120``, and tutoring turns routinely take many seconds because
  they wait on an LLM. Leaving the default would sever long requests at the
  load balancer with a 504 while the worker kept going.
* **Deregistration delay 120s.** So in-flight tutoring turns drain on deploy
  rather than being dropped mid-answer.

HTTPS is conditional on ``domain-name`` being set. Without it the stack stays
HTTP-only, because a public ACM certificate cannot be issued for an AWS-owned
ELB domain — the hostname has to be one you control.

With a domain, port 80 becomes a 301 redirect rather than a second way in.
Serving the same content on both is how a session cookie marked Secure gets
sent over plaintext anyway by a client that followed an old http:// link.
"""
from __future__ import annotations

from dataclasses import dataclass

import pulumi
import pulumi_aws as aws

APP_PORT = 8000


@dataclass
class Edge:
    alb: aws.lb.LoadBalancer
    target_group: aws.lb.TargetGroup
    web_acl: aws.wafv2.WebAcl
    certificate: "aws.acm.Certificate | None" = None
    validation_records: "object | None" = None


def create_edge(
    prefix: str,
    vpc_id,
    public_subnet_ids,
    alb_sg_id,
    waf_block_mode: bool,
    tags: dict,
    domain_name: "str | None" = None,
    hosted_zone_id: "str | None" = None,
) -> Edge:
    alb = aws.lb.LoadBalancer(
        f"{prefix}-alb",
        name=f"{prefix}-alb",
        load_balancer_type="application",
        internal=False,
        security_groups=[alb_sg_id],
        subnets=public_subnet_ids,
        idle_timeout=120,  # must match gunicorn --timeout 120; see module docstring
        enable_deletion_protection=False,
        drop_invalid_header_fields=True,
        tags={**tags, "Name": f"{prefix}-alb"},
    )

    target_group = aws.lb.TargetGroup(
        f"{prefix}-tg",
        name=f"{prefix}-tg",
        port=APP_PORT,
        protocol="HTTP",
        vpc_id=vpc_id,
        target_type="ip",  # required for Fargate awsvpc networking
        deregistration_delay=120,
        health_check=aws.lb.TargetGroupHealthCheckArgs(
            enabled=True,
            path="/health/",
            protocol="HTTP",
            port="traffic-port",
            matcher="200",
            interval=30,
            timeout=10,
            healthy_threshold=2,
            unhealthy_threshold=3,
        ),
        tags={**tags, "Name": f"{prefix}-tg"},
    )

    # ── Certificate + HTTPS ────────────────────────────────────────────────
    # DNS validation, not email: email validation needs a mailbox at the
    # domain and cannot renew unattended. The zone for seselai.sc is at
    # name.com rather than Route 53, so the validation record is added by
    # hand once — the CNAME name/value are exported for that purpose.
    #
    # ACM re-validates on renewal using the same record, so it has to STAY in
    # the zone. Removing it after issuance silently breaks the renewal ~11
    # months later, which is the classic way a certificate expires on a
    # Saturday.
    certificate = None
    validation_records = None
    if domain_name:
        certificate = aws.acm.Certificate(
            f"{prefix}-cert",
            domain_name=domain_name,
            validation_method="DNS",
            tags={**tags, "Name": f"{prefix}-cert"},
            opts=pulumi.ResourceOptions(
                # A cert cannot be edited in place; replacing before deleting
                # keeps the listener serving throughout.
                delete_before_replace=False,
            ),
        )
        validation_records = certificate.domain_validation_options

        # Blocks until ACM sees the CNAME and issues. Both listener changes
        # below depend on this, which is the whole point: without it Pulumi
        # would flip port 80 to redirect at a 443 listener that failed to
        # create (ACM refuses an unissued cert), taking the site down until
        # someone added a DNS record by hand.
        #
        # Expect this to sit waiting while you add the record at name.com. If
        # it times out, nothing has changed — the site is still served on 80.
        # With a hosted zone in this account, the validation record is created
        # here rather than by hand at a registrar. That matters beyond
        # convenience: ACM re-validates on RENEWAL using this record, so a
        # hand-added one that someone later tidies up breaks the renewal
        # roughly eleven months later, long after anyone connects the two.
        # Under Pulumi it cannot silently disappear.
        validation_fqdns = None
        if hosted_zone_id:
            validation_record = aws.route53.Record(
                f"{prefix}-cert-validation-record",
                zone_id=hosted_zone_id,
                name=certificate.domain_validation_options[0].resource_record_name,
                type=certificate.domain_validation_options[0].resource_record_type,
                records=[certificate.domain_validation_options[0].resource_record_value],
                ttl=60,
                allow_overwrite=True,
            )
            validation_fqdns = [validation_record.fqdn]

        cert_validation = aws.acm.CertificateValidation(
            f"{prefix}-cert-validation",
            certificate_arn=certificate.arn,
            validation_record_fqdns=validation_fqdns,
            opts=pulumi.ResourceOptions(custom_timeouts=pulumi.CustomTimeouts(create="45m")),
        )

    if domain_name:
        # Port 80 redirects rather than serving. Two live ways in is how a
        # Secure-marked session cookie ends up travelling in plaintext because
        # something followed an old http:// link.
        aws.lb.Listener(
            f"{prefix}-http",
            load_balancer_arn=alb.arn,
            port=80,
            protocol="HTTP",
            default_actions=[
                aws.lb.ListenerDefaultActionArgs(
                    type="redirect",
                    redirect=aws.lb.ListenerDefaultActionRedirectArgs(
                        protocol="HTTPS", port="443", status_code="HTTP_301"
                    ),
                )
            ],
            opts=pulumi.ResourceOptions(depends_on=[cert_validation]),
        )

        aws.lb.Listener(
            f"{prefix}-https",
            load_balancer_arn=alb.arn,
            port=443,
            protocol="HTTPS",
            # TLS 1.2 floor. TLS13-1-2-2021-06 offers 1.3 and keeps 1.2 for
            # older Android handsets, which the Seychelles pilot has.
            ssl_policy="ELBSecurityPolicy-TLS13-1-2-2021-06",
            certificate_arn=certificate.arn,
            default_actions=[
                aws.lb.ListenerDefaultActionArgs(
                    type="forward", target_group_arn=target_group.arn
                )
            ],
            opts=pulumi.ResourceOptions(depends_on=[cert_validation]),
        )
    else:
        aws.lb.Listener(
            f"{prefix}-http",
            load_balancer_arn=alb.arn,
            port=80,
            protocol="HTTP",
            default_actions=[
                aws.lb.ListenerDefaultActionArgs(
                    type="forward", target_group_arn=target_group.arn
                )
            ],
        )

    # ── WAF ────────────────────────────────────────────────────────────────
    # Shipped in COUNT mode, switched to BLOCK on 2026-08-07 (waf-block-mode).
    #
    # The original note said to flip only once the sampled logs were clean,
    # because the common rule set false-positives on rich-text teacher
    # dashboard input and blocking real teachers is the wrong way to find that
    # out. That reasoning still holds — it just does not apply yet. This
    # environment has no users, so the cost of a false positive today is a
    # `curl` that fails instead of a classroom that stops, and the database is
    # about to receive a copy of real student records. Better to have the rules
    # live before the data lands than after.
    #
    # The teacher-dashboard false positive is therefore UNRESOLVED, not
    # disproven. When real teachers first use this environment, check the
    # AWSManagedRulesCommonRuleSet counter before believing a report that the
    # dashboard is broken.
    override = (
        aws.wafv2.WebAclRuleOverrideActionArgs(none=aws.wafv2.WebAclRuleOverrideActionNoneArgs())
        if waf_block_mode
        else aws.wafv2.WebAclRuleOverrideActionArgs(count=aws.wafv2.WebAclRuleOverrideActionCountArgs())
    )

    managed_groups = [
        ("AWSManagedRulesCommonRuleSet", 10),
        ("AWSManagedRulesKnownBadInputsRuleSet", 20),
        ("AWSManagedRulesSQLiRuleSet", 30),
    ]

    # Rate limit, priority 0 so it is evaluated before the managed groups.
    #
    # This is the ONLY rate limit on unauthenticated traffic anywhere in the
    # system. apps/safety's RateLimiter keys on user id
    # (apps/safety/__init__.py:277), so it cannot see a request that has not
    # logged in yet — /, /accounts/login/ and /health/ are entirely unrated.
    # That is the gap a credential-stuffing run against student accounts walks
    # through, and it is the reason this rule exists.
    #
    # 2000 per 5 minutes per IP is deliberately loose: a classroom behind one
    # school NAT shares a source address, so a tight limit throttles a whole
    # class rather than an attacker. It stops automation, not browsing.
    RATE_LIMIT_PER_5_MIN = 2000

    # Counts rather than blocks when waf-block-mode is off, so flipping the flag
    # back for debugging silences every rule instead of leaving this one armed.
    rate_action = (
        aws.wafv2.WebAclRuleActionArgs(block=aws.wafv2.WebAclRuleActionBlockArgs())
        if waf_block_mode
        else aws.wafv2.WebAclRuleActionArgs(count=aws.wafv2.WebAclRuleActionCountArgs())
    )

    rate_rule = aws.wafv2.WebAclRuleArgs(
        name="RateLimitPerIP",
        priority=0,
        action=rate_action,
        statement=aws.wafv2.WebAclRuleStatementArgs(
            rate_based_statement=aws.wafv2.WebAclRuleStatementRateBasedStatementArgs(
                limit=RATE_LIMIT_PER_5_MIN,
                # IP, not FORWARDED_IP. The ALB appends the client address as
                # the last X-Forwarded-For hop and WAF sits in front of the
                # ALB, so the source address it sees is already the real one —
                # and unlike the header, it cannot be spoofed. Same reasoning
                # as apps/safety/client_ip.py:51-59.
                aggregate_key_type="IP",
            )
        ),
        visibility_config=aws.wafv2.WebAclRuleVisibilityConfigArgs(
            cloudwatch_metrics_enabled=True,
            metric_name="RateLimitPerIP",
            sampled_requests_enabled=True,
        ),
    )

    web_acl = aws.wafv2.WebAcl(
        f"{prefix}-waf",
        name=f"{prefix}-waf",
        scope="REGIONAL",
        default_action=aws.wafv2.WebAclDefaultActionArgs(
            allow=aws.wafv2.WebAclDefaultActionAllowArgs()
        ),
        rules=[rate_rule]
        + [
            aws.wafv2.WebAclRuleArgs(
                name=group,
                priority=priority,
                override_action=override,
                statement=aws.wafv2.WebAclRuleStatementArgs(
                    managed_rule_group_statement=aws.wafv2.WebAclRuleStatementManagedRuleGroupStatementArgs(
                        name=group,
                        vendor_name="AWS",
                    )
                ),
                visibility_config=aws.wafv2.WebAclRuleVisibilityConfigArgs(
                    cloudwatch_metrics_enabled=True,
                    metric_name=group,
                    sampled_requests_enabled=True,
                ),
            )
            for group, priority in managed_groups
        ],
        visibility_config=aws.wafv2.WebAclVisibilityConfigArgs(
            cloudwatch_metrics_enabled=True,
            metric_name=f"{prefix}-waf",
            sampled_requests_enabled=True,
        ),
        tags={**tags, "Name": f"{prefix}-waf"},
    )

    aws.wafv2.WebAclAssociation(
        f"{prefix}-waf-assoc",
        resource_arn=alb.arn,
        web_acl_arn=web_acl.arn,
    )

    # Alias, not CNAME: an ALIAS resolves to the ALB's addresses directly, is
    # free to query, and unlike a CNAME may sit at a zone apex if this is ever
    # moved to one.
    if domain_name and hosted_zone_id:
        aws.route53.Record(
            f"{prefix}-alias",
            zone_id=hosted_zone_id,
            name=domain_name,
            type="A",
            aliases=[
                aws.route53.RecordAliasArgs(
                    name=alb.dns_name,
                    zone_id=alb.zone_id,
                    evaluate_target_health=True,
                )
            ],
        )

    return Edge(
        alb=alb,
        target_group=target_group,
        web_acl=web_acl,
        certificate=certificate,
        validation_records=validation_records,
    )
