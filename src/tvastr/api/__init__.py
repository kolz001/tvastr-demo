"""FastAPI surface for tvastr — health checks and on-demand pipeline runs."""

from tvastr.api.app import create_app

__all__ = ["create_app"]
