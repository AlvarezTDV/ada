# Feature: ADA Assistant Core

## Objective
Establish the foundational core for A.D.A (Ada), an assistant with a distinct human-like personality, concise responses, and conversational naturalness.

## Why
The user wants a personalized assistant that sounds human, responds concisely, and can eventually control their computer and perform automations. Starting with the core conversational CLI is the solid foundation.

## Scope
- Set up project files in `C:\Users\pc\ada`.
- Implement a configurable personality System Prompt.
- Create a conversational loop in Python using OpenAI API or a compatible endpoint (e.g. Ollama for local if preferred, but we will start with a highly customizable API setup).
- Maintain short-term conversational context.

## Constraints
- Local computer setup.
- Global Python environment (no virtual environment for now per user request, but we will document how to run it).
- Run in CLI terminal.

## Checklist
- [x] TSK-01: Create project layout and `requirements.txt` <!-- id: 0 -->
- [x] TSK-02: Design the System Prompt for ADA's personality and voice <!-- id: 1 -->
- [x] TSK-03: Implement `ada.py` conversational core and CLI loop <!-- id: 2 -->
- [x] TSK-04: Test conversational behavior and response conciseness <!-- id: 3 -->
- [x] TSK-05: Implement `voice.py` (TTS Text-to-Speech and STT Speech-to-Text) <!-- id: 4 -->
- [x] TSK-06: Integrate voice pipeline into `ada.py` CLI interface <!-- id: 5 -->

## Progress and Verification
- Current state: Text interaction, SQLite memory, and Voice I/O (STT/TTS) fully implemented and integrated.
