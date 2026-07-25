# Internal Agent Platform — Tactical & Strategic Plan

**A reference architecture, written as a worked example.**
**First iteration harness:** Deep Agents (LangChain) — deliberately replaceable

> **This document describes a hypothetical organisation.** It is published as a
> reference architecture: a plan of the shape an engineering leader might take
> to a leadership team — complete enough to be argued with, and to be
> implemented against. The assumptions in Section 0, the regulatory
> considerations, the vendor decisions and the phase gates are all
> *illustrative*. They are not the position, posture, or roadmap of any real
> company, and nothing here is a statement of fact about any organisation's
> compliance obligations, vendor relationships, or internal systems.
>
> If you adapt this, replace Section 0 with your own assumptions and verify
> anything regulatory with your own Legal and Compliance functions. The
> architecture is the reusable part. The assumptions are not.

---

## 0. Stated assumptions

These are assumptions, not findings — and here they are the assumptions of a
worked example. Anyone reusing this plan should replace the table wholesale
before it informs a real decision.

| # | Assumption | If wrong |
|---|---|---|
| A1 | First use case is an internal engineering / operations agent, not customer-facing | Re-scope Phase 1; identity and consent requirements increase materially |
| A2 | Target cloud is GCP (Vertex, Cloud Run, Cloud Trace, IAM) | Architecture holds; Section 5 deployment detail changes |
| A3 | A commercial IDE coding assistant is already licensed and deployed to engineering | The "buy the IDE, build the platform" split still holds but needs its own business case |
| A4 | Some AI-specific regulatory obligation may apply to part of the estate | Removes the strongest timing argument; the plan still stands on its own merits |

**An assumption like A4 must be verified with Legal and Compliance before it appears in any leadership material.** Do not carry a regulatory claim into a leadership deck on the strength of secondary reporting. It is called out here precisely because getting it wrong is a common and expensive failure.

---

## 1. Executive summary

We are not building a coding agent. Commercial tools already do that well, we already pay for one, and competing with them is unwinnable.

We are building the **layer beneath any agent**: a governed context and evidence gateway that lets agents operate on systems and data that cannot leave our cloud boundary, and produces an auditable record of everything they do.

The agent runtime — the "harness" — is treated as a commodity with an expected useful life of 12–24 months. Deep Agents is the first iteration because our teams already run LangGraph and can maintain it on day one. The architecture assumes we will replace it, and is designed so that replacing it costs a sprint rather than a program.

**Ask:** one team, six weeks, to a measured number against an existing baseline. Hard kill gate at week six.

---

## 2. Strategy

### 2.1 What we build and what we buy

| Layer | Decision | Rationale |
|---|---|---|
| IDE / repo coding assistance | **Buy** — Copilot | Already licensed, already security-reviewed, better than anything we would build |
| Agent runtime / harness | **Rent** — Deep Agents now | No differentiation; commoditizing fast; expect to replace |
| **Context gateway (MCP)** | **Build** | Our internal knowledge is the differentiator. No vendor has it |
| **Evidence log** | **Build** | Nobody else can produce records in our schema, in our store |
| **Policy & agent identity** | **Build** | Enforcement must sit inside our boundary |

### 2.2 Why the platform cannot be Copilot

Copilot's cloud agent executes on Microsoft infrastructure. That is fine for repo-shaped work on code we are comfortable exposing. It is not a substitute for a platform when:

- the workload touches regulated data that cannot leave our boundary
- the work spans systems with no GitHub surface (internal services, data platform, ticketing, lineage)
- we require tool-call-level evidence in our own schema
- we need to enforce policy at the tool boundary rather than accept a vendor's allowlist

The two are complementary, not competing. The failure mode to avoid is pitching this as a Copilot replacement — that is a comparison we lose and do not need to win.

### 2.3 The one-sentence position

> The agent runtime is replaceable. The context and the evidence are ours.

---

## 3. Architecture

### 3.1 Principles

Each principle has a test. A principle without a test is a slogan.

**P1 — The harness gets a client, never a store.**
Memory, skills, and evidence live in infrastructure we would have chosen independently of any agent framework.
*Test:* point a second harness at the gateway and run the eval suite. If it needs schema changes, we leaked framework concepts into the store.

**P2 — Enforce at the tool boundary, never in the prompt or the loop.**
Middleware is harness-shaped and non-portable. MCP servers and sandbox policy are portable and are the only enforcement we can attest to.
*Test:* can a control be bypassed by changing the system prompt or adding custom middleware? If yes, it is not a control.

**P3 — Evidence is emitted at the point of action, not reconstructed from traces.**
A trace is a copy. The evidence record is the original.
*Test:* if the observability vendor were removed tomorrow, would we still have the audit trail?

**P4 — Ephemeral state may be framework-owned; semantic state may not.**
Thread state, checkpoints, scratch files: let the harness own them. Memory, skills, evals, evidence: never.
*Test:* if we lost it, could we recreate it by re-running? If yes, the harness may own it.

**P5 — Every layer must be independently deployable and independently killable.**
*Test:* can we turn off the agent and keep the gateway? Can we swap the gateway's storage without touching the agent?

### 3.2 Layer model

```
┌──────────────────────────────────────────────────────────────┐
│  CLIENTS                                                     │
│  Copilot (IDE)  ·  Deep Agents CLI  ·  future harnesses      │
└───────────────────────────┬──────────────────────────────────┘
                            │  MCP (tools)
┌───────────────────────────▼──────────────────────────────────┐
│  CONTROL PLANE — ours, in our cloud, harness-agnostic        │
│                                                              │
│  ┌────────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ Context Gateway│  │ Policy /     │  │ Evidence Log     │  │
│  │ (MCP server)   │  │ AuthZ        │  │ (append-only)    │  │
│  └───────┬────────┘  └──────┬───────┘  └────────┬─────────┘  │
│          │                  │                   │            │
│  ┌───────▼──────────────────▼───────────────────▼─────────┐  │
│  │ Knowledge graph  ·  Skills (Git)  ·  Evidence store    │  │
│  └────────────────────────────────────────────────────────┘  │
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│  EXECUTION — replaceable                                     │
│  Deep Agents · backends (local shell / sandbox) · Vertex     │
└──────────────────────────────────────────────────────────────┘
```

The horizontal line between control plane and execution is the architecture. Everything above it we own forever. Everything below it we expect to replace.

### 3.3 Components

**Context Gateway (MCP server)** — the single interface between any agent and our internal knowledge. Exposes *tools*, not resources, because major clients including Copilot's cloud agent support MCP tools only. Every call authenticated, authorised, and logged.

Initial tool surface — start narrow, resist the platform instinct:

| Tool | Purpose |
|---|---|
| `lookup_service_context` | Ownership, dependencies, criticality for a service or repo |
| `get_control_requirements` | Applicable controls / standards for a change class |
| `recall` | Retrieve prior assertions relevant to a subject |
| `record_evidence` | Write an evidence record (called by the harness middleware) |

**Evidence Log** — append-only, our schema, our store. See Section 6.

**Skills library** — `SKILL.md` files (YAML frontmatter + markdown) in Git. Git is the system of record; the harness gets a checkout. Reviewable, diffable, portable across harnesses that support the format.

**Memory store** — deferred until Phase 3 or the second team, whichever comes first. At pilot scale, accumulated memory is small enough to rebuild in a week. Do not build a memory platform before there is memory worth keeping.

**Harness (Deep Agents)** — configured to own nothing durable.

### 3.4 The portability contract

Written down so it survives staff turnover and schedule pressure.

**The harness MAY own:** conversation state, checkpoints, scratch files under `/tmp` and `/workspace`, tool-call retry logic, summarisation and compaction, prompt assembly.

**The harness MAY NOT own:** memory as system of record, skills as system of record, the evidence log, policy decisions, agent identity, credentials.

**Banned by name** — these fail silently and look identical to the correct choice:

| Banned | Why |
|---|---|
| `ContextHubBackend` | Vendor-hosted memory. Same ergonomics as `StoreBackend`, opposite risk |
| `StoreBackend(store=None)` | Binds to whatever store the runtime supplies at call time |
| Evidence written only to a tracing vendor | Trace is a copy, never the original |
| Skills defined in framework config rather than Git | Un-diffable, un-reviewable, un-portable |
| Any hosted service in the runtime critical path | An open licence protects against code withdrawal, not service withdrawal |

---

## 4. Deep Agents configuration

Concrete shapes for the first iteration. Verified against `deepagents` 0.6.x source.

### 4.1 Backend routing

```python
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend

backend = CompositeBackend(
    default=StateBackend(),                       # ephemeral, harness-owned — fine
    routes={
        "/skills/":   StoreBackend(store=GitSkillStore(), namespace=ns),
        "/memories/": StoreBackend(store=GatewayStore(),  namespace=ns),  # Phase 3+
    },
)
```

`StoreBackend` takes an explicit `store: BaseStore | None`. **Always pass it.** Leaving it `None` resolves the store from the runtime at call time, which is exactly the coupling P1 forbids. That single keyword argument is the difference between owning our memory and renting it.

### 4.2 Agent assembly

```python
from deepagents import create_deep_agent

agent = create_deep_agent(
    model=vertex_model,                    # explicit; the default is deprecated
    system_prompt=load_prompt(),
    tools=[*gateway_mcp_tools],            # our MCP server
    backend=backend,
    skills=["/skills/"],
    permissions=[...],                     # typed filesystem permissions
    interrupt_on={...},                    # HITL on write-class tools
    middleware=[
        EvidenceMiddleware(sink=evidence_sink),   # ~100 LOC, non-negotiable
        ToolErrorMiddleware(),
        ModelCallLimitMiddleware(run_limit=N, exit_behavior="end"),
    ],
)
```

### 4.3 Middleware budget

Custom `AgentMiddleware` is non-portable by construction. **Ceiling: 1,500 LOC.** Past that, logic belongs in an MCP server instead. Track this number; report it in the quarterly portability test.

The only middleware that is genuinely required in Phase 1 is `EvidenceMiddleware`. Everything else is convenience.

### 4.4 Local → cloud

Deliberately a constructor argument, not a migration:

| | Local (Phase 1) | Cloud (Phase 3) |
|---|---|---|
| Execution | `LocalShellBackend` | Sandbox backend |
| Model | Vertex | Vertex |
| Gateway | localhost MCP | Cloud Run, private ingress |
| Evidence | BigQuery (dev dataset) | BigQuery (governed dataset) |
| Runtime | CLI on engineer's machine | Cloud Run / GKE |

Agent code is identical. This is the main reason Deep Agents is the first iteration.

---

## 5. Implementation plan

### Phase 0 — Baseline (2 days)

- Select the use case. **Selection criterion: work that Copilot structurally cannot do**, because the data or system cannot leave our boundary — not work Copilot does slightly worse.
- Capture the current metric in writing, in units leadership already reports.
- Identify one team, 5–8 engineers, who volunteered.

*Exit:* a named use case, a named team, a number on paper.

### Phase 1 — Local pilot (weeks 1–3)

**Build:** Deep Agents + `LocalShellBackend` + thin CLI. Vertex models. One MCP connection to the gateway, read-only, 2–4 tools. `EvidenceMiddleware` writing to BigQuery.

**Do not build:** sandboxes, async triggers, GitHub App, PR automation, dashboard, memory service, portability test, governance review.

*Exit:* engineers using it on real work; evidence rows accumulating.

### Phase 2 — Measure (weeks 4–6)

Tune prompt and context. Expect the wins to come from context quality, not from the harness.

*Deliverable:* baseline, after, sample size, honest failure modes.

**Kill gate:** if the metric has not moved by week 6, stop and say so publicly. A visible, honest kill buys more credibility than a rescue — and is why the pilot is small.

### Phase 3 — Cloud (weeks 7–12)

- Gateway to Cloud Run, private ingress, workload identity
- Sandbox backend replaces local shell
- Evidence dataset moves under formal retention and access control
- Cloud Trace via OTEL — as a *copy*, never the record
- First run of the portability test (Section 7.2) while it is still cheap to fix

*Exit:* same use case running unattended-capable in our cloud, evidence intact.

### Phase 4 — Production shape (post-week 12, gated on Phase 3)

Async triggers, machine identity with short-lived credentials, HITL approval on write-class actions, second use case, second team. Memory store lands here or at second-team onboarding, whichever comes first.

### Sequencing rationale

We front-load the metric and defer the platform. This inverts the instinct to build foundations first, and it is deliberate: an internal platform with no users and no number does not get cut for being wrong — it gets cut for being invisible. The one exception is evidence logging, which is the only piece that cannot be backfilled.

---

## 6. Evidence schema

Emitted per tool call from run one. Not reviewed by anyone in Phase 1. Just written.

```
run_id             stable across a task
parent_run_id      subagent lineage
agent_principal    the non-human identity acting
delegated_by       the human on whose authority it acts
intent             the task as stated
tool               tool name
args_hash          hash, not payload
policy_decision    allow | deny | escalate
approval_ref       HITL approval id, where applicable
result_hash        hash, not payload
model_id           model + version
harness_version    deepagents==x.y.z
timestamp
```

`harness_version` is a **field, not a dependency** — the record outlives the harness that produced it. Hash rather than store payloads in Phase 1 to avoid a data-classification conversation before there is anything to govern.

**Why this is the one non-negotiable:** most technical debt is refinanceable. This is not. If the pilot works and someone asks in month four what it has been doing, "we will add logging now" leaves us with no history for exactly the period that matters.

---

## 7. Strategic direction

### 7.1 Harness replaceability as a managed property

Expected useful life of the current harness: 12–24 months. This is not pessimism — it is the observed cadence of the category.

Managed by three mechanisms:

1. **Vendor the library.** Fork `deepagents` into the internal registry at a pinned SHA. MIT permits it. A hostile relicence, an abandoned repo, or a breaking release becomes a scheduling decision rather than an incident.
2. **Cap interface debt.** The 1,500 LOC middleware ceiling, reported quarterly.
3. **Prove the exit.** Section 7.2.

### 7.2 The portability test

Quarterly, half a day, one engineer: point a second harness at the gateway and run the eval suite. Not a migration — a smoke test.

Report a single number to leadership: **harness switching cost, in engineer-weeks, verified [date].** Target: one sprint. If it ever exceeds that, the harness has entered the boundary and we fix it that quarter, while it is still cheap.

This converts an architectural aspiration into a governable metric.

### 7.3 When to move to ADK

ADK is the leading Phase 4+ candidate, not a Phase 1 one. Move when **two or more** of the following are true:

- Deployment and IAM burden of self-hosting the LangGraph runtime exceeds the cost of migrating
- We need Java or Go agents, which LangChain does not serve
- Platform-native audit emission materially reduces our evidence-plumbing cost
- Deep Agents' pre-1.0 breaking-change cadence becomes an operational tax

Because of P1, this is a runtime swap, not a rebuild. Adopt the ADK runtime if we go; do **not** adopt its managed memory or session services as systems of record — that reproduces the dependency we are avoiding, just with a vendor we already trust.

### 7.4 Longer arc

- **Skills as an organisational asset.** Once skills are in Git and reviewed, they become a durable encoding of institutional practice — arguably more valuable than any agent that reads them, and portable across every harness.
- **Agent identity as first-class.** Non-human identity with lifecycle, scope, and revocation. This connects directly to existing consent-chain work and is where the platform stops being a productivity tool and becomes infrastructure.
- **Federation before centralisation.** Multiple gateway instances with central policy scales better than one gateway for everything, and handles residency naturally.

---

## 8. Leadership watch list

Signals with a trigger and a pre-agreed response, so the response is a decision rather than a scramble.

| # | Signal | Trigger | Response |
|---|---|---|---|
| W1 | Harness churn | Breaking release, project stall, or licence change in `deepagents` | Pin to vendored SHA; evaluate at next quarterly review. Not an emergency |
| W2 | Interface debt | Custom middleware exceeds 1,500 LOC | Refactor logic into MCP servers that quarter |
| W3 | Portability drift | Switching cost exceeds one sprint | Treat as a P1 violation; remediate before next phase |
| W4 | Ungoverned MCP in developer tooling | Any MCP server reaching internal systems without allowlist review | Claim the governance mandate; publish the allowlist policy |
| W5 | Regulatory scope change | Legal confirms an AI-specific obligation applies to part of the estate | Evidence log moves from good practice to control evidence; formalise retention |
| W6 | Vendor capability convergence | Copilot or ADK ships governed, in-boundary execution | Re-run the build/buy decision honestly. Shrinking our scope is a success, not a defeat |
| W7 | Pilot stall | No metric movement by week 6 | Kill publicly. Do not extend |
| W8 | Scope creep | Requests for dashboard, second use case, or memory platform before the gate | Deferred to post-gate. The answer is "after the number" |

W6 deserves emphasis. The correct posture is that we would be pleased to build less. The platform exists to close a gap, not to defend headcount — and saying so in advance is what makes the rest of this document credible.

---

## 9. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Pilot shows no measurable gain | Medium | Low if killed fast | Small scope, hard gate, public kill |
| Perceived as duplicating Copilot | **High** | High | Use-case selection criterion in Phase 0; never pitch on productivity alone |
| Harness breaking changes (pre-1.0) | High | Low | Vendored fork, pinned SHA |
| Evidence gap discovered late | Low | **Severe** | Evidence from run one — the single non-negotiable |
| Governance review blocks Phase 3 | Medium | Medium | Evidence log exists before the review, not after |
| Platform outgrows team capacity | Medium | Medium | Defer memory service; federate rather than centralise |
| Regulatory claim overstated to leadership | Medium | High | Verify A4 with Legal before any external-facing material |

---

## 10. Open questions

1. Which use case satisfies the Phase 0 criterion? (Requires internal inventory knowledge)
2. Does GitHub Enterprise Server change the Copilot side of the split?
3. Who owns MCP allowlist policy today — and if nobody, do we claim it?
4. Vertex model choice and residency posture for the pilot
5. Evidence retention period and access model — Legal and Records input required

---

## Appendix A — Deliberately not doing

Recorded so that deferral reads as a decision rather than an omission.

- Building a coding agent to compete with Copilot
- Forking or deploying Open SWE (used as a pattern reference only — its middleware directory is a useful hardening checklist for Phase 3)
- A memory platform before the second team
- Multi-agent orchestration before a single agent produces a number
- A dashboard before there is anything worth displaying
- Any hosted third-party service in the runtime or audit path
