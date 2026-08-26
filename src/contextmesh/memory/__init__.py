"""Session memory: harvest knowledge from finished sessions, recall it into new ones.

This is the half of ContextMesh that compression cannot substitute for. A
compressor makes the current session cheaper; memory stops the next session
from rediscovering what this one already learned -- including the things
that did not work.
"""
