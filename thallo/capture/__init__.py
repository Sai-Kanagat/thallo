"""Source capture (WP-10): weekly questions, voice answers, the long interview, shot lists.

- `records`: the capture tables (migration 0026).
- `questions`: the week's three questions and shot list per play.
- `service`: consent, recordings, typed answers, transcription through the registry, shots.
- `candidates`: numbers in transcripts become claim candidates, never verified claims.
"""
from thallo.capture.candidates import extract_candidates, find_numbers, parse_number
from thallo.capture.records import (
    AnswerMode,
    CaptureAnswer,
    CaptureQuestion,
    ShotRequest,
    ShotState,
    TranscriptState,
)

__all__ = [
    "AnswerMode", "CaptureAnswer", "CaptureQuestion", "ShotRequest", "ShotState", "TranscriptState",
    "extract_candidates", "find_numbers", "parse_number",
]
