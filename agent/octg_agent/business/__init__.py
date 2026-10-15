"""Business Brain: a structured, user-approved source of business facts and authorized documents for the task loop.

Layering: this package imports only the standard library, `octg_agent.sensitive` and `octg_agent.files.authorization`. The browser engine,
bridge, files and model layers never import it (tests/test_isolation.py enforces both directions). The task loop reaches it only through
`BrainReader`; writes exist only on `BrainAdmin`, which is wired to the local CLI and the authenticated side panel, never to a model.
"""
