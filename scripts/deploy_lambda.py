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
ROOT = Path(__file__).parent.parent
BUILD = ROOT / "build_lambda"

MANAGED_POLICIES = [
    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    "arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess",
    "arn:aws:iam::aws:policy/AmazonBedrockFullAccess",
]

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
    return arn


def deploy(lam, role_arn: str, package: bytes) -> None:
    env = {"Variables": {
        "AUTONOMYGATE_STORAGE": "dynamo",
        "AUTONOMYGATE_AGENT": "groq",
    }}
    try:
        lam.get_function(FunctionName=FUNC_NAME)
        print("updating function code ...")
        lam.update_function_code(FunctionName=FUNC_NAME, ZipFile=package)
        waiter = lam.get_waiter("function_updated_v2")
        waiter.wait(FunctionName=FUNC_NAME)
    except lam.exceptions.ResourceNotFoundException:
        print("creating function ...")
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
