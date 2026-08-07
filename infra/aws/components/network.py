"""VPC, subnets, egress, and the security-group chain.

The security-group chain is load-bearing for correctness, not just hygiene:
``apps/safety/client_ip.py`` trusts the LAST ``X-Forwarded-For`` hop, which is
only safe because nothing can reach the app except through the ALB. If the
task security group is ever widened beyond the ALB security group, client IPs
become attacker-controlled.

CIDR is deliberately 10.30.0.0/16 — the Azure VNet uses 10.20.0.0/16, and the
two clouds run in parallel, so the ranges must not collide if they are ever
peered or joined over VPN.
"""
from __future__ import annotations

from dataclasses import dataclass

import pulumi
import pulumi_aws as aws

VPC_CIDR = "10.30.0.0/16"
APP_PORT = 8000
POSTGRES_PORT = 5432


@dataclass
class Network:
    vpc: aws.ec2.Vpc
    public_subnets: list
    private_subnets: list
    alb_sg: aws.ec2.SecurityGroup
    tasks_sg: aws.ec2.SecurityGroup
    rds_sg: aws.ec2.SecurityGroup

    @property
    def public_subnet_ids(self):
        return [s.id for s in self.public_subnets]

    @property
    def private_subnet_ids(self):
        return [s.id for s in self.private_subnets]


def create_network(prefix: str, azs: list[str], region: str, tags: dict) -> Network:
    vpc = aws.ec2.Vpc(
        f"{prefix}-vpc",
        cidr_block=VPC_CIDR,
        enable_dns_hostnames=True,
        enable_dns_support=True,
        tags={**tags, "Name": f"{prefix}-vpc"},
    )

    igw = aws.ec2.InternetGateway(
        f"{prefix}-igw", vpc_id=vpc.id, tags={**tags, "Name": f"{prefix}-igw"}
    )

    public_subnets, private_subnets = [], []
    for i, az in enumerate(azs):
        public_subnets.append(
            aws.ec2.Subnet(
                f"{prefix}-public-{i}",
                vpc_id=vpc.id,
                cidr_block=f"10.30.{i}.0/24",
                availability_zone=az,
                # The ALB lives here. Tasks do not, so no auto public IPs.
                map_public_ip_on_launch=False,
                tags={**tags, "Name": f"{prefix}-public-{az}", "Tier": "public"},
            )
        )
        private_subnets.append(
            aws.ec2.Subnet(
                f"{prefix}-private-{i}",
                vpc_id=vpc.id,
                cidr_block=f"10.30.{10 + i}.0/24",
                availability_zone=az,
                tags={**tags, "Name": f"{prefix}-private-{az}", "Tier": "private"},
            )
        )

    public_rt = aws.ec2.RouteTable(
        f"{prefix}-public-rt",
        vpc_id=vpc.id,
        routes=[aws.ec2.RouteTableRouteArgs(cidr_block="0.0.0.0/0", gateway_id=igw.id)],
        tags={**tags, "Name": f"{prefix}-public-rt"},
    )
    for i, subnet in enumerate(public_subnets):
        aws.ec2.RouteTableAssociation(
            f"{prefix}-public-rta-{i}", subnet_id=subnet.id, route_table_id=public_rt.id
        )

    # ONE NAT gateway, not one per AZ. It is a single point of failure for
    # egress and halves the standing cost (~$33/mo each). Acceptable while AWS
    # serves no users; revisit before it takes real traffic.
    nat_eip = aws.ec2.Eip(f"{prefix}-nat-eip", domain="vpc", tags=tags)
    nat = aws.ec2.NatGateway(
        f"{prefix}-nat",
        allocation_id=nat_eip.id,
        subnet_id=public_subnets[0].id,
        tags={**tags, "Name": f"{prefix}-nat"},
        opts=pulumi.ResourceOptions(depends_on=[igw]),
    )

    private_rt = aws.ec2.RouteTable(
        f"{prefix}-private-rt",
        vpc_id=vpc.id,
        routes=[
            aws.ec2.RouteTableRouteArgs(cidr_block="0.0.0.0/0", nat_gateway_id=nat.id)
        ],
        tags={**tags, "Name": f"{prefix}-private-rt"},
    )
    for i, subnet in enumerate(private_subnets):
        aws.ec2.RouteTableAssociation(
            f"{prefix}-private-rta-{i}",
            subnet_id=subnet.id,
            route_table_id=private_rt.id,
        )

    # S3 gateway endpoint: free, and media traffic would otherwise be billed as
    # NAT data processing. Interface endpoints for ECR/Secrets/Logs are
    # deliberately omitted — at ~$7/mo each they cost more than the NAT traffic
    # they would displace at this scale.
    aws.ec2.VpcEndpoint(
        f"{prefix}-s3-endpoint",
        vpc_id=vpc.id,
        service_name=f"com.amazonaws.{region}.s3",
        route_table_ids=[private_rt.id],
        tags={**tags, "Name": f"{prefix}-s3-endpoint"},
    )

    # ── Security groups: a strict chain, internet → ALB → tasks → database ──
    alb_sg = aws.ec2.SecurityGroup(
        f"{prefix}-alb-sg",
        vpc_id=vpc.id,
        description="ALB: public HTTP ingress",
        ingress=[
            aws.ec2.SecurityGroupIngressArgs(
                protocol="tcp", from_port=80, to_port=80,
                cidr_blocks=["0.0.0.0/0"], description="HTTP from anywhere",
            ),
            # 443 is opened now so adding a TLS listener later needs no SG
            # change. Nothing listens on it until a certificate exists.
            aws.ec2.SecurityGroupIngressArgs(
                protocol="tcp", from_port=443, to_port=443,
                cidr_blocks=["0.0.0.0/0"], description="HTTPS from anywhere (unused until ACM)",
            ),
        ],
        egress=[
            aws.ec2.SecurityGroupEgressArgs(
                protocol="-1", from_port=0, to_port=0, cidr_blocks=["0.0.0.0/0"]
            )
        ],
        tags={**tags, "Name": f"{prefix}-alb-sg"},
    )

    tasks_sg = aws.ec2.SecurityGroup(
        f"{prefix}-tasks-sg",
        vpc_id=vpc.id,
        description="ECS tasks: app port from the ALB ONLY",
        egress=[
            aws.ec2.SecurityGroupEgressArgs(
                protocol="-1", from_port=0, to_port=0, cidr_blocks=["0.0.0.0/0"],
                description="LLM APIs, ECR, S3, Secrets Manager",
            )
        ],
        tags={**tags, "Name": f"{prefix}-tasks-sg"},
    )
    # Declared as a separate rule rather than inline: this is THE control that
    # makes last-hop X-Forwarded-For trustworthy, and it deserves to be
    # greppable on its own.
    aws.ec2.SecurityGroupRule(
        f"{prefix}-tasks-from-alb",
        type="ingress",
        security_group_id=tasks_sg.id,
        source_security_group_id=alb_sg.id,
        protocol="tcp",
        from_port=APP_PORT,
        to_port=APP_PORT,
        description="App port from the ALB security group only",
    )

    rds_sg = aws.ec2.SecurityGroup(
        f"{prefix}-rds-sg",
        vpc_id=vpc.id,
        description="RDS: Postgres from the ECS tasks ONLY",
        tags={**tags, "Name": f"{prefix}-rds-sg"},
    )
    aws.ec2.SecurityGroupRule(
        f"{prefix}-rds-from-tasks",
        type="ingress",
        security_group_id=rds_sg.id,
        source_security_group_id=tasks_sg.id,
        protocol="tcp",
        from_port=POSTGRES_PORT,
        to_port=POSTGRES_PORT,
        description="Postgres from the task security group only",
    )

    return Network(
        vpc=vpc,
        public_subnets=public_subnets,
        private_subnets=private_subnets,
        alb_sg=alb_sg,
        tasks_sg=tasks_sg,
        rds_sg=rds_sg,
    )
