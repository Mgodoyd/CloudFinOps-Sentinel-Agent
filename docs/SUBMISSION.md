# Submission notes

> The event's own text, unedited, is kept in
> [HACKATHON.md](HACKATHON.md). This file maps it to the project.

A map from what the hackathon asked for to where this project answers it, so a
judge can check a requirement without reading the whole codebase first.

- **Hackathon** · All Things Agentic, run by Google on Devpost
- **Deadline** · 31 August 2026, 18:00 CST
- **Track** · **The Taskmaster**
- **Live service** · Cloud Run, `us-central1`
- **Repository** · <https://github.com/Mgodoyd/CloudFinOps-Sentinel-Agent>

---

## What the hackathon asked for

> *Most AI today waits for you to ask. The next generation doesn't. AI agents
> are systems that can take a goal, make a plan, and actually carry it out —
> pulling information, making decisions, and completing multi-step tasks on
> their own, while you do something else.*

Build and deploy an autonomous agent on **Gemini 3.5 or newer**, using at least
one **Google agent framework** and at least one **Google Cloud infrastructure
service**, that operates beyond a chat loop — running asynchronously in the
background and handling the heavy lifting of a real workflow.

## Why The Taskmaster

The track asks for *"a complete workflow, not just a chatbot"* — an agent that
finds a messy, multi-step chore, handles the details, **sends the right info to
the right places**, and does the heavy lifting.

Cloud waste is exactly that chore. It is not hard to understand; it is hard to
stay on top of. A service is provisioned at 4 GiB "to be safe" and never touched
again. Someone sets `min-instances=1` to dodge cold starts on a service that
serves forty requests a day, and it bills around the clock forever. A disk
outlives the VM it was attached to. None of it is difficult to spot — it is just
nobody's job this week, and the bill arrives monthly.

Every hour, unattended, this agent scans four Google Cloud APIs across ten
regions, prices what it finds against what it actually uses, asks Gemini for
judgement and a plan, applies the small reversible changes itself, and pushes
the ones a human still owns to Slack and Telegram. The output is not a report.
It is a changed estate, a queue of decisions waiting for a person, and a full
audit trail of why.

The other two tracks were considered and set aside deliberately. *Collaborative
Partner* wants stateful multi-turn dialogue; this agent does not converse, and
bolting on a chat interface would have diluted it. *Fortified Enterprise Fleet*
wants a **network** of catalogued agents; this is one agent, and three of the
pieces that track names are here (Memory Bank, Agent Observability, long-running
asynchronous execution) but a registry and a gateway are not.

---

## Required technologies

| Requirement | How it is met | Where |
|---|---|---|
| **Gemini 3.5 or newer**, via Gemini API or Vertex AI | `gemini-3.5-flash-lite` for per-resource judgement and planning. `USE_VERTEX=true` switches the same code to Vertex AI. | [`app/core/analyst.py`](../app/core/analyst.py), [`app/core/planner.py`](../app/core/planner.py) |
| **A Google agent framework** | **GenAI SDK** (`google-genai`) — structured output via `response_schema`, so the shape is validated by the SDK rather than parsed hopefully. | [`app/core/agent.py`](../app/core/agent.py) |
| **A Google Cloud infrastructure service** | **Cloud Run** (the service), **Firestore** (Memory Bank), **Cloud Scheduler** (hourly trigger), **Secret Manager** (credentials), **BigQuery** (billing export), **Cloud Trace** (spans). | [`deploy/deploy.sh`](../deploy/deploy.sh) |
| **Runs asynchronously in the background** | Cloud Scheduler drives an hourly audit into `/webhook/pubsub`. Nothing needs a browser open. | [`app/main.py`](../app/main.py) |
| **Beyond a chat loop** | Observe → Analyse → Plan → Execute → Adapt → Remember. Two model calls per audit, a fixed toolbox, and an autonomy matrix enforced in code. | [README · the agent loop](../README.md#the-agent-loop) |

### Bonus: another Google AI model

**Gemma 4 31b** is the second tier. When Gemini returns nothing — quota,
capacity, a timeout — Gemma writes the fleet summary the report would otherwise
lose, and the per-resource judgement degrades to deterministic rules. Same API,
same SDK, so it is a model name rather than a second integration.

The narrowness of its job is measured, not stylistic: asked for the analyst's
full per-resource schema Gemma did not answer inside a usable deadline, while
the same fleet summarised in a paragraph came back in about eighteen seconds.
[The numbers are in the README](../README.md#degradation-has-three-steps-not-two).

---

## Judging criteria

### Innovation & Operational Utility — 40%

> *How much real-world friction does the agent remove on its own?*

**The friction is mine.** This was built on $150 of hackathon credits, in the
project it now audits. Its first real run found a webhook listener left up with
`min-instances=1` at 1% CPU — $52.24 a month for nothing — an orphaned disk, and
a static IP reserved to no one. Every one of those is my own leftover, from
building this.

It also found itself. `cloudfinops-sentinel` sits in its own inventory at
$6.88/month. That was not designed; it falls out of treating the project as the
estate, with no exceptions — and an agent that exempts itself from the rule it
enforces is not one I would trust with write credentials.

It runs hourly with nobody watching, and it acts. Level 1 changes — reversible,
under $40/month — are applied without asking. Level 2 changes become a ticket
that reaches the operator's phone. It remembers what it fixed, what shape each
resource had when it acted, and what a human declined, so it never proposes the
same thing twice and never re-raises a rejected change.

**The workflow completes without intervention.** What waits for a person is
only what is irreversible or expensive, which is a boundary drawn on purpose
rather than a step left unfinished. Deleting a disk unattended would not make
the agent more autonomous, only less careful.

### Architectural Discipline & Tech Stack — 30%

> *Robust, production-minded agents, not brittle scripts.*

| | |
|---|---|
| **Decoupling** | Measurement, judgement, planning, gating and execution are separate modules with one job each. The model can be removed and the audit still completes. |
| **State** | Firestore on Cloud Run, a JSON file locally, chosen automatically. Simulated and real runs get separate memory banks so a demo cannot queue a change against a live project. |
| **Credentials** | Secret Manager for the Gemini key, the dashboard token, the bot token and the Slack webhook. No default in code, and [a test](../tests/test_no_committed_secrets.py) that fails the build if a credential is committed. |
| **Failure handling** | Gemini → Gemma → deterministic rules. A failed plan step is re-planned around, up to twice. Firestore unreachable starts anyway. Every degraded run says so. |
| **Security** | Token auth with no development bypass, a separate scheduler credential, constant-time comparison, and [prompt-injection guardrails](../README.md#the-estate-is-untrusted-input) treating resource names as untrusted input. |
| **Retrieval** | Deliberately none. Every lookup into memory is exact and by a known key — `check_history(resource_id)`, `last_rejection(resource_id)` — which is what a key-value store is for. A vector index would add infrastructure and latency to answer a question nobody is asking: semantic similarity is the wrong retrieval model when the identifier is already exact. It would earn its place only if the agent accumulated hundreds of free-text rejection reasons and had to ask "have humans refused anything like this before?" |
| **Tests** | 418 passing, no credentials required, hermetic. Several exist because the bug happened: the approval contract, simulated/real isolation, the duplicate notification. |

### Demo & Production Readiness — 30%

> *Visible proof it runs on Google Cloud.*

[Six captures from the deployed service](../README.md#running-on-google-cloud):
the deck on its `.run.app` URL reading real Cloud Run services through Cloud
Monitoring with live writes enabled, the Cloud Run console, the Cloud Scheduler
job, the Memory Bank document in Firestore, OpenTelemetry spans in Cloud Trace,
and an approval arriving on a phone.

`./deploy/deploy.sh` is one command: APIs, a least-privilege service account,
Firestore, secrets, build, deploy, and the hourly schedule.

---

## Submission checklist

| Item | Status |
|---|---|
| Hosted project URL | Cloud Run, `us-central1` |
| Spin-up instructions in README | [Spin-up instructions](../README.md#spin-up-instructions) |
| Architecture diagram | [`docs/img/architecture.png`](img/architecture.png), plus [the decision model](img/decision-model.png) |
| Public code repository | this repository |
| Proof it runs on Google Cloud | [Running on Google Cloud](../README.md#running-on-google-cloud) |
| Findings and learnings | below |
| ~4-minute demo video | *submitted on Devpost* |
| Bonus — another Google AI model | Gemma 4 31b as the second tier |

---

## Findings and learnings

**The bug that taught me most.** The agent opened a ticket reading "reduce to
1 vCPU and 2Gi" and applied 512Mi. The planner returns only the dimensions it
cares about — a step meaning "scale to zero" carries `min_instances` and no
memory — and the executor filled the gap with a constant. On a service with a
5 GB observed peak that is an out-of-memory kill in production. It appeared in
four separate code paths, two of which execute without a human. The lesson: in a
human-in-the-loop system, what is approved and what is executed have to come
from the same data structure, never from two paths that ought to agree.

**Thresholds must be tested against measurement, not against the model.** The
autonomy level and the booked savings were being compared against Gemini's
`estimated_saving`. The model guessed $250 where the cost model computed
$148.15. If the number deciding whether a human must approve comes from the
model, the autonomy matrix means nothing.

**A model narrates changes it does not encode.** It said "2 vCPU and 8Gi" in
prose and returned `cpu: "1"` in the arguments — a combination Cloud Run rejects
at deploy time. Prose from an LLM is not a contract; the structure has to be
validated against the provider's own rules.

**The estate is untrusted input.** Resource names go into the prompt of an agent
holding write credentials, and anyone who can deploy a service chooses them.
Writing the guardrails surfaced two bugs in the guardrails themselves: an edit
to the system instruction had silently not applied, and the marker patterns
matched on whitespace when GCP names use hyphens — so they would have caught
nothing that can exist in a real project.

**Models are retired for new keys only.** `gemini-2.5-flash` appears in
`models.list()` and returns 404 when called with a recently created key. A 404
there means "not available to your key", not "your key is invalid", and sending
someone to regenerate a working credential is the worst possible error message.

**Live models are not a lighter Flash.** They speak `bidiGenerateContent` over a
WebSocket. Configuring one produces a failure that looks like a broken
credential.

**The free tier forces architecture.** Five requests per minute means a
tool-calling loop exhausts the minute on a single audit. Two structured calls —
one to judge the whole fleet, one to plan — is not an optimisation, it is what
makes the system viable.

**Ask a model for work it can deliver.** Gemma could not return the analyst's
per-resource schema inside any usable deadline: over 100 s for a single
resource, `504` for the fleet. The same fleet summarised in a paragraph came
back in 18 s. Measuring first turned a feature that would have timed out in the
demo into one that works.

**"Tolerated" exists for human reasons, not technical ones.** A resource with
$1/month of recoverable waste is correctly sized in practice. Painting it red
implies an action nobody will take, and a fleet that always looks broken teaches
operators to ignore the colour.
