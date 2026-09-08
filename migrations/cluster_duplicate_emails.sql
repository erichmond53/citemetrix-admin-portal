-- One-off: cluster the 5 known-duplicate emails from the step 1b migration.
-- The audit (Investigation C) already confirmed these 5 emails exist twice in
-- wp_citemetrix_score_leads; deferring the general dedupe DETECTOR (decisions-v2
-- SS4.2) is correct, but leaving dup_status='unique' on rows already known to be
-- duplicates asserts something known false, and leaves person_cluster_id NULL
-- for the exact rows step 2's "one active enrollment per person_cluster_id"
-- gate needs to be tested against. Cluster id = MIN(id) of each pair.

UPDATE leads a JOIN leads b
  ON a.email = b.email AND a.id < b.id
SET a.dup_status = 'possible_dup', a.person_cluster_id = a.id,
    b.dup_status = 'possible_dup', b.person_cluster_id = a.id
WHERE a.email IN (
  SELECT email FROM (
    SELECT email FROM leads GROUP BY email HAVING COUNT(*) > 1
  ) x
);
