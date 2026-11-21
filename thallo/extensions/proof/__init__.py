"""Proof, referral and customer voice (WP-28, module 29). See `service.py` for the rules."""
from thallo.extensions.proof.records import (
    AskKind,
    BuyerNote,
    MaterialKind,
    PieceKind,
    ProofAsk,
    ProofFlag,
    ProofGrant,
    ProofMaterial,
    ProofPiece,
    Use,
)
from thallo.extensions.proof.service import Proof, ProofError, ask_text, consent_live
from thallo.extensions.proof.voice import brief_notes, extract_notes, phrases

__all__ = ["AskKind", "BuyerNote", "MaterialKind", "PieceKind", "Proof", "ProofAsk", "ProofError", "ProofFlag",
           "ProofGrant", "ProofMaterial", "ProofPiece", "Use", "ask_text", "brief_notes", "consent_live",
           "extract_notes", "phrases"]
