"""Shared cron-failure alerting (step-1-brief.md SS10.3). Three hourly jobs
(drip_cron.py, migrate_step1b.py, dedupe_detector.py) run unattended, each
writing to a log nobody reads by default -- exactly the shape of the
deliverability.py permission bug that ran broken for 35 days behind a silent
except. jobs/blacklist.py already established the pattern (email_helper.send_email
on a real finding); this is the same mechanism for "the job didn't run at all."

One implementation, all three crons import it, rather than three copies that
drift (the failure mode step-1-brief.md SS8.2/SS9.3 already flagged once).
"""
import sys
import traceback

ALERT_TO = "eric@expertseoconsulting.com"


def alert_and_exit(job_name: str, exc: Exception):
    tb = traceback.format_exc()
    print(f"FATAL in {job_name}: {exc}\n{tb}")
    try:
        from email_helper import send_email
        send_email(
            ALERT_TO,
            f"ALERT: {job_name} cron failed",
            f"{job_name} crashed and did not complete this run:\n\n{tb}\n"
            f"\nCheck its log under reports/ for context.",
        )
    except Exception as alert_err:
        print("alert email failed:", alert_err)
    sys.exit(1)
