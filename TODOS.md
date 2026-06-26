# TODOS

## P1 Start Call

P1 may start now, but only in this order:

1. Define domain record version semantics against representative real P0 records.
2. Implement one domain Skill at a time.
3. Start domain implementation with `opc-sales-pipeline`.
4. Keep `opc-legal-risk` and `opc-finance-tax` behind stricter evidence and professional-boundary review.
5. Do not grant any domain Skill write authority until ownership migration, approval gates, and cross-Skill diagnostics are explicit and tested.

This is a go for P1 entry, not a go for broad autonomous mutation workflows.

Implemented P1 entry slice:

- Added machine-readable domain record version policy.
- Added `opc-sales-pipeline` as the first P1 Skill surface.
- Added `opc-workspace install --include-p1` to install the P1 Skill alongside `opc-ceo-office`.
- Preserved the no-write-authority boundary for P0 imported records.

## Skills

### Define domain record version semantics

**What:** Define immutable-version versus in-place-update rules for domain entities, including `revision`, `supersedes`, stable references, history retention, and archive behavior.

**Why:** Prevent new quotes, contracts, invoices, or delivery records from overwriting business evidence or invalidating cross-Skill references.

**Context:** This is the P1 entry task. P0 treats Google Sheets as the authoritative writer for six imported types and Workspace as authoritative for Briefs/decisions. Complete this design against real P0 records before any P1 domain Skill receives write authority; define explicit ownership migration, prohibit dual writers, and do not introduce full event sourcing unless evidence later justifies it.

**Effort:** M
**Priority:** P1
**Depends on:** P0 Schema v1 and representative real records

### Implement the four domain Skills

**What:** Implement `opc-sales-pipeline`, `opc-legal-risk`, `opc-project-ops`, and `opc-finance-tax` as real domain workflows after the P0 Daily Briefing slice is proven.

**Why:** Complete the five-Skill operating model so each domain can maintain its own structured records and handoffs instead of remaining a read-only input to CEO Office.

**Context:** P0 deliberately excludes empty domain Skill stubs. `opc-ceo-office` now has passing local verification and checked-in connector/live/eval/benchmark/canary evidence, so domain work may begin after domain record version semantics are approved. Implement and forward-test one domain at a time, preserving the Workspace contract, ownership migration, approval gates, and cross-Skill diagnostics. Start with `opc-sales-pipeline`, because it reuses the existing pipeline/receivable operating loop and carries lower regulated-advice risk than legal or tax workflows. Domain-specific execution drafts such as follow-up or reminder drafts belong here, never in P0 CEO Office.

**Effort:** L
**Priority:** P1
**Depends on:** P0 test/eval completion, real-use acceptance, and domain record version semantics

### Package mature OPC Skills as a Codex Plugin

**What:** Package the stable OPC Skills, Workspace initialization assets, and version metadata as an installable Codex Plugin.

**Why:** Give external users one versioned installation and upgrade path instead of requiring manual directory copies.

**Context:** P0 remains a personal user-level Skill installed by a deterministic local script. Begin Plugin work only after the Workspace contract is stable in real use and at least one P1 domain Skill has validated cross-Skill handoffs; include compatibility policy, release automation, installation verification, and security review.

**Effort:** M
**Priority:** P2
**Depends on:** Stable P0 and at least one production-proven P1 domain Skill

## Integrations

### Support declarative mapping for existing Google Sheets

**What:** Add a declarative mapping layer from existing tab/column layouts into Workbook Contract fields without guessing mappings automatically.

**Why:** Let an OPC reuse existing operating spreadsheets instead of migrating all data into the fixed six-tab template.

**Context:** P0 intentionally supports only exact named ranges in the fixed template. Begin mapping only after five canary runs and a stable v1 contract; mappings must be explicit, versioned, previewable, testable, and rejected on ambiguity.

**Effort:** M
**Priority:** P2
**Depends on:** Five P0 canary runs and stable Workbook Contract v1

### Add approved Google Sheets write-back

**What:** Write approved dispositions and review dates to dedicated Sheet columns without granting broad record mutation.

**Why:** Reduce dual maintenance after read-only synchronization and decision semantics are proven.

**Context:** P0 is strictly read-only toward Sheets. Add write-back only with exact range grounding, before-write rereads, validation-aware values, idempotency, conflict detection, explicit per-batch approval, and rollback/audit behavior; never create general two-way sync implicitly.

**Effort:** M
**Priority:** P2
**Depends on:** Stable read-only import, per-record projections, and five production-sheet runs

## Automation

### Schedule draft-only Daily Briefing generation

**What:** Run Daily Briefing generation on a daily schedule and notify the CEO when a draft or actionable failure is ready, without unattended Apply.

**Why:** Make the briefing a reliable operating cadence instead of depending on the CEO remembering to invoke it manually.

**Context:** Add scheduling only after five successful manual canary runs prove the Briefing useful and the CLI reentrant. Handle machine sleep, overlapping runs, Workspace locks, missing Connector auth, notification failure, and catch-up behavior; canonical Apply and external actions must remain explicitly approved.

**Effort:** M
**Priority:** P2
**Depends on:** Stable P0 CLI and a selected notification channel

### Implement Weekly CEO Review

**What:** Generate a weekly review from approved Daily Briefs, source deltas, dispositions, repeated deferrals, and unresolved delegated work.

**Why:** Reveal operating patterns that a daily urgency view cannot show.

**Context:** Do not invent weekly KPIs before real history exists. Start after at least ten approved Briefs; define trend baselines, repeated-deferral rules, decision-quality review, and evidence links from accumulated P0 artifacts.

**Effort:** M
**Priority:** P1
**Depends on:** Ten approved Daily Briefs and stable disposition semantics

## Platform

### Implement the first concrete Schema migration

**What:** Implement and test an explicit v1-to-v2 Workspace and Workbook Contract migration when v2 requirements exist.

**Why:** Preserve real user data across an incompatible contract change without building a speculative generic migration framework.

**Context:** P0 detects incompatible versions, blocks writes, and creates validated backups. When v2 is designed, add a sealed dry-run, per-record transformation, validation, approval, journaled Apply, rollback, and fixture coverage for that exact migration only.

**Effort:** M
**Priority:** P1
**Depends on:** Approved v2 contract and real v1 canary data

## Completed
