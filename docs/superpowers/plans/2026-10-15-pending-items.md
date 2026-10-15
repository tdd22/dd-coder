# Pending Items Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add session-scoped pending items that a lite model may propose and only the harness may commit.

**Architecture:** `agent/pending.py` owns validation, transitions, rendering, and completion checks. `pending_extract` runs once per new user message, `agent` and `tools` keep the current loop, and `pending_verify` only stores completion proposals. Transcript evidence is appended before a `done` state can be committed.

**Tech Stack:** Python 3.13, LangGraph, unittest, OpenAI-compatible lite model.

## Global Constraints

- Pending state stays in the LangGraph checkpoint and is isolated by `thread_id`.
- Models cannot write `pending_items` directly.
- Extract may propose `create`, `block`, `open`, or `no_change`; verify may propose `done`.
- `done` requires real tool-result transcript events from the current request.
- Rejected or cancelled tool output cannot be evidence.
- `blocked -> done` and `done -> *` are rejected.
- Extraction, verification, and title generation do not increment `turns`.
- `pending_items` and `processed_pending_source_ids` persist across invokes.
- New invokes reset `pending_completion_proposals`; approval resume does not.
- No context compaction, retrieval evidence, pending UI, cancellation, or archive in this version.

---

### Task 1: Pending contract

**Files:**
- Create: `agent/pending.py`
- Test: `test_pending.py`

- [ ] Add failing tests for create, block, open, done, rejected transitions, render limits, and evidence checks.
- [ ] Implement pure pending functions and rerun `python -m unittest test_pending.py`.

### Task 2: Graph and transcript integration

**Files:**
- Modify: `agent/llm.py`
- Modify: `agent/routing.py`
- Modify: `agent/transcript.py`
- Modify: `agent/graph.py`
- Modify: `docs/DEVELOPMENT.md`
- Test: `test_pending.py`
- Test: `test_stop.py`

- [ ] Return transcript envelopes and record tool-result IDs.
- [ ] Add lite routes, JSON completion, both graph nodes, prompt injection, and post-run completion commit.
- [ ] Prove one extraction per user message, no extraction on resume, no model call without open items, and no `turns` or model-tier leakage.
- [ ] Run `python -m unittest test_pending.py test_stop.py test_dispatch.py test_user_memory.py`.
