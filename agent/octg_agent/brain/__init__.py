"""Orchestration layer: task state, observation, context, approval and the observe→reason→act→verify loop.

It drives the browser ONLY through `engine.dispatcher.Dispatcher.execute()` and reads the same registry (`engine.registry`) the CLI uses;
nothing in `engine/`, `bridge/` or `files/` imports this package.
"""
