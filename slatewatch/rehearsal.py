"""Compatibility spelling for the offline silence rehearsal machinery."""
from .rehearse import (
    CACHE_LIMIT,
    RELATION_LABELS,
    RUN_LIMIT,
    SESSION_LIMIT,
    Session,
    RehearsalCache,
    describe,
    explain,
    prepare,
    rehearse,
    silence_hits,
)

__all__ = [
    'CACHE_LIMIT', 'RELATION_LABELS', 'RUN_LIMIT', 'SESSION_LIMIT',
    'Session', 'RehearsalCache', 'describe', 'explain', 'prepare',
    'rehearse', 'silence_hits',
]
