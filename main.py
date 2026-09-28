"""
Screening Assist — API entrypoint.

Run:
  uvicorn main:app --host 0.0.0.0 --port 8000

Configuration is loaded exclusively from environment variables / .env
(see .env.example). Hardcoded credentials are not used.
"""

from app.factory import create_app

app = create_app()
