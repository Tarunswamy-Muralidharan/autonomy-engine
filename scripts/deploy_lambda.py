"""One-command AWS Lambda deployment for AutonomyGate.

Builds a linux-compatible dependency package, zips app + deps, creates (or
updates) the execution role, the Lambda function, and a public Function URL.

Usage:
    python scripts/deploy_lambda.py          # first deploy + redeploys
Never touches secrets: set GROQ_API_KEY yourself afterwards (one CLI command
printed at the end).
"""
from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import boto3

REGION = "us-east-1"
FUNC_NAME = "autonomygate"
ROLE_NAME = "autonomygate-lambda-role"
RUNTIME = "python3.12"
PREFIX = os.environ.get("AUTONOMYGATE_TABLE_PREFIX", "autonomygate")
ROOT = Path(__file__).parent.parent
BUILD = ROOT / "build_lambda"

MANAGED_POLICIES = [
    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
]

# Least privilege: only the three AutonomyGate tables (+ their indexes) and
# model invocation. The AWS-managed *FullAccess policies would have granted
# dynamodb:* on every table in the account, including DeleteTable.
def _inline_policy(account: str) -> dict:
    table_arns = [f"arn:aws:dynamodb:{REGION}:{account}:table/{PREFIX}-{t}"
                  for t in ("audit", "tickets", "calib", "ledger")]
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow",
             "Action": ["dynamodb:GetItem", "dynamodb:PutItem",
                        "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:Scan"],
             "Resource": table_arns + [a + "/index/*" for a in table_arns]},
            {"Effect": "Allow",
             "Action": ["bedrock:InvokeModel", "bedrock:Converse"],
             "Resource": "*"},
            # Read-only PII inspection for the audit redactor. Comprehend has
            # no resource-level ARNs for these calls, so "*" is the tightest
            # grant available; both actions are analysis-only.
            {"Effect": "Allow",
             "Action": ["comprehend:DetectPiiEntities",
                        "comprehend:ContainsPiiEntities"],
             "Resource": "*"},
        ],
    }

TRUST = {
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow",
                   "Principal": {"Service": "lambda.amazonaws.com"},
                   "Action": "sts:AssumeRole"}],
}


def build_package() -> bytes:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir()
    print("installing linux wheels ...")
    subprocess.run([
        sys.executable, "-m", "pip", "install",
        "--platform", "manylinux2014_x86_64",
        "--implementation", "cp", "--python-version", "3.12",
        "--only-binary=:all:", "--upgrade", "--quiet",
        "--target", str(BUILD),
        "-r", str(ROOT / "requirements-lambda.txt"),
    ], check=True)

    print("zipping ...")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for base in [BUILD]:
            for p in base.rglob("*"):
                if p.is_file() and "__pycache__" not in p.parts:
                    z.write(p, p.relative_to(base))
        for folder in ["app", "static"]:
            for p in (ROOT / folder).rglob("*"):
                if p.is_file() and "__pycache__" not in p.parts:
                    z.write(p, p.relative_to(ROOT))
        z.write(ROOT / "lambda_function.py", "lambda_function.py")
    data = buf.getvalue()
    print(f"package size: {len(data) / 1e6:.1f} MB")
    return data


def ensure_role(iam) -> str:
    try:
        arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
        print(f"role exists: {arn}")
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(TRUST),
            Description="AutonomyGate Lambda execution role",
        )["Role"]["Arn"]
        print(f"role created: {arn}; waiting for propagation ...")
        time.sleep(12)
    attached = {p["PolicyArn"] for p in
                iam.list_attached_role_policies(RoleName=ROLE_NAME)["AttachedPolicies"]}
    for policy in MANAGED_POLICIES:
        if policy not in attached:
            iam.attach_role_policy(RoleName=ROLE_NAME, PolicyArn=policy)
            print(f"attached {policy.split('/')[-1]}")

    # Drop over-broad managed policies from earlier deployments.
    for legacy in ("arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess",
                   "arn:aws:iam::aws:policy/AmazonBedrockFullAccess"):
        if legacy in attached:
            iam.detach_role_policy(RoleName=ROLE_NAME, PolicyArn=legacy)
            print(f"detached over-broad {legacy.split('/')[-1]}")

    account = arn.split(":")[4]
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="AutonomyGateLeastPrivilege",
                        PolicyDocument=json.dumps(_inline_policy(account)))
    print("applied least-privilege inline policy")
    return arn


def deploy(lam, role_arn: str, package: bytes) -> None:
    env = {"Variables": {
        "AUTONOMYGATE_STORAGE": "dynamo",
        "AUTONOMYGATE_AGENT": "groq",
        "AUTONOMYGATE_PII": "comprehend",
    }}
    try:
        current = lam.get_function(FunctionName=FUNC_NAME)
        print("updating function code ...")
        lam.update_function_code(FunctionName=FUNC_NAME, ZipFile=package)
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNC_NAME)

        # Reconcile configuration on every deploy, preserving existing
        # secrets. update_function_code alone never corrects config drift,
        # and a reviewer token that is never set leaves the review plane open.
        existing = current["Configuration"].get("Environment", {}).get("Variables", {})
        merged = {**env["Variables"], **existing}
        merged.setdefault("AUTONOMYGATE_REVIEWER_TOKEN", secrets.token_urlsafe(32))
        lam.update_function_configuration(
            FunctionName=FUNC_NAME, Timeout=120, MemorySize=1024,
            Environment={"Variables": merged})
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNC_NAME)
    except lam.exceptions.ResourceNotFoundException:
        print("creating function ...")
        env["Variables"]["AUTONOMYGATE_REVIEWER_TOKEN"] = secrets.token_urlsafe(32)
        for attempt in range(6):
            try:
                lam.create_function(
                    FunctionName=FUNC_NAME, Runtime=RUNTIME,
                    Role=role_arn, Handler="lambda_function.handler",
                    Code={"ZipFile": package}, Timeout=120, MemorySize=1024,
                    Environment=env,
                    Description="AutonomyGate - Graduated Autonomy Engine (Aivar PS-9.1)",
                )
                break
            except lam.exceptions.InvalidParameterValueException as exc:
                if "role" in str(exc).lower() and attempt < 5:
                    print("  role not ready yet, retrying in 10s ...")
                    time.sleep(10)
                else:
                    raise
        lam.get_waiter("function_active_v2").wait(FunctionName=FUNC_NAME)
    print("function ready.")


def ensure_url(lam) -> str:
    try:
        url = lam.get_function_url_config(FunctionName=FUNC_NAME)["FunctionUrl"]
    except lam.exceptions.ResourceNotFoundException:
        url = lam.create_function_url_config(
            FunctionName=FUNC_NAME, AuthType="NONE")["FunctionUrl"]
        try:
            lam.add_permission(
                FunctionName=FUNC_NAME, StatementId="public-url",
                Action="lambda:InvokeFunctionUrl", Principal="*",
                FunctionUrlAuthType="NONE")
        except lam.exceptions.ResourceConflictException:
            pass
    return url


def main() -> None:
    session = boto3.Session(region_name=REGION)
    iam, lam = session.client("iam"), session.client("lambda")
    role_arn = ensure_role(iam)
    package = build_package()
    deploy(lam, role_arn, package)
    url = ensure_url(lam)
    print("\n=== DEPLOYED ===")
    print(f"URL: {url}")
    print("\nNext (run yourself, pasting your Groq key):")
    print(f'  aws lambda update-function-configuration --function-name {FUNC_NAME} '
          f'--region {REGION} --environment "Variables={{AUTONOMYGATE_STORAGE=dynamo,'
          f'AUTONOMYGATE_AGENT=groq,GROQ_API_KEY=<PASTE_YOUR_KEY>}}"')


if __name__ == "__main__":
    main()
