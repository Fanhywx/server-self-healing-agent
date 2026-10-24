# Server-Side Fault Self-Healing Agent

A multi-model collaborative agent system that automatically detects, diagnoses, and repairs server-side application faults through a ticket-based human-in-the-loop workflow.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     User / Dashboard                         │
│              (Streamlit Web Console)                         │
└──────────────────┬──────────────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────────────┐
│                    Ticket Agent (Orchestrator)               │
│  ┌─────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐   │
│  │ Filter  │ │ Generate │ │ Patch    │ │ Sandbox      │   │
│  │ (Qwen3) │ │ (DeepSeek)│ │ (Kimi K2)│ │ Validation   │   │
│  └────┬────┘ └────┬─────┘ └────┬─────┘ └──────┬───────┘   │
│       │           │            │               │             │
│       ▼           ▼            ▼               ▼             │
│  [Local]     [Cloud API]  [Cloud API]    [Local:8081]        │
│  QWEN3-7B    DeepSeek V4.1  Kimi K2.7      Flask Target App   │
│  GPU         Flash         Code           + Visual Check     │
└─────────────────────────────────────────────────────────────┘
```

## Core Components

| Component | File | Description |
|-----------|------|-------------|
| **Target Application** | `target_app.py` | Flask web app with intentional faults for testing |
| **Dashboard** | `dashboard.py` | Streamlit console for monitoring, ticket management, and manual review |
| **Self-Heal Engine** | `self_heal.py` | Orchestrates the full repair pipeline |
| **Diagnose Module** | `diagnose.py` | Error analysis and root cause identification |
| **Ticket Agent** | `ticket_agent.py` | Multi-model orchestration engine |
| **Offline Fallback** | `offline_fallback.py` | Local-only repair when cloud APIs are unavailable |
| **Common Utils** | `common_utils.py` | Shared utilities (logging, config loading, etc.) |
| **Visual Check** | `visual_check.py` | Screenshot-based UI verification after patching |

## Technology Stack

- **Backend**: Python 3.10+, Flask, Streamlit
- **AI Models**:
  - DeepSeek V4.1 Flash — Fault diagnosis & solution generation
  - Kimi K2.7 Code — Code patch generation
  - DeepSeek V4 Flash Vision — Visual verification via screenshots
  - QWEN3-7B (local) — Initial request filtering & offline fallback
- **API Provider**: ModelVerse (`https://api.modelverse.cn/v1`)
- **Deployment**: Ubuntu 22.04, Nginx reverse proxy

## Quick Start

### Prerequisites

```bash
pip install flask streamlit requests openai pillow pyyaml
```

### Configuration

Edit `config.json`:

```json
{
  "modelverse_api_key": "YOUR_API_KEY_HERE",
  "target_app_port": 8080,
  "dashboard_port": 8501,
  "sandbox_port": 8081,
  "preview_port": 8095
}
```

> ⚠️ Replace `YOUR_API_KEY_HERE` with your actual ModelVerse API key before deployment.

### Run the System

```bash
# Terminal 1: Start target application
python target_app.py

# Terminal 2: Start dashboard
streamlit run dashboard.py
```

Access the dashboard at `http://localhost:8501`.

## How It Works

The self-healing process follows a structured ticket-based workflow:

1. **Request Filtering** — Local QWEN3-7B model filters incoming fault reports to determine if they require automated handling.

2. **Fault Diagnosis** — DeepSeek V4.1 Flash analyzes error logs and stack traces to identify the root cause.

3. **Solution Generation** — DeepSeek generates a proposed fix strategy based on the diagnosis.

4. **Patch Generation** — Kimi K2.7 Code produces the actual code patch to fix the identified issue.

5. **Sandbox Validation** — The patched code is deployed to an isolated sandbox environment (port 8081) for functional testing.

6. **Visual Verification** — deepseek-vision-exp captures screenshots of the patched application and verifies UI correctness.

7. **Human Review Gate** — All patches enter an `await_review` state requiring manual approval before merging into production.

8. **Preview & Merge** — Approved patches are previewed on port 8095, then merged by the user via the dashboard.

## Features

- **Multi-model Collaboration**: Leverages specialized models for different stages of the repair pipeline
- **Ticket-Based Workflow**: Structured process with clear state transitions and audit trail
- **Sandbox Isolation**: Patches are validated in isolated environments before affecting production
- **Visual Verification**: AI-powered screenshot comparison ensures UI changes are correct
- **Human-in-the-Loop**: Final merge requires explicit user approval
- **Offline Fallback**: Local QWEN3-7B can handle simple fixes without cloud dependencies
- **Real-time Monitoring**: Streamlit dashboard shows live status of all tickets and processes

## File Reference

| File | Purpose |
|------|---------|
| `config.json` | Configuration file (API keys, ports, model settings) |
| `main.py` | Entry point that starts all services |
| `target_app.py` | Flask target application with intentional faults |
| `dashboard.py` | Streamlit management console |
| `self_heal.py` | Main self-healing orchestration logic |
| `diagnose.py` | Fault diagnosis module |
| `ticket_agent.py` | Multi-model ticket processing agent |
| `offline_fallback.py` | Local-only fallback repair mechanism |
| `visual_check.py` | Screenshot-based visual verification |
| `common_utils.py` | Shared utility functions |

## Design Notes

- The system is designed as a proof-of-concept for server-side fault self-healing using LLM agents
- All cloud API calls go through ModelVerse's unified endpoint
- Local models run on GPU when available; CPU fallback is supported
- The sandbox environment uses a separate Flask instance to isolate test patches from the main application
- Visual checks use screenshot capture and AI vision models rather than DOM inspection
