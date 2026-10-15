"""Collaboration layer: the structured, authenticated channel through which a reasoning/coding AI (the collaborator) delegates work to this local agent.

It is provider-neutral (no model is imported here), capability-based (callers name a registered capability, never an engine action or a path) and
persistent (every request, result, piece of evidence, artifact, approval and audit line lives in <data dir>/collab.db). See COLLAB.md.
"""
PROTOCOL = "octg-collab/1"
