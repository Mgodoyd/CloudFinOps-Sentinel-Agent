# Architecture

## System

```mermaid
flowchart TB
    subgraph trigger["Triggers"]
        SCHED["Cloud Scheduler<br/><i>hourly</i>"]
        UI_BTN["Operator<br/><i>Run Audit</i>"]
    end

    subgraph run["Cloud Run · cloudfinops-sentinel"]
        API["FastAPI<br/><i>lazy: nothing scans until asked</i>"]

        subgraph agent["Agent core"]
            OBSERVE["1 · Observe<br/>deterministic"]
            ANALYSE["2 · Analyse<br/><b>Gemini</b>"]
            PLAN["3 · Plan<br/><b>Gemini</b>"]
            EXEC["4 · Execute<br/>deterministic"]
            ADAPT["5 · Re-plan on failure<br/><b>Gemini</b>"]
        end

        MATRIX{{"Autonomy matrix<br/><i>enforced in code</i>"}}
        TRACE["Trace + SSE"]
    end

    subgraph gcp["Google Cloud"]
        CR["Cloud Run Admin API"]
        MON["Cloud Monitoring"]
        CE["Compute Engine"]
        AR["Artifact Registry"]
        FS[("Firestore<br/><i>Memory Bank</i>")]
    end

    GEM["Gemini 3.5 Flash Lite<br/><i>GenAI SDK</i>"]
    HUMAN["Human<br/><i>approve / reject</i>"]
    DASH["Command Deck<br/><i>EN · ES</i>"]

    SCHED -->|Pub/Sub push| API
    UI_BTN --> API
    API --> OBSERVE

    OBSERVE -->|read only| CR & MON & CE & AR
    OBSERVE -->|measured facts| ANALYSE
    ANALYSE <-->|structured JSON| GEM
    ANALYSE --> PLAN
    PLAN <-->|ordered plan| GEM
    PLAN --> MATRIX

    MATRIX -->|Level 1| EXEC
    MATRIX -->|Level 2| HUMAN
    MATRIX -->|below threshold| TRACE
    HUMAN -->|approved| EXEC

    EXEC -->|mutations| CR & CE & AR
    EXEC -->|failure| ADAPT
    ADAPT <--> GEM
    ADAPT --> EXEC

    OBSERVE & ANALYSE & PLAN & EXEC --> TRACE
    TRACE -->|Server-Sent Events| DASH
    agent <--> FS
    DASH --> HUMAN

    classDef llm fill:#4a2d7a,stroke:#9b6bff,color:#fff
    classDef det fill:#0d2b4a,stroke:#4d7cfe,color:#fff
    classDef gate fill:#5a3d00,stroke:#ffc44d,color:#fff
    classDef store fill:#0b3d2e,stroke:#2ffcaa,color:#fff
    class ANALYSE,PLAN,ADAPT,GEM llm
    class OBSERVE,EXEC,API det
    class MATRIX,HUMAN gate
    class FS store
```

## The agent loop

```mermaid
sequenceDiagram
    participant T as Trigger
    participant A as Agent
    participant G as Gemini
    participant M as Autonomy matrix
    participant C as Google Cloud
    participant H as Human
    participant F as Firestore

    T->>A: audit
    A->>C: discover (10 regions, 4 APIs, parallel)
    C-->>A: services · disks · IPs · images
    A->>C: Cloud Monitoring — CPU/memory peaks
    C-->>A: p99 over 24h

    Note over A,G: One call for the whole fleet
    A->>G: measured facts only
    G-->>A: verdict · diagnosis · target shape · risk · confidence

    A->>G: plan: which tool, on what, in what order
    G-->>A: ordered steps with intent + expected outcome

    loop each step
        A->>M: may this run unattended?
        alt below action threshold
            M-->>A: skip — costs more attention than it saves
        else Level 1 — reversible, low value
            M->>C: apply
            C-->>A: new revision + applied limits
        else Level 2 — irreversible or high value
            M->>H: approval ticket (model's own wording)
            H-->>C: approved → apply
            C-->>A: GCP confirmation
        end
    end

    opt a step failed
        A->>G: re-plan around the failure
        G-->>A: revised steps
    end

    A->>F: remediations · tickets · runs · resource shapes
    A-->>T: report + live trace
```

## Where the LLM sits

| Stage | Decided by | Why |
|---|---|---|
| Discovery, cost, utilization | Code + GCP APIs | Measurement is fact; a model must not invent a number |
| Diagnosis, recommendation, risk | **Gemini** | Judgement is what a model is for |
| Plan: which tool, what order | **Gemini** | Sequencing under a goal |
| Level 1 / Level 2 / skip | Code | A persuasive model must not talk its way into an irreversible action |
| Execution | Code | One handler per resource type, never inferred |
| Adaptation after failure | **Gemini** | Deciding whether to retry differently or stop |

## Failure behaviour

| Failure | Response |
|---|---|
| Model unavailable / quota / 503 | Deterministic audit completes; the report says so |
| Model unavailable to this key (404) | Walks `MODEL_FALLBACKS` |
| A plan step fails | Re-plans around it, up to twice |
| Model proposes an unknown tool | Step dropped before dispatch, and logged |
| Cloud Monitoring unavailable | Modelled utilization, labelled `MODELLED`, 5-min backoff |
| Compute API disabled | Cloud Run audit continues; the gap is shown, not hidden |
| Firestore unavailable | Runs without history rather than refusing to start |
