# Phase 0 — Baseline worksheet

**Status: BLANK. This is a template, not a completed phase.**

Phase 0 is two days of organisational work that cannot be done in a repository:
selecting a use case, naming a team, and writing down a number. The code in this
repo implements Phase 1. This worksheet exists so Phase 0 has somewhere to land,
and so the exit criteria are checkable rather than remembered.

*Exit criteria, from Section 5: a named use case, a named team, a number on paper.*

---

## 1. Use case

### The selection criterion is not negotiable

> **Work that Copilot structurally cannot do**, because the data or system cannot
> leave our boundary — not work Copilot does slightly worse.

This is the highest-likelihood risk in the register ("Perceived as duplicating
Copilot", High/High). A use case that fails this test loses the argument in the
first review meeting regardless of how well the pilot performs.

**Test each candidate against all four. A candidate needs at least one YES.**

| # | Question | Candidate answer |
|---|---|---|
| 1 | Does the workload touch regulated data that cannot leave our boundary? | |
| 2 | Does the work span systems with no GitHub surface (internal services, data platform, ticketing, lineage)? | |
| 3 | Do we require tool-call-level evidence in our own schema? | |
| 4 | Do we need policy enforced at the tool boundary rather than a vendor allowlist? | |

**Selected use case:** _______________________

**Which criterion it satisfies, and why:** _______________________

**Why Copilot cannot do this** (one paragraph, written so a sceptic can check it):

_______________________

---

## 2. Team

Section 5 requires one team, 5–8 engineers, **who volunteered**. Conscripted
pilots produce conscripted numbers.

| Field | Value |
|---|---|
| Team name | |
| Engineering manager | |
| Named engineers (5–8) | |
| Volunteered? (Y/N) | |
| Start date | |

---

## 3. The number

Capture the current metric **in writing, in units leadership already reports**.
Inventing a new unit for the pilot means the result cannot be compared to
anything, which is the same as not having a result.

| Field | Value |
|---|---|
| Metric name | |
| Unit (as leadership already reports it) | |
| Current value | |
| Measurement method | |
| Measurement window | |
| Sample size | |
| Captured by / date | |
| Where the raw baseline data lives | |

**Known weaknesses in this baseline** (state them now; a baseline whose flaws are
discovered at week six is a baseline that gets argued with instead of acted on):

_______________________

---

## 4. Kill gate — pre-agreed

From Section 5, watch item W7, and the risk register.

> **If the metric has not moved by week 6, stop and say so publicly.**
> A visible, honest kill buys more credibility than a rescue.

| Field | Value |
|---|---|
| Gate date (week 6) | |
| "Moved" means (specific threshold, agreed in advance) | |
| Who declares the outcome | |
| Where the outcome is published | |

Agreeing the threshold **before** the pilot runs is the whole point. A threshold
set at week six is a negotiation, not a gate.

---

## 5. Blocking dependency — assumption A4

Assumption A4 (an AI-specific regulatory obligation applying to part of the
estate) **must be verified with Legal and Compliance before it appears in any
leadership material**. The plan is explicit: do not carry a regulatory claim
into a leadership deck on the strength of secondary reporting. The risk register
rates overstating it Medium/High.

| Field | Value |
|---|---|
| Legal contact | |
| Date raised | |
| Verdict | |
| Date confirmed | |

Until this row is filled in, the pilot stands on its own merits — which the plan
says it does anyway. Nothing in Phase 1 depends on the answer.

---

## 6. Open questions carried from Section 10

These need owners before Phase 3, not before Phase 1.

| # | Question | Owner | Status |
|---|---|---|---|
| 1 | Which use case satisfies the Phase 0 criterion? | | Answered by section 1 above |
| 2 | Does GitHub Enterprise Server change the Copilot side of the split? | | |
| 3 | Who owns MCP allowlist policy today — and if nobody, do we claim it? | | |
| 4 | Vertex model choice and residency posture for the pilot | | |
| 5 | Evidence retention period and access model (Legal and Records input) | | |
