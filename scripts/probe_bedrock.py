"""Probe Bedrock for a working Claude model ID (one tiny call per candidate)."""
import boto3

REGION = "us-east-1"
CANDIDATES = [
    "us.amazon.nova-2-lite-v1:0",
    "amazon.nova-2-lite-v1:0",
    "us.amazon.nova-pro-v1:0",
    "amazon.nova-pro-v1:0",
]

client = boto3.client("bedrock-runtime", region_name=REGION)
for model_id in CANDIDATES:
    try:
        resp = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "Reply with exactly: OK"}]}],
            inferenceConfig={"maxTokens": 10},
        )
        text = resp["output"]["message"]["content"][0]["text"]
        print(f"WORKS: {model_id} -> {text!r}")
        break
    except Exception as exc:
        print(f"FAIL:  {model_id} -> {type(exc).__name__}: {str(exc)[:160]}")
