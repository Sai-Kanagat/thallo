"""Every closed vocabulary in the system.

Adding a value is an additive change any package may request from the lead.
Renaming or removing a value is a breaking change (docs/contracts.md).
Values are stored in Postgres as text with CHECK constraints that mirror these.
"""
from __future__ import annotations

from enum import IntEnum, StrEnum


class Capability(StrEnum):
    """The 16 outside capabilities from BUILD_PLAN 2.1b. The core calls these, never a vendor."""

    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    TRANSCRIPTION = "transcription"
    VOICEOVER = "voiceover"
    PUBLISHING = "publishing"
    CALENDAR = "calendar"
    CRM = "crm"
    MESSAGING = "messaging"
    BOOKING = "booking"
    ANALYTICS = "analytics"
    ADS = "ads"
    RESEARCH = "research"
    STORAGE = "storage"
    NOTIFICATIONS = "notifications"
    AUTOMATION_HOOKS = "automation_hooks"


class ConnectionMode(StrEnum):
    MANUAL = "manual"
    LOCAL = "local"
    HOSTED = "hosted"
    MOCK = "mock"


class TextRole(StrEnum):
    """Which job a text model is doing. Model choice is per role."""

    PLAN = "plan"
    BRIEF = "brief"
    DRAFT = "draft"
    EXTRACT = "extract"
    CLASSIFY = "classify"
    GRADE = "grade"
    SECOND_READ = "second_read"
    MEMO = "memo"


class Language(StrEnum):
    EN = "en"
    IT = "it"
    TA = "ta"


class Channel(StrEnum):
    LINKEDIN_PERSONAL = "linkedin_personal"
    LINKEDIN_COMPANY = "linkedin_company"
    FACEBOOK = "facebook"
    INSTAGRAM = "instagram"
    GOOGLE_BUSINESS_PROFILE = "google_business_profile"
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    NEWSLETTER = "newsletter"
    WEBSITE = "website"
    YOUTUBE = "youtube"
    SEARCH_ADS = "search_ads"
    SOCIAL_ADS = "social_ads"


class FunnelStage(StrEnum):
    AWARENESS = "awareness"
    CONSIDERATION = "consideration"
    CONVERSION = "conversion"
    RETENTION = "retention"


class RateTag(StrEnum):
    """Growth model rates are either an owner's guess or measured from 30+ outcomes."""

    GUESSED = "guessed"
    MEASURED = "measured"


class ClaimKind(StrEnum):
    NUMBER = "number"
    PRICE = "price"
    DATE = "date"
    NAME = "name"
    QUOTE = "quote"
    CAPABILITY = "capability"
    OFFER = "offer"  # a time-limited price or promotion; expires in 30 days by default


class SourceKind(StrEnum):
    VOICE_ANSWER = "voice_answer"
    INTERVIEW = "interview"
    DOCUMENT = "document"
    WEB_PAGE = "web_page"
    SELLER_NOTE = "seller_note"
    TYPED = "typed"
    RESEARCH = "research"  # untrusted: may only suggest claim candidates


class MediaKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    PDF = "pdf"


class MediaProvenance(StrEnum):
    CLIENT_PHOTO = "client_photo"
    CLIENT_CLIP = "client_clip"
    CLIENT_GRAPHIC = "client_graphic"
    TEMPLATE_RENDER = "template_render"
    GENERATED_BACKGROUND = "generated_background"
    GENERATED = "generated"  # people, products, premises: off by default
    PERSON_RECORDING = "person_recording"  # someone recorded it themselves (manual voiceover, voice note)
    PERSON_FILE = "person_file"  # a person made or uploaded the finished file


class CheckLayer(StrEnum):
    """Order is fixed: code checks, then graders, then the second reader."""

    CODE = "code"
    GRADER = "grader"
    SECOND_READER = "second_reader"


class CheckVerdict(StrEnum):
    PASS = "pass"
    FLAG = "flag"
    FAIL = "fail"


class ApprovalSubject(StrEnum):
    BUNDLE = "bundle"
    SPEND_PROPOSAL = "spend_proposal"
    TEMPLATE = "template"
    TRUST_LEVEL = "trust_level"  # subject is an autonomy_state row
    RULE_PROPOSAL = "rule_proposal"


class ApprovalMethod(StrEnum):
    PER_BUNDLE = "per_bundle"
    EXPRESS_LIST = "express_list"
    TEMPLATE_INSTANCE = "template_instance"


class RejectionReason(StrEnum):
    OFF_VOICE = "off_voice"
    GENERIC = "generic"
    WRONG_FACT = "wrong_fact"
    WEAK_VISUAL = "weak_visual"
    WRONG_TIMING = "wrong_timing"
    NOT_INTERESTING = "not_interesting"
    OTHER = "other"


class EditKind(StrEnum):
    WORD_SWAP = "word_swap"
    CUT = "cut"
    TONE_SHIFT = "tone_shift"
    STRUCTURE_CHANGE = "structure_change"
    FACT_FIX = "fact_fix"
    TYPO = "typo"  # logged, never counts as a defect


class RuleScope(StrEnum):
    PILLAR = "pillar"
    CHANNEL = "channel"
    BRAND = "brand"


class TouchKind(StrEnum):
    LINK = "link"
    WHATSAPP_CODE = "whatsapp_code"
    FORM = "form"
    CALL = "call"
    WALK_IN_CODE = "walk_in_code"
    SELF_REPORT = "self_report"


class ConsentBasis(StrEnum):
    """Placeholder vocabulary. What each basis permits lives in region_policy rows a lawyer fills."""

    OPT_IN = "opt_in"
    ENQUIRY = "enquiry"
    EXISTING_CUSTOMER = "existing_customer"
    NONE = "none"


class LeadRoute(StrEnum):
    HOT = "hot"  # high fit, hot: brief and drafted reply to the seller, clock starts
    WARM = "warm"  # high fit, warm: approved nurture sequence
    LOW_FIT = "low_fit"  # no outreach, kept for reporting


class MessageKind(StrEnum):
    ACKNOWLEDGEMENT = "acknowledgement"
    QUALIFICATION = "qualification"
    NURTURE = "nurture"
    REMINDER = "reminder"
    REPLY_DRAFT = "reply_draft"  # always sent by a human from their own account


class MessageChannel(StrEnum):
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    SMS = "sms"


class OutcomeKind(StrEnum):
    WON = "won"
    LOST = "lost"


class LossReason(StrEnum):
    """Starting list, to be confirmed by Sai with each pilot."""

    PRICE = "price"
    FINANCE_REFUSED = "finance_refused"
    CHOSE_COMPETITOR = "chose_competitor"
    NO_BUDGET = "no_budget"
    TIMING = "timing"
    NO_RESPONSE = "no_response"
    NOT_A_FIT = "not_a_fit"
    OTHER = "other"


class AutonomyLevel(IntEnum):
    L0_SHADOW = 0
    L1_FULL_REVIEW = 1
    L2_BUNDLE_REVIEW = 2
    L3_EXPRESS_LIST = 3
    L4_TEMPLATE_INSTANCES = 4


class MetricDay(IntEnum):
    D2 = 2
    D7 = 7
    D14 = 14


class ActorKind(StrEnum):
    HUMAN = "human"
    SYSTEM = "system"
    AGENT = "agent"


class HumanOnlyAction(StrEnum):
    """BUILD_PLAN 2.8: these require a named human. No agent or API caller may perform them."""

    APPROVE = "approve"
    VERIFY_CLAIM = "verify_claim"
    SIGN_SPEND = "sign_spend"
    RAISE_TRUST_LEVEL = "raise_trust_level"
    SIGN_TEMPLATE = "sign_template"
    FIRST_REPLY = "first_reply"  # first reply to a named person, and all negotiation
    PUBLIC_REPLY = "public_reply"  # replies to comments, complaints and reviews
    NEW_CAMPAIGN_OR_AUDIENCE = "new_campaign_or_audience"
    CONSENT_CALL = "consent_call"


class UserRole(StrEnum):
    OPERATOR = "operator"
    FOUNDER = "founder"
    APPROVER = "approver"
    SELLER = "seller"
    STAFF = "staff"


class CampaignRole(StrEnum):
    """Channel roles from BUILD_PLAN 2.6, each with its own target multiple of A."""

    BRAND_SEARCH = "brand_search"
    HIGH_INTENT_SEARCH = "high_intent_search"
    LOCAL_SOCIAL = "local_social"
    BOOST = "boost"
    RETARGETING = "retargeting"
    ACCOUNT_LIST = "account_list"  # LinkedIn to a named account list (B2B)


class BreakerScope(StrEnum):
    RUN = "run"
    DAY = "day"


class IncidentSeverity(StrEnum):
    LOW = "low"
    HIGH = "high"
    HARD_RULE = "hard_rule"
