"""Newsletter (WP-29, BUILD_PLAN module 31).

    issue.py    pieces that beat their forecast in, one issue variant out, left for the gate
    send.py     subscribers, the one human-approved send path, unsubscribe
    records.py  newsletter_issue, newsletter_subscriber, newsletter_delivery (migration 0040)
"""
from thallo.extensions.newsletter.issue import NO_WINNERS, IssueOutcome, Pick, draft_issue, winners, write_issue
from thallo.extensions.newsletter.records import NewsletterDelivery, NewsletterIssue, NewsletterSubscriber
from thallo.extensions.newsletter.send import (
    SendRefused,
    SendReport,
    confirm_sent,
    footer,
    send_issue,
    subscribe,
    unsubscribe,
)

__all__ = ["NO_WINNERS", "IssueOutcome", "NewsletterDelivery", "NewsletterIssue", "NewsletterSubscriber", "Pick",
           "SendRefused", "SendReport", "confirm_sent", "draft_issue", "footer", "send_issue", "subscribe",
           "unsubscribe", "winners", "write_issue"]
