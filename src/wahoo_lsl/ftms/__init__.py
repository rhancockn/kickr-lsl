"""FTMS power control for the Wahoo KICKR (spec 2026-10-21).

This package's __init__ and client.py import only the standard library, so the client can
be used from environments without bleak or pylsl.
"""

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 47610
