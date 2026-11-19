"""Link and touch tracking (WP-12, BUILD_PLAN module 14 and 2.6).

Ties every click, chat, call, form and walk-in back to the variant or ad that caused it, with
three independent signals: the tracked link, a code, and the self-report answer.

    links.py         mint tracked links, WhatsApp links with reference codes, QR and offer codes
    capture.py       each entry point becomes a `touch`
    redirector.py    the public `<prefix><slug>` redirect: stored destination only, no cookie, no IP
    wiring.py        the redirector's narrow database role and the click recorder
    selfreport.py    the fixed "how did you hear about us" question
    report.py        the weekly share of leads with no source
"""
