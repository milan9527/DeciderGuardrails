"""Build and deploy the demo. Secrets are passed through SDK/stdin, never CLI arguments."""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import ipaddress
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".deployment"
sys.path.insert(0, str(ROOT))

from scripts.login_config import load_login, runtime_config


def write_private(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Open with restrictive permissions before any sensitive bytes reach disk.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)


def run(command, cwd=ROOT, **kwargs):
    print("Running:", " ".join(str(x) for x in command[:5]), flush=True)
    return subprocess.run(command, cwd=cwd, check=True, **kwargs)


def generate_template():
    spec = importlib.util.spec_from_file_location("infra_template", ROOT / "infra/template.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    template = json.dumps(module.build_template(), indent=2) + "\n"
    (ROOT / "infra/app.json").write_text(template)
    return template


def image_tag():
    files = [ROOT / "pyproject.toml", ROOT / "Dockerfile.backend", ROOT / ".dockerignore"]
    for directory in ("guardrails_bench", "demo_api"):
        files += [p for p in (ROOT / directory).rglob("*")
                  if p.is_file() and p.suffix in {".py", ".json", ".jsonl"}]
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return "src-" + digest.hexdigest()[:20]


def frontend_digest():
    files = [p for p in (ROOT / "frontend/src").rglob("*") if p.is_file()]
    files += [ROOT / "frontend" / name for name in
              ("index.html", "package.json", "package-lock.json", "tsconfig.json", "vite.config.ts")]
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def get_stack(client, name):
    try:
        return client.describe_stacks(StackName=name)["Stacks"][0]
    except ClientError as exc:
        if "does not exist" in exc.response.get("Error", {}).get("Message", ""):
            return None
        raise


def outputs(stack):
    return {item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])}


def apply_stack(client, name, template, parameters=None):
    current = get_stack(client, name)
    arguments = {
        "StackName": name, "TemplateBody": template,
        "Parameters": [{"ParameterKey": key, "ParameterValue": value}
                       for key, value in (parameters or {}).items()],
        "Capabilities": ["CAPABILITY_IAM"],
        "Tags": [{"Key": "Application", "Value": "decider-guardrails"},
                 {"Key": "ManagedBy", "Value": "CloudFormation"}],
    }
    if current:
        if current["StackStatus"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"}:
            raise RuntimeError(f"{name} is {current['StackStatus']}; inspect stack events before retrying")
        try:
            client.update_stack(**arguments)
        except ClientError as exc:
            if "No updates are to be performed" in exc.response["Error"]["Message"]:
                print(f"{name}: no infrastructure changes", flush=True)
                return outputs(current)
            raise
    else:
        client.create_stack(**arguments, OnFailure="ROLLBACK")
    previous = None
    deadline = time.monotonic() + 3600
    while time.monotonic() < deadline:
        stack = get_stack(client, name)
        status = stack["StackStatus"]
        events = client.describe_stack_events(StackName=name)["StackEvents"]
        latest = events[0]
        state = (status, latest["EventId"])
        if state != previous:
            print(f"{name}: {status} · {latest['LogicalResourceId']} {latest['ResourceStatus']}",
                  flush=True)
            previous = state
        if status in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
            return outputs(stack)
        if not status.endswith("_IN_PROGRESS"):
            failures = [f"{e['LogicalResourceId']}: {e.get('ResourceStatusReason', '')}"
                        for e in events if e["ResourceStatus"].endswith("FAILED")]
            raise RuntimeError(f"{name}: {status}\n" + "\n".join(failures[:5]))
        time.sleep(10)
    raise RuntimeError(f"Timed out waiting for {name}; resources may still be deploying")


def context(session, args):
    identity = session.client("sts").get_caller_identity()
    if args.account and identity["Account"] != args.account:
        raise RuntimeError("AWS account differs from --account")
    ec2 = session.client("ec2")
    existing = get_stack(session.client("cloudformation"), args.project + "-app")
    existing_parameters = {
        p["ParameterKey"]: p["ParameterValue"] for p in (existing or {}).get("Parameters", [])
    }
    selected_vpc = args.vpc_id or existing_parameters.get("Vpc")
    excluded = {"us-east-1": {"use1-az3"}, "us-west-1": {"usw1-az2"}}
    zones = [
        zone["ZoneName"]
        for zone in ec2.describe_availability_zones(
            Filters=[{"Name": "state", "Values": ["available"]}, {"Name": "zone-type", "Values": ["availability-zone"]}]
        )["AvailabilityZones"]
        if zone["ZoneId"] not in excluded.get(args.region, set())
    ]
    if len(zones) < 2:
        raise RuntimeError("Two CloudFront VPC-origin-compatible availability zones are required")
    prefix_lists = ec2.describe_managed_prefix_lists(
        Filters=[{"Name": "prefix-list-name", "Values": ["com.amazonaws.global.cloudfront.origin-facing"]}]
    )["PrefixLists"]
    if not prefix_lists:
        raise RuntimeError("CloudFront origin-facing prefix list is unavailable in this region")
    vpcs = ec2.describe_vpcs(
        **({"VpcIds": [selected_vpc]} if selected_vpc else {
            "Filters": [{"Name": "is-default", "Values": ["true"]}],
        })
    )["Vpcs"]
    if len(vpcs) != 1:
        raise RuntimeError("No default VPC; specify an existing --vpc-id")
    vpc = vpcs[0]
    gateways = ec2.describe_internet_gateways(
        Filters=[{"Name": "attachment.vpc-id", "Values": [vpc["VpcId"]]}]
    )["InternetGateways"]
    if not gateways:
        raise RuntimeError("Selected VPC must already have an internet gateway")
    if not ec2.describe_vpc_attribute(
        VpcId=vpc["VpcId"], Attribute="enableDnsSupport"
    )["EnableDnsSupport"]["Value"]:
        raise RuntimeError("Selected VPC must have DNS support enabled")
    network_keys = ("Vpc", "VpcCidr", "InternetGateway", "TaskSubnetACidr", "OriginSubnetACidr",
                    "TaskSubnetBCidr", "OriginSubnetBCidr")
    if (all(key in existing_parameters for key in network_keys)
            and existing_parameters["Vpc"] == vpc["VpcId"]):
        # Re-deployment needs no new address space and must preserve its existing subnets.
        network = {key: existing_parameters[key] for key in network_keys}
        original_zones = [existing_parameters.get("AvailabilityZone" + name, zones[i])
                          for i, name in enumerate(("A", "B"))]
        return identity["Account"], original_zones, prefix_lists[0]["PrefixListId"], network
    occupied = [
        ipaddress.ip_network(s["CidrBlock"])
        for s in ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc["VpcId"]]}])["Subnets"]
    ]
    available = [
        str(subnet) for subnet in ipaddress.ip_network(vpc["CidrBlock"]).subnets(new_prefix=24)
        if not any(subnet.overlaps(existing) for existing in occupied)
    ]
    if len(available) < 4:
        raise RuntimeError("Selected VPC needs space for four new /24 subnets")
    network = {
        "Vpc": vpc["VpcId"], "VpcCidr": vpc["CidrBlock"],
        "InternetGateway": gateways[0]["InternetGatewayId"],
        **dict(zip(("TaskSubnetACidr", "OriginSubnetACidr", "TaskSubnetBCidr", "OriginSubnetBCidr"),
                   available[:4])),
    }
    return identity["Account"], zones[:2], prefix_lists[0]["PrefixListId"], network


def prepare(session, args):
    load_login(args.credentials_file)
    template = generate_template()
    cfn = session.client("cloudformation")
    for path in ("foundation.json", "app.json"):
        cfn.validate_template(TemplateBody=(ROOT / "infra" / path).read_text())
    account, zones, prefix_list, network = context(session, args)
    tag = image_tag()
    run(["npm", "ci", "--no-audit", "--no-fund"], cwd=ROOT / "frontend")
    run(["npm", "run", "build"], cwd=ROOT / "frontend")
    run(["docker", "build", "--platform", "linux/arm64", "-f", "Dockerfile.backend",
         "-t", f"{args.project}:{tag}", "."])
    plan = {
        "account": account, "region": args.region, "project": args.project,
        "availability_zones": zones, "cloudfront_prefix_list": prefix_list,
        "network": network,
        "image_tag": tag,
        "frontend_source_sha256": frontend_digest(),
        "app_template_sha256": hashlib.sha256(template.encode()).hexdigest(),
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        "architecture": [
            "Private S3 bucket + CloudFront OAC",
            "CloudFront VPC origin + internal ALB in private subnets",
            "One ARM64 ECS Fargate API task, 0.5 vCPU / 1 GiB",
            "Task public IPv4 is for outbound HTTPS; inbound API only from internal ALB",
            "ECR, Secrets Manager and CloudWatch Logs; no NAT gateway",
            "Existing VPC/IGW with four newly allocated dedicated /24 subnets",
        ],
    }
    write_private(STATE / "plan.json", json.dumps(plan, indent=2) + "\n")
    print("Prepared:", STATE / "plan.json", flush=True)
    return plan


def read_prepared(session, args):
    plan = json.loads((STATE / "plan.json").read_text())
    account = session.client("sts").get_caller_identity()["Account"]
    if (plan["account"], plan["region"], plan["project"]) != (account, args.region, args.project):
        raise RuntimeError("Prepared account/region/project do not match deployment")
    if plan["image_tag"] != image_tag():
        raise RuntimeError("Backend changed after prepare; run prepare again")
    if plan.get("frontend_source_sha256") != frontend_digest():
        raise RuntimeError("Frontend changed after prepare; run prepare again")
    if plan["app_template_sha256"] != hashlib.sha256(generate_template().encode()).hexdigest():
        raise RuntimeError("Infrastructure changed after prepare; run prepare again")
    if not (ROOT / "frontend/dist/index.html").exists():
        raise RuntimeError("Frontend is not built; run prepare again")
    return plan


def deploy(session, args):
    # No implicit resource deployment in prepare.
    plan = read_prepared(session, args)
    login = load_login(args.credentials_file)
    cfn = session.client("cloudformation")
    foundation = apply_stack(cfn, args.project + "-foundation",
                             (ROOT / "infra/foundation.json").read_text())
    secrets = session.client("secretsmanager")
    sys.path.insert(0, str(ROOT))
    from guardrails_bench.providers import load_key
    key = load_key(args.key_file)
    current_key = secrets.get_secret_value(SecretId=foundation["DeciderSecretArn"])["SecretString"]
    if current_key != key:
        secrets.put_secret_value(SecretId=foundation["DeciderSecretArn"], SecretString=key)
    previous_auth = secrets.get_secret_value(SecretId=foundation["DemoSecretArn"])["SecretString"]
    auth_config = runtime_config(login, previous_auth)
    if auth_config != previous_auth:
        secrets.put_secret_value(SecretId=foundation["DemoSecretArn"], SecretString=auth_config)
    # Fail before publishing if a runtime credential accidentally enters the static build.
    for path in (ROOT / "frontend/dist").rglob("*"):
        if path.is_file() and any(value.encode() in path.read_bytes() for value in (key, login["password"])):
            raise RuntimeError("A runtime credential was found in the frontend build")
    ecr = session.client("ecr")
    tag = plan["image_tag"]
    exists = False
    try:
        ecr.describe_images(repositoryName=foundation["RepositoryName"], imageIds=[{"imageTag": tag}])
        exists = True
    except ecr.exceptions.ImageNotFoundException:
        pass
    if not exists:
        auth = ecr.get_authorization_token()["authorizationData"][0]
        username, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
        run(["docker", "login", "--username", username, "--password-stdin",
             auth["proxyEndpoint"]], input=password.encode(), stdout=subprocess.DEVNULL)
        image = foundation["RepositoryUri"] + ":" + tag
        run(["docker", "tag", f"{args.project}:{tag}", image])
        run(["docker", "push", image])
    image_info = ecr.describe_images(
        repositoryName=foundation["RepositoryName"], imageIds=[{"imageTag": tag}]
    )["imageDetails"][0]
    image_uri = foundation["RepositoryUri"] + "@" + image_info["imageDigest"]
    previous_stack = get_stack(cfn, args.project + "-app")
    previous_image = next((p["ParameterValue"] for p in previous_stack.get("Parameters", [])
                           if p["ParameterKey"] == "ImageUri"), None) if previous_stack else None
    app_outputs = apply_stack(cfn, args.project + "-app", generate_template(), {
        **plan["network"],
        "ImageUri": image_uri, "DeciderSecretArn": foundation["DeciderSecretArn"],
        "DemoSecretArn": foundation["DemoSecretArn"], "DeciderEndpoint": args.endpoint,
        "AvailabilityZoneA": plan["availability_zones"][0],
        "AvailabilityZoneB": plan["availability_zones"][1],
        "CloudFrontPrefixListId": plan["cloudfront_prefix_list"],
    })
    # Secret-only changes need a task restart to refresh injected configuration.
    if (current_key != key or auth_config != previous_auth) and previous_image == image_uri:
        ecs = session.client("ecs")
        print("Refreshing ECS runtime credentials...", flush=True)
        ecs.update_service(cluster=app_outputs["Cluster"], service=app_outputs["Service"],
                           forceNewDeployment=True)
        ecs.get_waiter("services_stable").wait(cluster=app_outputs["Cluster"],
                                               services=[app_outputs["Service"]])
    s3 = session.client("s3")
    dist = ROOT / "frontend/dist"
    files = sorted((p for p in dist.rglob("*") if p.is_file()),
                   key=lambda p: p.name == "index.html")  # Publish entrypoint last.
    for path in files:
        key_name = path.relative_to(dist).as_posix()
        s3.put_object(
            Bucket=app_outputs["FrontendBucket"], Key=key_name, Body=path.read_bytes(),
            ContentType=mimetypes.guess_type(key_name)[0] or "application/octet-stream",
            CacheControl="public,max-age=31536000,immutable" if key_name.startswith("assets/")
            else "no-cache,max-age=0,must-revalidate",
        )
    session.client("cloudfront").create_invalidation(
        DistributionId=app_outputs["DistributionId"],
        InvalidationBatch={"CallerReference": str(time.time_ns()),
                           "Paths": {"Quantity": 1, "Items": ["/*"]}},
    )
    state = {**plan, **app_outputs, "foundation": foundation, "image_uri": image_uri,
             "deployed_at": datetime.now(timezone.utc).isoformat()}
    write_private(STATE / "deployment.json", json.dumps(state, indent=2) + "\n")
    print("Website:", state["WebsiteUrl"], flush=True)
    print("Demo username/password saved locally:", args.credentials_file, flush=True)
    print("Run scripts/smoke_demo.py to verify access controls and scenarios.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "deploy", "status"])
    parser.add_argument("--project", default="decider-guardrails-lab")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--account", help="Expected AWS account ID")
    parser.add_argument("--vpc-id", help="Existing VPC with internet gateway; defaults to the default VPC")
    parser.add_argument("--key-file", type=Path, default=ROOT / "DeciderKey.txt")
    parser.add_argument("--credentials-file", type=Path, default=STATE / "demo-login.json")
    parser.add_argument("--endpoint", default="https://d1e9udi8st2p3g.cloudfront.net/v1/systemone")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,25}", args.project):
        parser.error("--project must be 3–26 lowercase letters, digits or hyphens")
    session = boto3.Session(region_name=args.region)
    if args.command == "prepare":
        prepare(session, args)
    elif args.command == "deploy":
        deploy(session, args)
    else:
        cfn = session.client("cloudformation")
        for name in (args.project + "-foundation", args.project + "-app"):
            stack = get_stack(cfn, name)
            print(name, stack["StackStatus"] if stack else "not deployed")
            if stack:
                print(json.dumps(outputs(stack), indent=2))


if __name__ == "__main__":
    main()
