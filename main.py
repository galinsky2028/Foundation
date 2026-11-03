"""Entry point: uvicorn main:app"""
from foundation.web import create_app

app = create_app()
