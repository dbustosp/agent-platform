---
name: service-change-review
description: Assess a proposed change to an internal service against its ownership, criticality, and the control requirements that apply to its change class. Use when someone asks whether a change is safe to make, what approvals it needs, or which controls apply.
---

# Service change review

A change is reviewable when three things are known: who owns the thing being
changed, how much it matters, and which controls apply to a change of that
shape. All three live behind the gateway. None of them live in the repository,
which is why this review cannot be done from the code alone.

## Workflow

### 1. Establish the subject

Identify the specific service or repo being changed. If the request names a
system loosely ("the payments thing"), resolve it to a single service before
continuing. Reviewing the wrong service produces a confident, useless answer.

### 2. Pull ownership and criticality

Call `lookup_service_context` with the service name.

You are looking for:

- **Owner** — who has to agree, and who to route the question to
- **Criticality tier** — sets the bar for everything downstream
- **Dependencies** — the blast radius nobody mentions in the ticket

If the service is unknown to the gateway, stop and say so. Do not infer
ownership from commit history or file paths; an inferred owner is worse than no
owner, because it looks authoritative.

### 3. Classify the change

Name the change class in the vocabulary the control catalogue uses — not in the
words the requester used. Common classes: schema migration, dependency upgrade,
config change, access change, new external egress.

If the change spans two classes, treat it as both. Apply the union of the
requirements, not the intersection.

### 4. Pull the applicable controls

Call `get_control_requirements` with the change class.

For each returned control, state plainly whether the proposed change satisfies
it, does not satisfy it, or cannot be determined from what you have been told.
"Cannot be determined" is a legitimate and useful answer — it names the next
question. Marking an unknown as satisfied is the specific failure this skill
exists to prevent.

### 5. Check for prior assertions

Call `recall` with the service as the subject. A previous review may have
already established something relevant: a documented exception, a known gap, a
decision taken and recorded. Contradicting a prior assertion without noticing it
is the most common way this review goes wrong.

If you find a contradiction, surface it explicitly rather than silently
preferring the newer information.

## Output

Report in this order:

1. **Service** — name, owner, criticality tier
2. **Change class** — as classified, with the reasoning if it was ambiguous
3. **Controls** — one line each: control, verdict (satisfied / not satisfied /
   undetermined), and the evidence for the verdict
4. **Prior assertions** — anything `recall` returned that bears on this change
5. **Blocking items** — the specific things that must be resolved before the
   change proceeds, or "none"

## Constraints

- Every tool call you make is recorded. That is the point; work normally.
- Do not approve anything. This skill produces a review, and the review goes to
  the owner named in step 2. Approval is a human act with an approval reference.
- Do not soften an unsatisfied control into a "consideration". If a control is
  not met, the verdict is not met.
