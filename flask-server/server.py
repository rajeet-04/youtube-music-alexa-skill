"""Compatibility entry point: ``waitress-serve server:app`` exports the JUKES app."""

from jukes.app import create_app

app = create_app()
