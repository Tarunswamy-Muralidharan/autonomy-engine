"""AWS Lambda entrypoint: wraps the FastAPI app with Mangum (ASGI adapter)."""
from mangum import Mangum

from app.main import app

handler = Mangum(app)
