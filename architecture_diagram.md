# Architecture Diagram

## Agent Loop and Tool Design (One-Page View)

```mermaid
flowchart LR
    %% ===== Clients and API =====
    subgraph A[Clients and API Layer]
        UI[Frontend or Operator]
        HEALTH[GET /api/v1/health]
        JOBS[POST/GET /api/v1/jobs]
        ART[GET /api/v1/jobs/{job_id}/artifacts/{artifact_name}]
        API[api_server.py FastAPI App]
        UI --> JOBS --> API
        UI --> HEALTH --> API
        UI --> ART --> API
    end

    %% ===== Job control =====
    subgraph B[Job Orchestration and Execution]
        STORE[JobStore in-memory registry]
        QUEUE[outputs/jobs/{job_id}/artifacts]
        WORKER[ThreadPoolExecutor worker]
        BATCH[main.run async batch runner]
        API --> STORE
        STORE --> QUEUE
        STORE --> WORKER
        WORKER --> BATCH
    end

    %% ===== Agent graph =====
    subgraph C[Per-Ticket Agent Graph (LangGraph)]
        P[PARSE_TICKET]
        C1[GET_CUSTOMER]
        O1[GET_ORDER]
        PR[GET_PRODUCT]
        KB[SEARCH_KNOWLEDGE_BASE]
        EL[CHECK_REFUND_ELIGIBILITY]
        DEC[DECIDE Gemini + heuristic fallback]
        G{needs_escalation or confidence < threshold?}
        RES[RESOLVE path]
        ESC[ESCALATE path]
        FIN[FINALIZE]

        P --> C1 --> O1 --> PR --> KB --> EL --> DEC --> G
        G -- No --> RES --> FIN
        G -- Yes --> ESC --> FIN
    end

    %% ===== Tooling and data =====
    subgraph D[Tools and Data Sources]
        TOOLS[SupportTools retry + validation wrapper]
        DATA[(customers/orders/products/knowledge JSON)]
        ACT1[ISSUE_REFUND]
        ACT2[SEND_REPLY]
        ACT3[ESCALATE]

        C1 --> TOOLS
        O1 --> TOOLS
        PR --> TOOLS
        KB --> TOOLS
        EL --> TOOLS
        TOOLS --> DATA

        RES --> ACT1
        RES --> ACT2
        ESC --> ACT3
        ESC --> ACT2
    end

    %% ===== Reliability =====
    subgraph E[Reliability and Safety Controls]
        R1[Retry budget with exponential backoff]
        R2[Timeout, malformed, partial response handling]
        R3[Eligibility fallback to safe escalation]
        R4[Dead-letter flag for unrecoverable write failures]
        R5[Top-level pipeline exception fallback]
    end

    TOOLS --> R1
    TOOLS --> R2
    EL --> R3
    RES --> R4
    BATCH --> R5

    %% ===== Outputs =====
    subgraph F[Artifacts and Observability]
        OUT1[resolutions.json]
        OUT2[audit_log.json]
        OUT3[escalations.json]
        OUT4[dead_letter_queue.json]
        OUT5[summary.json]
        FINALOUT[Latest outputs mirror under outputs/]
    end

    FIN --> OUT1
    FIN --> OUT2
    FIN --> OUT3
    FIN --> OUT4
    FIN --> OUT5

    OUT1 --> FINALOUT
    OUT2 --> FINALOUT
    OUT3 --> FINALOUT
    OUT4 --> FINALOUT
    OUT5 --> FINALOUT

    %% Visual emphasis
    classDef success fill:#d7f5dd,stroke:#1f7a3f,color:#0e3d1f;
    classDef escalate fill:#ffe7c2,stroke:#b06a00,color:#5a3600;
    classDef fail fill:#ffd6d6,stroke:#a40000,color:#4d0000;

    class RES,OUT1,OUT5 success;
    class ESC,OUT3 escalate;
    class OUT4,R4,R5 fail;
```

## Notes

- Read path is deterministic: parse, lookup, knowledge retrieval, eligibility check, then decision.
- Action path is policy-safe: low confidence or risk markers route to escalation.
- Reliability is centralized in the tool wrapper: retries, backoff, schema validation, and error audit events.
- Batch processing is concurrent with semaphore control in main.run.
- API lifecycle supports queued jobs, polling status, and per-job artifact retrieval.
