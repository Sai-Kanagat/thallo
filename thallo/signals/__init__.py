"""Signal intake (WP-16, BUILD_PLAN module 6): pull what happened on each connected source each
night, keep the raw pull, and store every metric as a value or as missing (never as zero).

    context.py   what the intake stages for a provider call (uploads, touch reader, raw replies)
    intake.py    run one pull, check its shape, write `signal_pull` and `signal` rows
    touches.py   the own-logs reader over the tenant touch table
"""
