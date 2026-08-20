"""One-time setup: create the three AutonomyGate DynamoDB tables (on-demand billing).

Usage:  python scripts/create_tables.py   (uses AWS_REGION / default credentials)
"""
import os

import boto3

PREFIX = os.environ.get("AUTONOMYGATE_TABLE_PREFIX", "autonomygate")
REGION = os.environ.get("AWS_REGION", "us-east-1")

ddb = boto3.client("dynamodb", region_name=REGION)

TABLES = [
    {
        "TableName": f"{PREFIX}-audit",
        "AttributeDefinitions": [
            {"AttributeName": "action_id", "AttributeType": "S"},
            {"AttributeName": "session_id", "AttributeType": "S"},
        ],
        "KeySchema": [{"AttributeName": "action_id", "KeyType": "HASH"}],
        "GlobalSecondaryIndexes": [{
            "IndexName": "session-index",
            "KeySchema": [{"AttributeName": "session_id", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        "BillingMode": "PAY_PER_REQUEST",
    },
    {
        "TableName": f"{PREFIX}-tickets",
        "AttributeDefinitions": [
            {"AttributeName": "ticket_id", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
        ],
        "KeySchema": [{"AttributeName": "ticket_id", "KeyType": "HASH"}],
        "GlobalSecondaryIndexes": [{
            "IndexName": "status-index",
            "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        "BillingMode": "PAY_PER_REQUEST",
    },
    {
        "TableName": f"{PREFIX}-calib",
        "AttributeDefinitions": [{"AttributeName": "action_type", "AttributeType": "S"}],
        "KeySchema": [{"AttributeName": "action_type", "KeyType": "HASH"}],
        "BillingMode": "PAY_PER_REQUEST",
    },
    {
        "TableName": f"{PREFIX}-ledger",
        "AttributeDefinitions": [
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        "KeySchema": [
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        "BillingMode": "PAY_PER_REQUEST",
    },
]

for spec in TABLES:
    name = spec["TableName"]
    try:
        ddb.create_table(**spec)
        print(f"creating {name} ...")
        ddb.get_waiter("table_exists").wait(TableName=name)
        print(f"  {name} ready")
    except ddb.exceptions.ResourceInUseException:
        print(f"  {name} already exists")

try:
    ddb.update_time_to_live(
        TableName=f"{PREFIX}-ledger",
        TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires_at"},
    )
    print(f"  {PREFIX}-ledger TTL enabled on expires_at")
except ddb.exceptions.ClientError as exc:
    print(f"  {PREFIX}-ledger TTL: {exc.response['Error']['Code']}")

print("done.")
