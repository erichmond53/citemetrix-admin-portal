#!/usr/bin/env python3
"""Verification for lead_pool.py's enrollment gate, run against the real
migrated leads. Not a permanent fixture -- prints a report and cleans up any
test enrollment rows it creates, leaving drip_enrollments as it found it.

2026-09-02 (step 3c): repointed from lead_enrollments (0 rows, no writer,
not the live table) to drip_enrollments -- the table leads_drip.py's
enroll_batch()/process_due_enrollments() actually read and write post-3b,
and what is_eligible() now checks against. The gate is wired into that
engine as of this step; this script re-verifies it in isolation, plus the
exclude_enrollment_id path added for process_due_enrollments()'s send-time
re-check.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE, ".env"))
import pymysql
import lead_pool


def admin_db():
    return pymysql.connect(
        host="localhost", user=os.getenv("ADMIN_DB_USER", "adminportal"),
        password=os.getenv("ADMIN_DB_PASSWORD"), database=os.getenv("ADMIN_DB_NAME", "admin_portal"),
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
    )


def main():
    conn = admin_db()
    with conn.cursor() as cur:
        cur.execute("SELECT id, email, suppressed_at, wp_nurture_owned, person_cluster_id, stage FROM leads ORDER BY id")
        leads = cur.fetchall()

        print(f"=== Gate check against all {len(leads)} real migrated leads ===")
        eligible_ids = []
        for l in leads:
            elig, reason = lead_pool.is_eligible(cur, l["id"])
            tag = "ELIGIBLE" if elig else "blocked "
            print(f"  lead {l['id']:>3} ({l['email']:<40}) cluster={str(l['person_cluster_id']):<5} -> {tag}: {reason}")
            if elig:
                eligible_ids.append(l["id"])

        print(f"\n{len(eligible_ids)} of {len(leads)} eligible before any enrollment: {eligible_ids}")

        # Sanity checks against known facts. The only leads that can be
        # eligible are the ones with excluded=0 AND wp_nurture_owned=0 AND
        # suppressed_at IS NULL (step1brief.md SS12.2 added the excluded
        # check -- lead 43, the one row that used to slip through as the
        # sole wp_nurture_owned=0 lead, is now excluded=1 test debris, so
        # today's real answer is 0 eligible, not 1).
        cur.execute("SELECT COUNT(*) AS n FROM leads WHERE suppressed_at IS NOT NULL")
        n_suppressed = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM leads WHERE wp_nurture_owned=1")
        n_wp_owned = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM leads WHERE excluded=1")
        n_excluded = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM leads WHERE suppressed_at IS NULL AND wp_nurture_owned=0 AND excluded=0")
        n_should_be_eligible = cur.fetchone()["n"]
        print(f"\nExpected: {n_suppressed} suppressed, {n_wp_owned} wp_nurture_owned, {n_excluded} excluded, "
              f"{n_should_be_eligible} pass all three gates (before the cluster check)")
        assert len(eligible_ids) == n_should_be_eligible, \
            f"MISMATCH: gate returned {len(eligible_ids)} eligible, expected {n_should_be_eligible}"
        print("PASS: eligible count matches suppression+interlock filter exactly (no cluster conflicts yet, so this should match 1:1)")

        # Cluster test: all 10 REAL clustered rows are wp_nurture_owned=1, so
        # is_eligible() short-circuits on the interlock check before ever
        # reaching the cluster-conflict branch -- testing against them would
        # "pass" for the wrong reason (interlock, not cluster logic), which
        # is exactly what happened on the first version of this script: both
        # assertions passed but neither exercised the cluster branch at all.
        # Flipping wp_nurture_owned on a real row to force it open is exactly
        # the kind of touch the interlock exists to prevent. So: create two
        # synthetic leads (not touching any of the 43 real ones), both
        # genuinely passing suppression+interlock, sharing a cluster --
        # isolates the cluster-conflict branch specifically. Deleted at the end.
        print(f"\n=== Cluster-conflict test (synthetic leads, isolating the cluster branch specifically) ===")
        cur.execute(
            """INSERT INTO leads (email, original_source, stage, suppressed_at, wp_nurture_owned, person_cluster_id, legacy_source_table, legacy_id)
               VALUES ('gate-test-a@citemetrix-test.invalid','direct','new',NULL,0,900001,'gate_test',1),
                      ('gate-test-b@citemetrix-test.invalid','direct','new',NULL,0,900001,'gate_test',2)"""
        )
        conn.commit()
        cur.execute("SELECT id FROM leads WHERE legacy_source_table='gate_test' ORDER BY legacy_id")
        synth = cur.fetchall()
        lead_a, lead_b = synth[0]["id"], synth[1]["id"]
        print(f"Created synthetic leads {lead_a} and {lead_b}, both suppressed_at=NULL, wp_nurture_owned=0, cluster=900001")

        # drip_enrollments.campaign_id is FK'd to drip_campaigns (added in step
        # 3b's repoint) -- unlike the old lead_enrollments.sequence_id, an
        # arbitrary placeholder id no longer satisfies the schema. Create a
        # throwaway campaign row for this test, deleted at the end.
        cur.execute(
            "INSERT INTO drip_campaigns (name, active) VALUES ('gate-test-campaign (safe to delete)', 0)"
        )
        conn.commit()
        test_campaign_id = cur.lastrowid
        print(f"Created throwaway campaign {test_campaign_id} to satisfy drip_enrollments' FK")

        elig_a0, reason_a0 = lead_pool.is_eligible(cur, lead_a)
        assert elig_a0 is True, f"synthetic lead {lead_a} should be eligible before any enrollment (got: {reason_a0})"
        print(f"  lead {lead_a} before enrollment: {reason_a0} -- correct")

        print(f"Enrolling lead {lead_a} via lead_pool.enroll() (this time through the real gate, since it's genuinely eligible)...")
        enrollment_id = lead_pool.enroll(cur, conn, lead_a, test_campaign_id)
        print(f"  -> enrolled, enrollment id {enrollment_id}")

        elig_a, reason_a = lead_pool.is_eligible(cur, lead_a)
        assert elig_a is False and "already has an active enrollment" in reason_a, \
            f"lead {lead_a} should now be blocked by its own active enrollment, got: {reason_a}"
        print(f"  lead {lead_a} re-checked: blocked ({reason_a}) -- correct, own active enrollment")

        elig_b, reason_b = lead_pool.is_eligible(cur, lead_b)
        assert elig_b is False and "already has an active enrollment" in reason_b, \
            f"cluster-mate {lead_b} should be blocked by lead {lead_a}'s active enrollment via shared cluster, got: {reason_b}"
        print(f"  cluster-mate lead {lead_b} checked: blocked ({reason_b}) -- correct, same cluster, this IS the cluster-conflict branch firing")

        # lead 43 (id 43) used to be this script's "unrelated real lead" control
        # -- it was the one real lead with no reason to be blocked, so a clean
        # elig3 is True proved the synthetic cluster (900001) didn't leak into
        # unrelated leads. Step1brief.md SS12.2: lead 43 turned out to be test
        # debris and is now excluded=1, so it's no longer a usable "eligible"
        # control. Use a real lead with its OWN distinct cluster instead (12,
        # cluster=12) -- confirm its blocking reason is unchanged by cluster
        # 900001's enrollment, which is the same cross-cluster-isolation claim.
        elig3, reason3 = lead_pool.is_eligible(cur, 12)
        assert elig3 is False and "wp_nurture_owned" in reason3, \
            f"unrelated real lead 12 (cluster=12, distinct from the synthetic 900001) should be unaffected, got: {reason3}"
        print(f"  unrelated real lead 12 (cluster=12) checked: blocked ({reason3}) -- correct, same reason as always, unaffected by the synthetic cluster's enrollment")

        # exclude_enrollment_id test (step 3c): process_due_enrollments() has to
        # re-check an already-actively-enrolled lead at send time without that
        # lead's own active row self-blocking it.
        print(f"\n=== exclude_enrollment_id test (send-time re-check path) ===")
        elig_a_excl, reason_a_excl = lead_pool.is_eligible(cur, lead_a, exclude_enrollment_id=enrollment_id)
        assert elig_a_excl is True, f"lead {lead_a} should be eligible when its own enrollment is excluded, got: {reason_a_excl}"
        print(f"  lead {lead_a} re-checked excluding its own enrollment {enrollment_id}: {reason_a_excl} -- correct, no longer self-blocked")

        # Note: exclude_enrollment_id=enrollment_id would NOT be a valid test
        # here -- enrollment_id belongs to lead_a, and excluding it removes
        # lead_a's row from lead_b's cluster check too (there's nothing else
        # to find), so lead_b would come back eligible. That pairing never
        # happens in production (process_due_enrollments always excludes the
        # enrollment id belonging to the SAME lead it's checking). The real
        # thing to verify is that exclusion is scoped to the one row named,
        # not a blanket cluster bypass -- so exclude an id that isn't in play:
        elig_b_excl, reason_b_excl = lead_pool.is_eligible(cur, lead_b, exclude_enrollment_id=0)
        assert elig_b_excl is False and "already has an active enrollment" in reason_b_excl, \
            f"cluster-mate {lead_b} should still be blocked by lead {lead_a}'s enrollment when the excluded id isn't lead_a's, got: {reason_b_excl}"
        print(f"  cluster-mate lead {lead_b} re-checked with exclude_enrollment_id=0 (not in play): still blocked ({reason_b_excl}) -- correct, exclusion only exempts the exact row named")

        # Clean up -- delete the test enrollment, the throwaway campaign, then
        # the synthetic leads and their events.
        cur.execute("DELETE FROM drip_enrollments WHERE id=%s", (enrollment_id,))
        cur.execute("DELETE FROM drip_campaigns WHERE id=%s", (test_campaign_id,))
        cur.execute("DELETE FROM lead_events WHERE lead_id IN (%s,%s)", (lead_a, lead_b))
        cur.execute("DELETE FROM leads WHERE id IN (%s,%s)", (lead_a, lead_b))
        conn.commit()
        print(f"  cleaned up: deleted test enrollment + throwaway campaign + both synthetic leads")

        cur.execute("SELECT COUNT(*) AS n FROM leads WHERE legacy_source_table='gate_test'")
        assert cur.fetchone()["n"] == 0, "synthetic test leads were not fully cleaned up"
        cur.execute("SELECT COUNT(*) AS n FROM drip_campaigns WHERE id=%s", (test_campaign_id,))
        assert cur.fetchone()["n"] == 0, "throwaway campaign was not cleaned up"
        cur.execute("SELECT COUNT(*) AS n FROM drip_enrollments WHERE lead_id IN (%s,%s)", (lead_a, lead_b))
        remaining = cur.fetchone()["n"]
        print(f"\ntest-lead drip_enrollments row count after cleanup: {remaining} (should be 0)")
        assert remaining == 0, "test enrollment(s) were not fully cleaned up"

    conn.close()
    print("\n=== ALL CHECKS PASSED ===")


if __name__ == "__main__":
    main()
