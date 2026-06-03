# Jawafdehi Caseworker Agent

This directory contains the core business logic, workflow hooks, and setup configuration for the Caseworker Agent. The Caseworker automates taking raw corruption case data — from the CIAA and Special Courts via NGM — and drafting/publishing complete cases into the Jawafdehi public transparency portal.

## Flexible Provider Support

The agent logic here is designed to be **provider agnostic**. While there are configurations supporting certain platforms specifically, our goal is to allow this workflow to run anywhere.

### Supported Providers

1. **Kiro CLI (First-Class)**
   - The primary execution loop for the caseworker relies on invoking `kiro-cli` in a stateless, user-story driven architecture.
   - The configuration `jawafdehi-caseworker.json` supports standard execution via `kiro-cli`.
   - You can run the automation loop via `python .agents/caseworker/run_workflow.py <CIAA-case-number>`.

2. **GitHub Copilot**
   - You can invoke the caseworker instructions directly in your IDE chat interface.
   - Ensure the Jawafdehi Copilot MCP settings are configured.
   - You can prompt the IDE: "Please follow the workflow defined in `.agents/caseworker/instructions/INSTRUCTIONS.md`".

## Getting Started Locally

Before running the agent using any of these providers, please refer to [docs/LOCAL_AGENT_SETUP.md](../../docs/LOCAL_AGENT_SETUP.md) for complete setup instructions on configuring the Jawafdehi API, making an authentication token, and tying it to your `mcp.json` setup!
