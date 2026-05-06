"""Gunicorn entrypoint: `gunicorn -b 127.0.0.1:5000 wsgi:app`."""
from starboard.app import create_app

app = create_app()
