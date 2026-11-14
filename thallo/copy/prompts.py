"""The writer's instructions. Only trusted text goes here: our fixed rules, the brief (an internal
row), the brand's approved voice rules and the claim codes. Source material, earlier drafts and
compile findings (which quote model output) travel as untrusted data, never in these strings."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from thallo.contracts.enums import Channel, ClaimKind, Language
from thallo.contracts.models import Brief, Claim
from thallo.providers.text.base import LANGUAGE_NAMES
from thallo.truth import CompileResult

PROMPT_VERSION = "copy.draft/1"

ANGLES = (
    "Open with the customer's situation or problem, in their words where the source has them.",
    "Open with one concrete detail from the source material: a thing, a place, a moment.",
    "Open with what the reader can do next, then give the reason.",
)

RULES = """Rules:
- Use only facts from the source material in the data and from the claims listed below. Invent nothing.
- Never type a digit, a number word, a price, a percentage, a date, a ranking, a superlative, a comparison, or \
the name of a customer or a person. When the piece needs one of the listed facts, write its slot exactly as \
shown, for example {{C-0012}}. Code fills the slot later.
- A slot already carries its unit and wording. Do not repeat the unit, and put no letter, sign, multiplier or \
dash directly next to a slot.
- Write in a plain, specific voice. No empty openers, no "not just X, it's Y" contrasts, no lists of three \
adjectives, no hype words.
- No em dashes, no emoji, no hashtags unless the brief asks for them.
- Output the text of the piece only: no title, no preface, no notes."""


def _mask(text: str) -> str:
    return "".join("#" if ch.isdigit() else ch for ch in text)


def claim_hint(claim: Claim, language: Language) -> str:
    """What a slot stands for, without the value: digits masked, names hidden."""
    kind = claim.kind
    if kind is ClaimKind.NAME:
        return "a name (never type it; use the slot)"
    if kind is ClaimKind.QUOTE:
        return "a customer quote (use the slot; do not paraphrase it)"
    wording = claim.languages.get(language.value)
    if wording is None:
        wording = claim.value + (f" {claim.unit}" if claim.unit else "")
    return f"{kind.value}, reads like: {_mask(wording)}"


def claims_block(claims: Mapping[str, Claim], allowed_ids: Sequence[str], language: Language) -> str:
    allowed = set(allowed_ids)
    lines = [f"{{{{{code}}}}}: {claim_hint(cl, language)}" for code, cl in sorted(claims.items()) if cl.id in allowed]
    body = "\n".join(lines) if lines else "None. Use no facts that need one."
    return "Claims you may use (as slots only):\n" + body


def brief_block(brief: Brief, channel: Channel, language: Language, max_chars: int | None,
                voice_rules: Sequence[str]) -> str:
    parts = [
        f"Channel: {channel.value}. Language: {LANGUAGE_NAMES[language]}.",
        f"Length: at most {max_chars} characters." if max_chars else "Length: as the channel suits, short.",
        "Brief:",
        f"- Audience: {brief.audience}",
        f"- Job to be done: {brief.job_to_be_done}",
        f"- Message: {brief.message}",
        f"- Call to action: {brief.call_to_action}",
    ]
    if voice_rules:
        parts.append("Voice rules for this brand:")
        parts += [f"- {r}" for r in voice_rules]
    return "\n".join(parts)


def draft_instructions(*, angle: int, brief: Brief, channel: Channel, language: Language, max_chars: int | None,
                       voice_rules: Sequence[str], claims: Mapping[str, Claim]) -> str:
    return "\n\n".join([
        f"You write one {channel.value} piece for a small business, from the brief below and the source material "
        "given as data.",
        RULES,
        f"Angle for this candidate: {ANGLES[angle % len(ANGLES)]}",
        brief_block(brief, channel, language, max_chars, voice_rules),
        claims_block(claims, brief.allowed_claim_ids, language),
    ])


def repair_instructions(*, brief: Brief, channel: Channel, language: Language, max_chars: int | None,
                        voice_rules: Sequence[str], claims: Mapping[str, Claim]) -> str:
    return "\n\n".join([
        f"You are fixing one {channel.value} piece for a small business. Its previous draft failed the compile "
        "check. The first data item is a JSON object with that draft and the findings; the rest is the source "
        "material. Rewrite the whole piece so that every finding is fixed and the rules below hold.",
        RULES,
        brief_block(brief, channel, language, max_chars, voice_rules),
        claims_block(claims, brief.allowed_claim_ids, language),
    ])


def findings_data(previous: str, result: CompileResult | None, fill_problems: Sequence[str]) -> str:
    """Untrusted: the draft and the findings quote model output."""
    findings = [] if result is None else [
        {"kind": f.kind.value, "severity": f.severity.value, "text": f.match, "why": f.message} for f in result.blocks]
    findings += [{"kind": "fill", "severity": "block", "why": p} for p in fill_problems]
    return json.dumps({"previous_draft": previous, "findings": findings}, ensure_ascii=False)
