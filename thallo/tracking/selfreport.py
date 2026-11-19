"""The self-report question: "How did you hear about us?". The third independent signal next to the
link and the code. Answers are a fixed option key; free text is never stored here (untrusted text
belongs only in `untrusted_data`)."""
from __future__ import annotations

from thallo.contracts.enums import Language

QUESTION = {
    Language.EN: "How did you hear about us?",
    Language.IT: "Come ci hai conosciuto?",
}

# key: (English label, Italian label). Order is the order shown.
OPTIONS: dict[str, tuple[str, str]] = {
    "instagram": ("Instagram", "Instagram"),
    "facebook": ("Facebook", "Facebook"),
    "tiktok": ("TikTok", "TikTok"),
    "linkedin": ("LinkedIn", "LinkedIn"),
    "google_search": ("Google search", "Ricerca Google"),
    "google_maps": ("Google Maps", "Google Maps"),
    "whatsapp": ("WhatsApp", "WhatsApp"),
    "friend": ("A friend or colleague", "Un amico o un collega"),
    "walked_past": ("Walked past", "Passando davanti"),
    "flyer": ("A flyer or poster", "Un volantino o un manifesto"),
    "event": ("An event", "Un evento"),
    "other": ("Other", "Altro"),
}


def question(language: Language) -> dict:
    """The question as data for a form or a seller's call script."""
    lang = language if language in QUESTION else Language.EN
    idx = 1 if lang is Language.IT else 0
    return {"field": "how_heard", "label": QUESTION[lang],
            "options": [{"value": k, "label": v[idx]} for k, v in OPTIONS.items()]}


def normalise_answer(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    key = raw.strip().lower()
    return key if key in OPTIONS else None


__all__ = ["OPTIONS", "QUESTION", "normalise_answer", "question"]
