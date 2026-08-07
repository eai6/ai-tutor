"""ALB, target group, and the WAF Web ACL.

Two settings here are easy to miss and expensive to discover late:

* **Idle timeout 120s.** The ALB default is 60. Gunicorn runs with
  ``--timeout 120``, and tutoring turns routinely take many seconds because
  they wait on an LLM. Leaving the default would sever long requests at the
  load balancer with a 504 while the worker kept going.
* **Deregistration delay 120s.** So in-flight tutoring turns drain on deploy
  rather than being dropped mid-answer.

HTTP only for now. AWS serves no users yet and has no hostname, and a public
ACM certificate cannot be issued for an AWS-owned ELB domain anyway. The 443
listener arrives with a real domain — see the spec.
"""
from __future__ import annotations

from dataclasses import dataclass

import pulumi_aws as aws

APP_PORT = 8000


@dataclass
class Edge:
    alb: aws.lb.LoadBalancer
    target_group: aws.lb.TargetGroup
    web_acl: aws.wafv2.WebAcl


def create_edge(
    prefix: str,
    vpc_id,
    public_subnet_ids,
    alb_sg_id,
    waf_block_mode: bool,
    tags: dict,
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
    # Deployed in COUNT mode first. Azure runs OWASP CRS in Prevention mode, so
    # this is briefly weaker — deliberately. The common rule set false-positives
    # on rich-text teacher dashboard input, and discovering that by blocking
    # real teachers is the wrong way to find out. Flip waf-block-mode to true
    # once the sampled logs are clean; that flip is tracked work, not optional.
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

    web_acl = aws.wafv2.WebAcl(
        f"{prefix}-waf",
        name=f"{prefix}-waf",
        scope="REGIONAL",
        default_action=aws.wafv2.WebAclDefaultActionArgs(
            allow=aws.wafv2.WebAclDefaultActionAllowArgs()
        ),
        rules=[
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

    return Edge(alb=alb, target_group=target_group, web_acl=web_acl)
