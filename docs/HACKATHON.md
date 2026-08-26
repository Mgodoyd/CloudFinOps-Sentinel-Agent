# All Things Agentic Hackathon

> **Ready, Set, Agent!** Build next-generation agents that run in the background,
> handle the heavy lifting of massive datasets, and automate complex workflows
> asynchronously.

<sub>Verbatim copy of the event brief as published on Devpost, formatted for
readability. No wording has been changed, added or removed — typos included.
Captured 26 August 2026. How this project answers it:
**[SUBMISSION.md](SUBMISSION.md)**.</sub>

---

## At a glance

| | |
|---|---|
| **Deadline** | 31 ago 2026 @ 6:00pm CST · *5 days to deadline* |
| **Prize pool** | $180,000 in cash |
| **Participants** | 8476 participants |
| **Host** | Google · Managed by Devpost |
| **Format** | Online · Public |
| **Categories** | Enterprise Machine Learning/AI Productivity |

**Who can participate**

- Above legal age of majority in country of residence
- Specific countries/territories excluded

<sub>`Join hackathon` · `View full rules` · `View schedule` · `Devpost icon rgb30px`</sub>

---

## About

A global hackathon to build next-generation AI agents on Gemini and Google
Cloud. All skill levels welcome.

Most AI today waits for you to ask. The next generation doesn't. AI agents are
systems that can take a goal, make a plan, and actually carry it out — pulling
information, making decisions, and completing multi-step tasks on their own,
while you do something else.

All things agentic hacakthon is a global hackathon that challenges you to build
one. Using Gemini, Google's open-source Agent Development Kit (ADK), and Google
Cloud, you'll create an agent that takes real action to remove everyday friction
— at work, at home, or across an entire enterprise.

You don't need to be an AI researcher to take part. Whether you're a seasoned
engineer or building your first agent, we give you the tools, starter guides,
and $150 in Google Cloud credits to go from idea to working demo. Pick a track,
build your agent, and show us what "autonomous" really looks like.

Whether you're a full-stack engineer, a system architect, or a startup founder,
this hackathon hands you the tools to build agents that actively work for
everyone. Redefining interaction — from static chatbots to immersive
experiences.

---

## How to Get Started

Follow these steps to go from sign-up to submission in a weekend and lean on the
Resources tab, packed with guides, credits, and cost-saving tips to help you win.

1. **Get your tools.** Sign up for a no-cost Google Cloud trial, then grab your
   $150 in Google Cloud credits using the credit form on the Resources tab.
2. **Learn the basics.** New to agents? The beginner guides in Resources walk you
   through what an agent is and how to build your first one with ADK — no
   experience required.
3. **Pick a track.** Choose the one that fits your idea: The Taskmaster, The
   Collaborative Partner, or The Fortified Enterprise Fleet. Enter any track you
   like — full track breakdowns are waiting in Resources.
4. **Build on Gemini + Google Cloud.** Keep your spend low with the cost-saving
   tips in Resources.
5. **Submit before the deadline:** a demo video, your code repo, an architecture
   diagram, and a short write-up. Full checklist below under "What to Submit."

Everything you need is one click away — hit View Resources to explore the guides,
credits, and track deep-dives.

> **Tips to be successful:** solve a real, specific problem you actually have;
> show your agent doing something, not just talking; keep your demo video tight
> and show it working live; and document your project so a judge can follow it.

---

## Requirements

### What to Build

Build and deploy a next-generation, autonomous AI Agent leveraging Gemini 3.5
Flash that operates beyond standard chat loops. The system can run
asynchronously in the background, handle the heavy lifting of complex workflows,
or dynamically manipulate data pipelines and representations.

Projects must be built within one of these three categories:

#### 🗂️ Taskmaster

Build a complete workflow, not just a chatbot. Don't just make an agent that
writes text. Make one that takes action. Find a messy, multi-step chore in your
job, classes, or personal life. Build an agent that handles the details, sends
the right info to the right places, and proves it can do the heavy lifting for
you.

#### 🤝 Collaborative Partner

Build an agent that leads the way and takes notes. It should ask clarifying
questions, guide the user step-by-step, and have a clear way to capture
feedback, so it constantly adapts to the user's unique way of thinking.

#### 🏛️ Fortified Enterprise Fleet

Build a scalable network of institutional agents that hook into official
enterprise infrastructure. Teams must demonstrate how agents are cataloged for
cross-department use, how they safely maintain context across weeks of
asynchronous operations, and how they interact with production data without
violating enterprise compliance, data sovereignty, or security policies.

| Area | Components |
|---|---|
| **Discovery & Lifecycle** | Agent Registry (the central repository for publishing, versioning, and discovering enterprise-approved agents). |
| **Core Execution & State** | Agent Runtime (for long-running, asynchronous background execution) and Memory Bank (for persistent, secure cross-session context over extended timelines). |
| **Security & Governance** | Agent Identity (For zero-trust access control), Agent Gateway (for unified routing and policy enforcement), and Model Armor (inline guardrails to block prompt injection, tool poisoning, and PII leaks). |
| **Telemetry** | Agent Observability (OpenTelemetry-compliant audit logs and end-to-end reasoning chain traces). |

Recommended Tech to use (Gemini Enterprise Agent Platform):

---

### Every project, in every track, must use

- **Gemini 3.5 or newer** accessed through Gemini API or Vertex AI
- **At least one Google Agent Framework:** Google ADK, GenAI SDK, Antigravity
  SDK or GenKit
- **At least one Google Cloud infrastructure service** (such as Cloud Run,
  Cloud SQL, Firestore, GKE, Pub/Sub).

> **Note on cost & deployment:** Your app does not need to be publicly accessible
> or live at the exact moment of submission or judging (so you don't rack up
> unnecessary costs). You just need to provide clear proof that it was built and
> deployed on Google Cloud — for example, shown in your demo video and code
> repository. See Resources for tips on keeping your costs near zero.

---

## What to Submit

**Category**

**URL to the hosted Project** *(if available)* for judging and testing, such as
web UI, Chrome Extension, mobile app, etc. A hosted project is highly
encouraged.

**Text description**

- Features and functionality
- Technologies used
- Other data sources used
- Findings and learnings

**URL to your public or private code repository** (on Github, Gitlab, or
Bitbucket) to show how your project was built. If your repo is private, share it
with testing@devpost.com and cloudhackathons@google.com

**Spin-up Instructions:** A step-by-step guide in your README.md explaining how
to set up and run the project locally or deploy it to the cloud. Even if the
judges do not run it, these instructions prove the project is reproducible.

**Architecture Diagram** with a clear visual representation of your system (e.g.,
how Gemini connects to your backend, database, and frontend).

**~ 4-min Demo video**

- Short overview of the problem your Project is solving
- Value proposition
- Demo of the app in action
- Must demonstrate the backend is running on Google Cloud (ie: Google Cloud
  Console, Cloud Run dashboard, Vertex AI logs, URL of .run, etc)

### For Bonus Points

Optionally you can do one or both of the following:

- **Publish a piece of content (blog, podcast, video):** Covering how the project
  was built on any public platform (e.g., medium.com, dev.to, YouTube, etc.). The
  content must be public (not unlisted). You must include language that says you
  created the piece of content for the purposes of entering this hackathon.
- **Publish a social media post:** Highlight or promote your project on social
  media post on X, LinkedIn, Instagram, or Facebook. For any social media posts
  on platforms such as X or LinkedIn, include the hashtag
  `#AllThingsAgenticHackathon`.
- **Successfully integrate Google AI models** such as Gemma, Veo or Lyria.

<sub>Questions? Start with the FAQs for the quick answers on eligibility, tracks,
credits, and submissions and see the Official Rules for anything binding.</sub>

---

## Prizes

**$180,000 in prizes**

| Prize | Cash | Winners |
|---|---|---|
| **Grand Prize** | $50,000 | 1 |
| **The Taskmaster** | $20,000 | 1 |
| **The Collaborative Partner** | $20,000 | 1 |
| **The Fortified Enterprise Fleet** | $20,000 | 1 |
| **Startup Excellence** *(Incorporated Organizations eligible — see rules for details)* | $20,000 | 1 |
| **Individual/Hobbyist** *(Best Team/Solo Build)* | $10,000 | 2 |
| **Best Architectural Design** | $5,000 | 2 |
| **Best Multimodal UX** | $5,000 | 2 |
| **Honorable Mentions** | $2,000 | 5 |

<details>
<summary><b>Full prize detail</b></summary>

**Grand Prize** — $50,000 in cash · 1 winner
- $50,000 in USD
- $5,000 in Google Cloud Credits for use with a Cloud Billing Account
- Virtual Coffee with a Google Team Member
- Social Promo

**The Taskmaster** — $20,000 in cash · 1 winner
- $20,000 in USD
- $2,000 in Google Cloud Credits for use with a Cloud Billing Account
- Virtual Coffee with a Google Team Member
- Social Promo

**The Collaborative Partner** — $20,000 in cash · 1 winner
- $20,000 in USD
- $2,000 in Google Cloud Credits for use with a Cloud Billing Account
- Virtual Coffee with a Google Team Member
- Social Promo

**The Fortified Enterprise Fleet** — $20,000 in cash · 1 winner
- $20,000 in USD
- $2,000 in Google Cloud Credits for use with a Cloud Billing Account
- Virtual Coffee with a Google Team Member
- Social Promo

**Startup Excellence** (Incorporated Organizations eligible - see rules for
details) — $20,000 in cash · 1 winner
- $20,000 in USD
- $5000 in Google Cloud Credits for use with a Cloud Billing Account
- Virtual Coffee with a Google Team Member
- Social Promo

> Must be submitting on behalf of an organization that is incorporated, and you
> must provide a corporate email address

**Individual/Hobbyist (Best Team/Solo Build)** — $10,000 in cash · 2 winners
- $10,000 in USD
- $1000 in Google Cloud Credits for use with a Cloud Billing Account
- Virtual Coffee with a Google Team Member
- Social Promo

**Best Architectural Design** — $5,000 in cash · 2 winners
- $5,000 in USD
- $1000 in Google Cloud Credits for use with a Cloud Billing Account

**Best Multimodal UX** — $5,000 in cash · 2 winners
- $5,000 in USD
- $1000 in Google Cloud Credits for use with a Cloud Billing Account

**Honorable Mentions** — $2,000 in cash · 5 winners
- $2,000 in USD
- $500 in Google Cloud Credits for use with a Cloud Billing Account

</details>

---

## Judging Criteria

| Criterion | Weight | What it means |
|---|---|---|
| **Innovation & Operational Utility** | **40%** | How much real-world friction does the agent remove on its own? We reward autonomous, high-value action over simple chat — agents that make decisions and complete tasks with little to no hand-holding. |
| **Architectural Discipline & Tech Stack** | **30%** | How sound are your engineering choices? We look at how you decouple systems, manage state and memory, secure credentials, and handle failures — robust, production-minded agents, not brittle scripts. |
| **Demo & Production Readiness** | **30%** | How clearly do your video and repo prove it works? We want a live, unedited demo, a clean architecture diagram, reproducible setup, and visible proof it runs on Google Cloud. |
