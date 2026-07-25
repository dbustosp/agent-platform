---
name: incident-context-assembly
description: Assemble the ownership, dependency, and criticality picture for a service during an incident, plus any prior assertions about it. Use at the start of an incident when someone needs to know who owns a failing service, what it depends on, and what is already known about it.
---

# Incident context assembly

The first ten minutes of an incident are spent answering questions that have
already been answered somewhere. This skill answers them from the gateway
instead of from the memory of whoever happens to be online.

It assembles context. It does not diagnose, and it does not act.

## Workflow

### 1. Start from the failing service

Call `lookup_service_context` on the service that is alerting.

Record the owner, the criticality tier, and the dependency list verbatim. Do not
summarise the dependency list — during an incident the entry that gets dropped
in summarisation is reliably the one that mattered.

### 2. Walk one level of dependencies

For each direct dependency, call `lookup_service_context` again.

One level, not the transitive closure. A full graph walk during an incident
produces a wall of output nobody reads. If a dependency is itself tier-1, note
it prominently — a tier-1 service depending on a degraded tier-1 service is the
shape of an incident that is about to get worse.

Stop early if the dependency list is large: report the tier-1 dependencies and
say explicitly how many you did not expand.

### 3. Retrieve prior assertions

Call `recall` on the failing service, then on any tier-1 dependency.

You are looking for things that change the interpretation of the current
symptoms: known gaps, documented exceptions, recent changes, previous incidents
with the same signature. A prior assertion that this service has a known
capacity limit is worth more than any inference you can make from the alert.

### 4. Note what you could not find

An explicit gap is actionable. Silence is not. If the gateway has no record of a
service in the dependency chain, say so — an unowned service in the path of a
tier-1 incident is itself a finding worth reporting.

## Output

Lead with the answer to the question actually being asked, which is almost
always "who do I page":

1. **Owner of the failing service** — first line, no preamble
2. **Criticality** — tier, and what that implies for response
3. **Dependencies** — flat list, tier-1 entries marked, with owners
4. **Prior assertions** — anything known about these services already
5. **Gaps** — services in the chain the gateway does not know about

Keep it short enough to read on a phone.

## Constraints

- Assemble context only. Do not propose remediation, do not restart anything, do
  not open or modify a ticket. The gateway is read-only in this phase and that
  matches the intended scope of this skill.
- Do not speculate about root cause. Everything you report should be traceable
  to a specific tool result.
- If asked to act, decline and hand back to the incident commander named as the
  service owner.
