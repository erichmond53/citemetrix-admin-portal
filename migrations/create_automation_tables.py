#!/usr/bin/env python3
"""Schema split, stage 1: create the new automation tables, empty and additive.

email-builder-spec-2026-09-21.md SS2-3 (Chat, from direct observation of the live
MailPoet UI): split the drip engine's fused content+timing (drip_campaigns/
drip_steps/drip_enrollments/drip_send_log) into a content library (emails) and
an orchestration graph (automations/automation_steps/automation_runs), so an
email can be reused/duplicated/referenced from more than one sequence. Full
staged plan in the approved plan doc -- this script is stage 1 only: create the
tables, touch nothing that reads or writes today's live drip_* tables.

Five new tables (emails, automations, automation_steps, automation_runs,
automation_run_events), plus two additive changes to existing tables:
  - drip_campaigns.migrated_automation_id -- the ownership handshake. NULL means
    the legacy engine still owns this campaign; once set, drip_cron.py's legacy
    walker excludes it and the new walker picks it up. Flipping this ONE column
    is the entire cutover for a campaign -- see stage 4/5/6.
  - drip_send_log gains run_key/email_id/node_key (nullable) so BOTH engines log
    sends to the same table. This is deliberate, not a shortcut: drip_send_log is
    where opened_at/clicked_at live, written by /api/drip/ses-webhook keyed on
    provider_msg_id -- the entire 2026-09 deliverability investigation's dataset.
    Renaming or replacing this table at cutover would risk that data; extending
    it in place means the webhook needs zero changes, ever, through the whole
    migration.

No `emails`/`automations`/etc. rows are created here -- that's stage 2
(migrations/backfill_automations.py, not yet written). Nothing today reads
these new tables or columns; drip_cron.py, leads_drip.py, and app.py are
completely unchanged by this script.

Idempotent (checks current state before creating/altering, safe to re-run).

Usage: venv/bin/python migrations/create_automation_tables.py [--dry-run]
"""
import os
import sys

DRY_RUN = "--dry-run" in sys.argv

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE, ".env"))
import pymysql


def admin_db():
    return pymysql.connect(
        host=os.getenv("ADMIN_DB_HOST", "localhost"), user=os.getenv("ADMIN_DB_USER", "adminportal"),
        password=os.getenv("ADMIN_DB_PASSWORD"), database=os.getenv("ADMIN_DB_NAME", "admin_portal"),
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
    )


def table_exists(cur, table):
    cur.execute(
        "SELECT COUNT(*) c FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = %s",
        (table,),
    )
    return cur.fetchone()["c"] > 0


def column_exists(cur, table, column):
    cur.execute(
        "SELECT COUNT(*) c FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
        (table, column),
    )
    return cur.fetchone()["c"] > 0


TABLES = {
    "emails": """
        CREATE TABLE emails (
          id                 INT AUTO_INCREMENT PRIMARY KEY,
          name               VARCHAR(255) NOT NULL,
          subject            VARCHAR(500) NOT NULL,
          body_text          MEDIUMTEXT NULL,
          body_html          MEDIUMTEXT NULL,
          design_json        LONGTEXT NULL,
          cta_text           VARCHAR(255) NULL,
          cta_key            VARCHAR(120) NULL,
          cta_kind           ENUM('check','book_demo') NOT NULL DEFAULT 'check',
          onpage_title       VARCHAR(500) NULL,
          onpage_copy        TEXT NULL,
          onpage_sync_status VARCHAR(40) NULL,
          onpage_sync_error  TEXT NULL,
          is_survey_step     TINYINT(1) NOT NULL DEFAULT 0,
          survey_epoch       VARCHAR(40) NULL,
          status             ENUM('draft','scheduled','active','completed','archived') NOT NULL DEFAULT 'draft',
          created_by         INT NULL,
          created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          legacy_step_id     INT NULL,
          UNIQUE KEY uq_emails_legacy_step (legacy_step_id),
          KEY idx_emails_status (status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "automations": """
        CREATE TABLE automations (
          id                       INT AUTO_INCREMENT PRIMARY KEY,
          name                     VARCHAR(255) NOT NULL,
          description              TEXT NULL,
          status                   ENUM('draft','active','inactive','archived') NOT NULL DEFAULT 'draft',
          run_once_per_subscriber  TINYINT(1) NOT NULL DEFAULT 1,
          trigger_type             ENUM('batch_enroll','check_completed','manual') NOT NULL DEFAULT 'batch_enroll',
          trigger_config           JSON NULL,
          source_batch_id          INT NULL,
          created_by               INT NULL,
          created_at               DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at               DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          legacy_campaign_id       INT NULL,
          UNIQUE KEY uq_automations_legacy (legacy_campaign_id),
          KEY idx_automations_status (status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    # automation_steps has an FK to automations, so it must be created after it.
    "automation_steps": """
        CREATE TABLE automation_steps (
          id                INT AUTO_INCREMENT PRIMARY KEY,
          automation_id     INT NOT NULL,
          node_key          VARCHAR(40) NOT NULL,
          node_type         ENUM('trigger','delay','send_email','if_else',
                                 'add_to_list','remove_from_list','unsubscribe') NOT NULL,
          config            JSON NULL,
          next_node_key     VARCHAR(40) NULL,
          next_node_key_alt VARCHAR(40) NULL,
          position_x        INT NOT NULL DEFAULT 0,
          position_y        INT NOT NULL DEFAULT 0,
          created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          legacy_step_id    INT NULL,
          UNIQUE KEY uq_steps_node (automation_id, node_key),
          KEY idx_steps_automation (automation_id),
          KEY idx_steps_legacy (legacy_step_id),
          CONSTRAINT fk_steps_automation FOREIGN KEY (automation_id) REFERENCES automations(id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    # automation_runs has an FK to automations too.
    "automation_runs": """
        CREATE TABLE automation_runs (
          id                    INT AUTO_INCREMENT PRIMARY KEY,
          automation_id         INT NOT NULL,
          lead_id               INT NOT NULL,
          current_node_key      VARCHAR(40) NULL,
          next_due_at           DATETIME NULL,
          status                ENUM('active','paused','completed','unsubscribed',
                                     'suppressed','blocked','cancelled') NOT NULL DEFAULT 'active',
          entered_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          last_sent_at          DATETIME NULL,
          last_email_id         INT NULL,
          legacy_enrollment_id  INT NULL,
          UNIQUE KEY uq_runs_lead_automation (lead_id, automation_id),
          UNIQUE KEY uq_runs_legacy (legacy_enrollment_id),
          KEY idx_runs_due (status, next_due_at),
          CONSTRAINT fk_runs_automation FOREIGN KEY (automation_id) REFERENCES automations(id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "automation_run_events": """
        CREATE TABLE automation_run_events (
          id               INT AUTO_INCREMENT PRIMARY KEY,
          run_key          INT NOT NULL,
          node_key         VARCHAR(40) NOT NULL,
          node_type        VARCHAR(20) NOT NULL,
          occurred_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          branch_taken     VARCHAR(40) NULL,
          engagement_state VARCHAR(30) NULL,
          filter_version   VARCHAR(30) NULL,
          KEY idx_runevents_run (run_key)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
}

# Creation order matters: automation_steps/automation_runs FK-reference automations.
TABLE_ORDER = ["emails", "automations", "automation_steps", "automation_runs", "automation_run_events"]


def main():
    conn = admin_db()
    try:
        with conn.cursor() as cur:
            for name in TABLE_ORDER:
                if table_exists(cur, name):
                    print(f"table {name} already exists -- skipping")
                else:
                    print(f"Creating table {name}")
                    if not DRY_RUN:
                        cur.execute(TABLES[name])

            if column_exists(cur, "drip_campaigns", "migrated_automation_id"):
                print("drip_campaigns.migrated_automation_id already exists -- skipping")
            else:
                print("Adding drip_campaigns.migrated_automation_id (ownership handshake)")
                if not DRY_RUN:
                    cur.execute(
                        "ALTER TABLE drip_campaigns ADD COLUMN migrated_automation_id INT NULL AFTER active"
                    )

            for col, ddl in (
                ("run_key", "ALTER TABLE drip_send_log ADD COLUMN run_key INT NULL AFTER enrollment_id"),
                ("email_id", "ALTER TABLE drip_send_log ADD COLUMN email_id INT NULL AFTER step_id"),
                ("node_key", "ALTER TABLE drip_send_log ADD COLUMN node_key VARCHAR(40) NULL AFTER email_id"),
            ):
                if column_exists(cur, "drip_send_log", col):
                    print(f"drip_send_log.{col} already exists -- skipping")
                else:
                    print(f"Adding drip_send_log.{col}")
                    if not DRY_RUN:
                        cur.execute(ddl)

            cur.execute("SHOW INDEX FROM drip_send_log WHERE Key_name='idx_sendlog_run'")
            if cur.fetchone():
                print("drip_send_log.idx_sendlog_run already exists -- skipping")
            else:
                print("Adding drip_send_log.idx_sendlog_run")
                if not DRY_RUN:
                    cur.execute("ALTER TABLE drip_send_log ADD KEY idx_sendlog_run (run_key)")

        if DRY_RUN:
            print("--dry-run: rolling back")
            conn.rollback()
        else:
            conn.commit()
            print("Committed.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
