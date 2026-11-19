"""The lead desk (WP-13): consent first, fit and heat as two numbers, drafts a person sends."""
from thallo.leads.desk import Desk, DeskError, body_hash, safe_link
from thallo.leads.touches import FakeTouchFeed, TouchEvent, TouchFeed

__all__ = ["Desk", "DeskError", "FakeTouchFeed", "TouchEvent", "TouchFeed", "body_hash", "safe_link"]
