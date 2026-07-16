# Credits

NeXgen Engine builds on the work of others. This file lists the
third-party components and their authors. Each component keeps its own upstream
license; this file gives credit and points to the source.

## Self-hosted stack (deploy/)

| Component | Upstream | License | Purpose |
|---|---|---|---|
| **n8n** | https://github.com/n8n-io/n8n | Sustainable Use License | Workflow automation engine |
| **Firecrawl** | https://github.com/mendableai/firecrawl | AGPL-3.0 | Self-hosted web scraping/search |
| **RapidOCR** | https://github.com/RapidAI/RapidOCR | Apache-2.0 | OCR engine (used in Vault OCR) |
| **Playwright MCP** | https://github.com/microsoft/playwright-mcp | Apache-2.0 | Browser automation over CDP |
| **FastAPI** | https://github.com/tiangolo/fastapi | MIT | Vault OCR API framework |
| **uvicorn** | https://github.com/encode/uvicorn | BSD-3-Clause | ASGI server |
| **Redis** | https://github.com/redis/redis | RSALv2/SSPL | Firecrawl queue |

## MCP servers (mcp/manifest.yaml)

| Server | Upstream | License |
|---|---|---|
| **firecrawl-mcp** | https://github.com/mendableai/firecrawl-mcp | MIT |
| **@playwright/mcp** | https://github.com/microsoft/playwright-mcp | Apache-2.0 |
| **@modelcontextprotocol/server-filesystem** | https://github.com/modelcontextprotocol/servers | MIT |
| **@modelcontextprotocol/server-memory** | https://github.com/modelcontextprotocol/servers | MIT |

## Skills (skills/skills.manifest.yaml)

| Skill | Author | Upstream | License |
|---|---|---|---|
| **humanizer** | blader | https://github.com/blader/humanizer | MIT |
| **frontend-design** | Anthropic (Claude Code official skill) | Claude Code plugin | See upstream |
| **knowledge-vault-hygiene** | Matteo Passeri (this repo) | — | This repo's LICENSE |

## Python libraries (scripts/ and ocr/)

| Library | License |
|---|---|
| PyYAML | MIT |
| google-api-python-client / google-auth | Apache-2.0 |
| Pillow | HPND |
| onnxruntime | MIT |

## Thanks

This project stands on the shoulders of the open-source and source-available
community above. If you extend NeXgen Engine with a new component, add it to this
file so credit stays accurate.
