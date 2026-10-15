"""Typed aiohttp application keys (avoids string-key warnings and import cycles)."""
from aiohttp import web

RT = web.AppKey("octg_runtime", object)
