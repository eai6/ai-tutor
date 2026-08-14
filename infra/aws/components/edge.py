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
    admin_allowed_cidrs: "list[str] | None" = None,
    admin_path: str = "/admin/",
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

    # SizeRestrictions_BODY blocks any request body over 8 KB. That is not a
    # tuning preference — it broke EVERY upload in the product the moment block
    # mode went on: platform logos, curriculum PDFs, material uploads, feedback
    # screenshots. Confirmed 2026-08-08 by posting to /dashboard/settings/ at
    # increasing sizes: 2 KB reached Django, 32 KB and above came back as the
    # WAF's own block page, and the sampled request named
    # AWS#AWSManagedRulesCommonRuleSet#SizeRestrictions_BODY.
    #
    # Count, not a scope-down to the upload paths. WAF only inspects the first
    # 8 KB of a body regardless, so on an upload route the rule cannot evaluate
    # what it is nominally protecting against — it just measures length. The
    # size limit that matters is Django's DATA_UPLOAD_MAX_MEMORY_SIZE, which
    # applies to every route rather than a list someone has to remember to
    # extend. Counting keeps the metric so oversized bodies stay visible.
    #
    # The rest of the group keeps blocking. This is one rule, not the group.
    # CrossSiteScripting_BODY is the same story one rule along. With
    # SizeRestrictions_BODY counting, real logo uploads were still blocked —
    # 3 for 3 in the sampled requests on 2026-08-08. Compressed image bytes
    # match the XSS signatures often enough to be effectively always: random
    # test payloads tripped it about one time in five, a real PNG every time.
    #
    # The rule inspects the first 8 KB of the body looking for markup. On a
    # multipart image upload there is no markup to find, only binary that
    # sometimes resembles it — so every block here is a false positive by
    # construction.
    #
    # Cost of counting it: WAF no longer screens request BODIES for reflected
    # XSS. Django still auto-escapes template output, which is where stored XSS
    # would surface, and the URI/query-string XSS rules in this same group keep
    # blocking. Defense in depth is reduced, not removed.
    #
    # TIGHTER OPTION, if that trade is unwanted: give the managed group a
    # scope_down_statement that skips requests whose Content-Type is
    # multipart/form-data. Uploads bypass the group entirely; JSON and
    # form-encoded posts keep full body inspection. More precise, more moving
    # parts — deliberately not done under time pressure.
    group_rule_overrides = {
        "AWSManagedRulesCommonRuleSet": [
            "SizeRestrictions_BODY",
            "CrossSiteScripting_BODY",
        ],
    }

    def _rule_action_overrides(group: str):
        """Force named rules in ``group`` to Count while the group blocks."""
        if not waf_block_mode:
            # The whole group is already counting; per-rule overrides would be
            # redundant, and AWS rejects an override that matches the group.
            return None
        return [
            aws.wafv2.WebAclRuleStatementManagedRuleGroupStatementRuleActionOverrideArgs(
                name=rule_name,
                action_to_use=aws.wafv2.WebAclRuleStatementManagedRuleGroupStatementRuleActionOverrideActionToUseArgs(
                    count=aws.wafv2.WebAclRuleStatementManagedRuleGroupStatementRuleActionOverrideActionToUseCountArgs()
                ),
            )
            for rule_name in group_rule_overrides.get(group, [])
        ] or None

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

    # ── Admin console restriction ──────────────────────────────────────────
    # The single highest-value change in the 2026-08-13 assessment (F-04), and
    # the only one needing no application code: /admin/ answers a working login
    # form to the whole internet, and one credential pair behind it is read and
    # write access to every student record, staff account and platform setting.
    #
    # Priority 1, immediately after the rate limit and ahead of the managed
    # groups, so an admin probe is refused before anything spends time
    # inspecting it.
    #
    # OFF unless admin-allowed-cidrs is set, and that default is deliberate:
    # guessing an allow-list here would lock the operators out of their own
    # console on the next deploy, which is how a security control gets reverted
    # in a hurry and stays reverted. Set it explicitly:
    #
    #     pulumi config set --path admin-allowed-cidrs[0] 203.0.113.0/24
    #
    # Blocks unconditionally, even when waf-block-mode is off. Counting this one
    # would leave the console open while reading as protected, and unlike the
    # managed groups there is no false-positive risk to measure first — the rule
    # matches one path and one address list.
    admin_rules = []
    if admin_allowed_cidrs:
        admin_ip_set = aws.wafv2.IpSet(
            f"{prefix}-admin-allowlist",
            name=f"{prefix}-admin-allowlist",
            scope="REGIONAL",
            ip_address_version="IPV4",
            addresses=admin_allowed_cidrs,
            tags={**tags, "Name": f"{prefix}-admin-allowlist"},
        )
        admin_rules.append(
            aws.wafv2.WebAclRuleArgs(
                name="RestrictAdminConsole",
                priority=1,
                action=aws.wafv2.WebAclRuleActionArgs(
                    block=aws.wafv2.WebAclRuleActionBlockArgs()
                ),
                statement=aws.wafv2.WebAclRuleStatementArgs(
                    and_statement=aws.wafv2.WebAclRuleStatementAndStatementArgs(
                        statements=[
                            # starts_with, so /admin/login/ and every nested
                            # admin route are covered by one rule.
                            aws.wafv2.WebAclRuleStatementAndStatementStatementArgs(
                                byte_match_statement=aws.wafv2.WebAclRuleStatementAndStatementStatementByteMatchStatementArgs(
                                    search_string=admin_path,
                                    positional_constraint="STARTS_WITH",
                                    field_to_match=aws.wafv2.WebAclRuleStatementAndStatementStatementByteMatchStatementFieldToMatchArgs(
                                        uri_path=aws.wafv2.WebAclRuleStatementAndStatementStatementByteMatchStatementFieldToMatchUriPathArgs()
                                    ),
                                    text_transformations=[
                                        # Lowercase then URL-decode, so
                                        # /ADMIN/ and /%61dmin/ do not walk
                                        # straight past a literal match.
                                        aws.wafv2.WebAclRuleStatementAndStatementStatementByteMatchStatementTextTransformationArgs(
                                            priority=0, type="URL_DECODE"
                                        ),
                                        aws.wafv2.WebAclRuleStatementAndStatementStatementByteMatchStatementTextTransformationArgs(
                                            priority=1, type="LOWERCASE"
                                        ),
                                    ],
                                )
                            ),
                            aws.wafv2.WebAclRuleStatementAndStatementStatementArgs(
                                not_statement=aws.wafv2.WebAclRuleStatementAndStatementStatementNotStatementArgs(
                                    statements=[
                                        aws.wafv2.WebAclRuleStatementAndStatementStatementNotStatementStatementArgs(
                                            ip_set_reference_statement=aws.wafv2.WebAclRuleStatementAndStatementStatementNotStatementStatementIpSetReferenceStatementArgs(
                                                arn=admin_ip_set.arn
                                            )
                                        )
                                    ]
                                )
                            ),
                        ]
                    )
                ),
                visibility_config=aws.wafv2.WebAclRuleVisibilityConfigArgs(
                    cloudwatch_metrics_enabled=True,
                    metric_name="RestrictAdminConsole",
                    sampled_requests_enabled=True,
                ),
            )
        )

    web_acl = aws.wafv2.WebAcl(
        f"{prefix}-waf",
        name=f"{prefix}-waf",
        scope="REGIONAL",
        default_action=aws.wafv2.WebAclDefaultActionArgs(
            allow=aws.wafv2.WebAclDefaultActionAllowArgs()
        ),
        rules=[rate_rule]
        + admin_rules
        + [
            aws.wafv2.WebAclRuleArgs(
                name=group,
                priority=priority,
                override_action=override,
                statement=aws.wafv2.WebAclRuleStatementArgs(
                    managed_rule_group_statement=aws.wafv2.WebAclRuleStatementManagedRuleGroupStatementArgs(
                        name=group,
                        vendor_name="AWS",
                        rule_action_overrides=_rule_action_overrides(group),
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
