---
name: opc-sales-pipeline
description: Prepare bounded sales pipeline follow-up and quote-review drafts for the OPC workspace after P1 record version semantics are approved.
---

# OPC Sales Pipeline

Use this Skill only after `opc-ceo-office` has produced an approved Workspace state and
P1 domain record version semantics have been accepted for the relevant records.

## Authority

This Skill has no write authority over P0 imported `pipeline` or `receivable` records.
Those records remain Google-Sheets-owned until an explicit ownership migration is
approved and tested. Do not mutate canonical Workspace records from this Skill.

## Allowed P1 Work

1. Read only bounded Top or explicitly provided sales records.
2. Draft follow-up options, quote-review questions, and next-action recommendations.
3. Treat `lead` as a revisioned working record only when the Workspace grants
   `opc_sales_pipeline` ownership.
4. Treat `quote` as immutable evidence: changes require a successor revision with
   `stable_id`, incremented `revision`, and `supersedes`.

## Boundaries

- Do not send outbound messages.
- Do not commit price, scope, delivery dates, discounts, refunds, or payment terms.
- Do not infer hidden pipeline data from aggregate counts.
- Do not bypass `opc-ceo-office` import, review, seal, or approval flows.
