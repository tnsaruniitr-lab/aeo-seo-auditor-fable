# Supabase Queries — Persistence + Sieve Brain

All queries run against project `aldraxqsqeywluohskhs` using `mcp__supabase__execute_sql`.

---

## Audit Persistence

### Insert New Audit

```sql
INSERT INTO website_audits (
  url, domain, page_type, company_name, company_description, industry,
  competitors, target_queries, overall_score, overall_grade, section_scores,
  http_headers, meta_tags, schema_data, heading_structure,
  total_checks, passed, failed, warnings, na_checks,
  competitor_data, executive_diagnosis, top_fixes, audit_duration_seconds
) VALUES (
  $url, $domain, $page_type, $company_name, $company_description, $industry,
  $competitors::jsonb, $target_queries::jsonb, $overall_score, $overall_grade, $section_scores::jsonb,
  $http_headers::jsonb, $meta_tags::jsonb, $schema_data::jsonb, $heading_structure::jsonb,
  $total_checks, $passed, $failed, $warnings, $na_checks,
  $competitor_data::jsonb, $executive_diagnosis, $top_fixes::jsonb, $audit_duration_seconds
) RETURNING id;
```

### Insert Findings (batch)

```sql
INSERT INTO website_audit_findings (
  audit_id, check_id, category, subcategory, status, severity,
  title, description, fix_description, fix_before, fix_after,
  fix_effort, fix_impact, competitor_benchmark
) VALUES
  ($audit_id, $check_id, $category, $subcategory, $status, $severity,
   $title, $description, $fix_description, $fix_before, $fix_after,
   $fix_effort, $fix_impact, $competitor_benchmark),
  -- repeat for each finding
;
```

### Query Previous Audits for Same Domain

```sql
SELECT id, url, overall_score, overall_grade, section_scores, 
  total_checks, passed, failed, audited_at
FROM website_audits 
WHERE domain = $domain 
ORDER BY audited_at DESC 
LIMIT 5;
```

### Query Findings for an Audit

```sql
SELECT check_id, category, subcategory, status, severity, title, 
  description, fix_description, fix_effort, fix_impact
FROM website_audit_findings 
WHERE audit_id = $audit_id 
ORDER BY 
  CASE severity WHEN 'critical' THEN 1 WHEN 'high' THEN 2 WHEN 'medium' THEN 3 WHEN 'low' THEN 4 WHEN 'info' THEN 5 END,
  category, check_id;
```

---

## Sieve Brain Queries

These query the Sieve brain tables for evidence-backed audit intelligence.
**Only use these if the brain tables have data** — check first with a count query.

### Check Brain Availability

```sql
SELECT 
  (SELECT count(*) FROM rules WHERE domain_tag IN ('seo','aeo','geo','entity')) as rule_count,
  (SELECT count(*) FROM anti_patterns WHERE domain_tag IN ('seo','aeo','entity')) as anti_pattern_count,
  (SELECT count(*) FROM principles WHERE domain_tag IN ('seo','aeo','geo','entity')) as principle_count,
  (SELECT count(*) FROM playbooks WHERE domain_tag IN ('seo','aeo','geo')) as playbook_count;
```

If counts are all 0, skip brain integration and note in report: "Brain intelligence not available — using built-in rubrics only."

### Load High-Confidence Rules

```sql
SELECT id, name, if_condition, then_logic, domain_tag, confidence_score
FROM rules 
WHERE domain_tag IN ('seo', 'aeo', 'geo', 'entity')
  AND confidence_score >= 0.85
  AND status = 'active'
ORDER BY confidence_score DESC 
LIMIT 50;
```

Use these to validate findings: if a check failure matches a brain rule's `if_condition`, cite the rule in the finding description for evidence backing.

### Load Anti-Patterns

```sql
SELECT id, title, description, domain_tag, risk_level, detection_pattern
FROM anti_patterns 
WHERE domain_tag IN ('seo', 'aeo', 'entity')
  AND risk_level IN ('critical', 'high', 'medium')
ORDER BY 
  CASE risk_level WHEN 'critical' THEN 1 WHEN 'high' THEN 2 WHEN 'medium' THEN 3 END
LIMIT 30;
```

Match detected issues against anti-patterns. If a match is found, escalate severity and use the anti-pattern's description as additional context in the finding.

### Load Relevant Principles

```sql
SELECT id, title, statement, domain_tag, confidence_score
FROM principles 
WHERE domain_tag IN ('seo', 'aeo', 'geo', 'entity')
  AND confidence_score >= 0.90
  AND status = 'active'
ORDER BY confidence_score DESC 
LIMIT 20;
```

Use principles to provide "Why This Matters" context for top findings.

### Load Remediation Playbooks

```sql
SELECT id, title, summary, domain_tag, confidence_score
FROM playbooks 
WHERE domain_tag IN ('seo', 'aeo', 'geo')
  AND confidence_score >= 0.85
ORDER BY confidence_score DESC 
LIMIT 10;
```

Reference relevant playbooks in the fix recommendations section. Format as: "For detailed remediation steps, see Sieve Playbook: '[name]'"

### Search Brain by Keyword

Use this when a specific finding needs brain context:

```sql
SELECT 'rule' as type, id, name as title, if_condition || ' → ' || then_logic as detail, confidence_score
FROM rules 
WHERE (name ILIKE '%$keyword%' OR if_condition ILIKE '%$keyword%')
  AND domain_tag IN ('seo', 'aeo', 'geo', 'entity')
  AND confidence_score >= 0.80
UNION ALL
SELECT 'anti_pattern' as type, id, title, description as detail, 0 as confidence_score
FROM anti_patterns 
WHERE (title ILIKE '%$keyword%' OR description ILIKE '%$keyword%')
  AND domain_tag IN ('seo', 'aeo', 'entity')
ORDER BY confidence_score DESC
LIMIT 10;
```

---

## Layered Brain Queries (New Architecture — Source-Aware)

### Layer 2: Direct ID Lookup WITH Sources (Curated Mappings)

Use brain-mappings.md to get specific entry IDs per check. Fetch by ID WITH source documents:

```sql
-- Fetch rules by ID WITH source citation data
-- source_refs_json contains document IDs as JSON array e.g. [270]
-- source_org contains the organization name e.g. "Perplexity"
SELECT r.id, r.name, r.if_condition, r.then_logic, 
  r.confidence_score::text, r.domain_tag::text,
  r.source_org,
  d.title as source_title, d.source_url
FROM rules r
LEFT JOIN documents d ON d.id = (
  SELECT (jsonb_array_elements_text(r.source_refs_json::jsonb))::int LIMIT 1
)
WHERE r.id IN ($id1, $id2, $id3);
```

**If the jsonb join fails** (source_refs_json format varies), use simpler fallback:
```sql
SELECT id, name, confidence_score::text, source_org
FROM rules WHERE id IN ($id1, $id2, $id3);
```

```sql
-- Fetch anti-patterns by ID WITH source citation data
SELECT ap.id, ap.title, ap.description, ap.risk_level::text, ap.domain_tag::text,
  ap.source_org,
  d.title as source_title, d.source_url
FROM anti_patterns ap
LEFT JOIN documents d ON d.id = (
  SELECT (jsonb_array_elements_text(ap.source_refs_json::jsonb))::int LIMIT 1
)
WHERE ap.id IN ($id1, $id2, $id3);
```

This is the PRIMARY brain query method. Direct lookup with source chain, no keyword search.

### Layer 3: Supplementary Scan WITH Sources (Discovery)

After all 101 checks, find brain entries NOT already mapped to any check:

```sql
-- Find anti-patterns relevant to this page but not mapped to any check
-- Include source data for citation
SELECT ap.id, ap.title, ap.description, ap.risk_level::text, ap.domain_tag::text,
  ap.source_org, d.title as source_title, d.source_url
FROM anti_patterns ap
LEFT JOIN documents d ON d.id = (
  SELECT (jsonb_array_elements_text(ap.source_refs_json::jsonb))::int LIMIT 1
)
WHERE ap.domain_tag IN ('seo', 'aeo', 'entity')
  AND ap.id NOT IN ($all_mapped_ap_ids)
  AND (ap.title ILIKE '%$keyword%' OR ap.description ILIKE '%$keyword%')
  AND risk_level IN ('high', 'medium')
LIMIT 10;
```

### Supplementary Findings Persistence

```sql
-- Insert supplementary finding
INSERT INTO website_audit_supplementary (
  audit_id, sieve_entry_type, sieve_entry_id, sieve_entry_title,
  sieve_entry_description, sieve_confidence, sieve_risk_level, relevance_note
) VALUES (
  $audit_id, $type, $entry_id, $title, $description, $confidence, $risk_level, $note
);
```

---

## Review Cycle Queries (Slow Loop)

Run these periodically (after every 20 audits or when Sieve ingests new documents)
to identify candidates for static ruleset updates.

### 1. Most Common Supplementary Findings

```sql
-- Which supplementary patterns keep appearing across audits?
-- Candidates for promotion to new static checks
SELECT sieve_entry_type, sieve_entry_id, sieve_entry_title, 
  sieve_risk_level, count(*) as occurrences,
  array_agg(DISTINCT audit_id) as audit_ids
FROM website_audit_supplementary
WHERE promoted_to_check_id IS NULL
GROUP BY sieve_entry_type, sieve_entry_id, sieve_entry_title, sieve_risk_level
HAVING count(*) >= 3
ORDER BY occurrences DESC;
```

If any appear 5+ times → strong candidate for new check.

### 2. New High-Confidence Sieve Rules Not Mapped

```sql
-- Rules created since last review that aren't in brain-mappings.md
-- $mapped_rule_ids = all rule IDs currently in brain-mappings.md
SELECT id, name, if_condition, then_logic, confidence_score::text, domain_tag::text, created_at
FROM rules 
WHERE domain_tag IN ('seo', 'aeo', 'geo', 'entity')
  AND confidence_score >= 0.90
  AND id NOT IN ($mapped_rule_ids)
  AND created_at > $last_review_date
ORDER BY confidence_score DESC
LIMIT 20;
```

Each is a candidate for: (a) mapping to existing check, or (b) new check creation.

### 3. Confidence Changes on Mapped Rules

```sql
-- Check if any mapped rules had confidence changes
-- $mapped_rule_ids = all rule IDs in brain-mappings.md
SELECT id, name, confidence_score::text, updated_at
FROM rules 
WHERE id IN ($mapped_rule_ids)
  AND updated_at > $last_review_date
ORDER BY updated_at DESC;
```

If confidence dropped significantly (e.g., 0.99 → 0.70), consider removing the mapping.

### 4. New Anti-Patterns Not Mapped

```sql
SELECT id, title, description, risk_level::text, domain_tag::text, created_at
FROM anti_patterns 
WHERE domain_tag IN ('seo', 'aeo', 'entity')
  AND risk_level IN ('high', 'medium')
  AND id NOT IN ($mapped_ap_ids)
  AND created_at > $last_review_date
ORDER BY risk_level, created_at DESC
LIMIT 20;
```

### 5. Audit Trend Summary

```sql
-- Overall audit health across recent audits
SELECT 
  count(*) as total_audits,
  avg(overall_score) as avg_score,
  avg(passed::float / NULLIF(total_checks, 0) * 100) as avg_pass_rate,
  count(CASE WHEN overall_grade = 'F' THEN 1 END) as failing_audits,
  max(audited_at) as latest_audit
FROM website_audits
WHERE audited_at > now() - interval '30 days';
```

---

## Usage Notes

1. **Layer 2 (direct ID lookup) is PRIMARY** — always use curated mappings from brain-mappings.md
2. **Layer 3 (supplementary scan) runs AFTER all 97 checks** — discovery phase, not check phase
3. **Keyword search is FALLBACK ONLY** — use for checks with no curated mapping yet
4. **Brain queries are optional** — the audit runs without them using static-rules.md alone
5. **Cap citations at 3 per finding** — more is noise, not signal
6. **Track supplementary findings** — they feed the review cycle for static rule evolution
7. **Never block on brain queries** — if Supabase is slow/down, skip enrichment entirely
