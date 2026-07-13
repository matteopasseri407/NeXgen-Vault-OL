# NeXgen Engine (Alpha)

[![CI](https://github.com/matteopasseri407/NeXgen-Engine/actions/workflows/ci.yml/badge.svg)](https://github.com/matteopasseri407/NeXgen-Engine/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/matteopasseri407/NeXgen-Engine)](https://github.com/matteopasseri407/NeXgen-Engine/releases/latest)
[![License: PolyForm Noncommercial 1.0.0](https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-blue)](LICENSE)

A Git-backed AgentOps control layer for AI coding CLIs — in plain terms, a shared rulebook and memory for AI agent tools like Claude Code, useful for non-coding work (notes, research, career docs) just as much as for software projects. Note: This project is currently in Alpha.

Shared instructions, generated MCP config, drift checks, secrets discipline, and cross-machine agent memory, all as plain files in a Git repo, not a hosted service.

You use Claude Code, Codex, OpenCode, or Antigravity, maybe more than one, maybe on two machines.
Each CLI reads its bootstrap instructions from a different file, keeps its own MCP config, and has no idea what the others are doing.
Change one and the rest drift out of sync, usually without anyone noticing until something breaks.
NeXgen Engine gives them one canonical source and a way to check whether they've drifted from it.

## Who this is for

You run at least one agentic CLI on your own machine and want the actual vault, not a demo of one.
If you run several CLIs, or the same setup across more than one machine, that's where the framework does most of its work: the provisioner and doctor scripts described below exist for that case.
If it's just one CLI on one machine, you still get the knowledge vault and the bootstrap discipline, without needing to run any of the sync tooling.

Evaluating this for more than one person (a couple of colleagues, a small company)? The security and identity model is mono-user today. Read [`docs/team.md`](docs/team.md) and, if you're weighing a shared Cloud-Server backend, [`docs/org-deployment.md`](docs/org-deployment.md) before you adopt it as shared infrastructure. Security posture and how to report an issue are in [`SECURITY.md`](SECURITY.md).

## Demo path

1. Clone the repo and run the preflight: `bash install.sh --check` on Linux/Mac, or `.\install.ps1 -Check` from PowerShell on Windows. It checks prerequisites, verifies the vault scaffold, and lists which agentic CLIs it finds on your machine. It writes nothing.
2. Open `INIT.md` and paste it into a filesystem-capable agent CLI (Claude Code, Codex, OpenCode, Antigravity), not a web chat, which can't write files. The agent interviews you (how many CLIs, how many machines, Local-Only or Cloud-Server) and writes `99-INDEX/USER-PROFILE.md`.
3. The agent mounts the MCP servers and skills for your chosen CLI(s), following the manifests in `03-INFRA/`.
4. If you're on the MULTI profile (2+ CLIs or machines), run `agent-sync apply` to propagate the canonical config, then `agent-doctor` to see the actual compliance check: 30+ live checks against your running CLIs, VPS services, and secrets handling, with a pass, warn, or fail on each line. On Windows the first `apply` also adds the commands' directory to your user PATH — open a new terminal afterwards so `agent-sync`, `agent-doctor`, `vault-groom` and `vault-push` resolve as bare commands.
5. Change something by hand afterward (a stray MCP entry, a config file edited outside the vault) and run `agent-doctor` again. That's the drift check working.

## What this does not do

No UI, no hosted dashboard, no proprietary memory store.
It doesn't compete with a RAG builder or a workflow orchestrator.
It assumes you already have opinions about which agents and tools you want, and gives them a shared, auditable floor to run on.

NeXgen's public-engine safety gates are maintainer tooling, not an end-user chore. Normal users push only their private vault data. Checks such as `engine-push`, public-repo leak gates, and disabled direct push on an engine development clone matter only for people publishing changes to this GitHub repository.

**What NeXgen does not do:** NeXgen governs configuration — one canonical source, generated derivatives, drift detection, single-door writes. It does **not** sit between an agent and its tools at runtime: `agent-doctor` cannot block a call made with hallucinated but valid-looking arguments. That boundary is enforced by your CLI harness (permission modes, user approval prompts) and by server-side validation in the MCP servers themselves (e.g., the `expected_hash` lock in `vault-library`).

## Core concepts

- **Infrastructure as Code for AI.** Manifest files define tools, permissions, and agent behaviors. A unified Python script (`agent_sync.py`) generates the correct configuration for different CLIs.
- **Git-backed memory.** The agents read and write Markdown files. Every change is version-controlled, diffable, and easy to revert.
- **Vault grooming (optional, on-demand).** `vault-groom.sh`/`.ps1` runs an LLM over a grooming playbook to flag stale, duplicate, or dead notes. A bare run (or `preview`) is always read-only. `vault-groom apply` is the guarded lane: it proposes a tranche, shows it in full, and only after you type `yes` does the write pass run — inside a disposable clone of the vault with no remote configured, so it physically cannot push. A mechanical audit then compares what was actually committed against the approved tranche, in both directions, and only a fully clean run gets promoted (fast-forwarded) into your real vault; anything else stays quarantined in the clone, with your vault untouched. Works with whichever of `claude`, `codex`, or `agy` you already have (`GROOM_RUNNER`). An optional n8n workflow only reminds you it's due every 14 days — the grooming pass itself is never scheduled or run unattended.
- **Deterministic AI Council (Alpha).** A local orchestrator (`council.py`) that coordinates multiple models for brainstorming and relay tasks. It uses explicit Python code to pass control, rather than relying on an LLM to manage the rules.
- **Drift detection.** In MULTI profile, the `agent-doctor` script runs 30+ read-only checks against your CLIs' live configuration, vault wiring, skills, and secrets handling, reporting pass/warn/fail per line (non-zero exit code on failures). It detects drift and misconfiguration; it does not sit in the execution path. In MINIMAL, there is no doctor: a single CLI on a single machine is verified visually.
- **Cross-platform consistency (optional).** In MULTI profile, the system forces agents to behave identically across different machines (e.g., a Windows workstation and a Linux laptop) through a provisioner. In MINIMAL, there is only one machine, so the provisioner is a no-op and is not installed.

## Architecture: The Three Planes

NeXgen Engine separates operations into three distinct planes:

1. **Behavior:** A single operating policy (`AGENTS.md`) linked into every runtime.
2. **Configuration:** An abstract MCP manifest compiled into each CLI's specific dialect by a generator script.
3. **Memory:** A plain-Markdown vault, written through serialized paths. 

Writes go through one door per kind of thing. Knowledge notes are written only through a memory tool server that serializes with a lock and an expected-hash check, preventing agents from overwriting each other's work.

**Skills stay lazy by design.** Tool awareness and policy remain in the
bootstrap and MCP manifest. Optional task playbooks live outside eager
discovery roots and are opened only when needed. See
[`docs/lazy-skills.md`](docs/lazy-skills.md).

## Shared Tools via MCP (Modular & Free-Tier Ready)

Agents share infrastructure rather than reinventing it. A few services run once, in an environment you deploy and own (not a service this project or its author operates for you), and every agent reaches them over the Model Context Protocol (MCP):

> **Note:** These specific tools are completely interchangeable. They were selected because they run comfortably and at zero cost on an **Oracle Cloud Always Free VPS** (4 ARM Ampere cores, 24GB RAM, 200GB SSD) — a tier anyone can provision for themselves. You can easily swap them for enterprise equivalents.

- **Semantic Search (bring-your-own):** the `vault-library` MCP contract (`semantic_search`, see `manifest.yaml`) is ready to call, and the retrieval governance in `AGENTS.md` routes to it. Unlike the three tools below, **no deploy code for the search backend itself ships in this repo** — `03-INFRA/deploy/` has no `semantic-search/` folder. Build and host your own service behind that contract (a self-hosted retrieval layer over static embeddings + BM25 is a proven shape for it) if you want this lane to actually answer; without one, agents fall back to lexical search per the governance doc.
- **Web Scraping:** A self-hosted Firecrawl instance you deploy (included in `03-INFRA/deploy/firecrawl/`) serves as the default read-only lane.
- **Local OCR:** A self-hosted OCR service you deploy (included in `03-INFRA/deploy/ocr/`) extracts text from screenshots, logs, and scanned documents locally.
- **Visible Browser:** For interactive tasks (forms, logins, page checks), agents attach to a real, visible Chrome window via the DevTools protocol. **Agents are strictly forbidden from running headless browsers behind the user's back.**

## What We Deliberately Didn't Build

We didn't write a proprietary memory engine. Markdown, Git, and a simple tool server already provide durable, auditable memory that humans and agents can both read. 
There are no complex "agent-to-agent negotiations", no autonomous Swarm A* planners, no CRDTs, and no secondary databases. The effort went entirely into the layer *above* storage: the operational governance and safety rails.

## What's inside

| Directory | Purpose |
|---|---|
| `03-INFRA/` | The engine. Contains the agent bootstrap rules (`AGENTS.md`), MCP server definitions, and validation scripts (`agent-sync`, `agent-doctor`). |
| `99-INDEX/` | The identity layer. Tells agents about the current hardware, operating system, and deployment context (`USER-PROFILE.md`). |
| `01-NOTES/` | Standard workspace for documentation. |
| `02-PROJECTS/` | Project tracking and execution logs. |
| `04-NOW/` | Active priorities. This restricts agents from wandering into irrelevant tasks. |

## Deployment modes

1. **Local-Only.** Runs entirely on your machine. Relies on native CLI tools and local models. Good for testing and single-user setups.
2. **Cloud-Server.** Connects to a self-hosted stack (like n8n for orchestration, Firecrawl for scraping, and dedicated OCR) deployed in **your own private environment** (VPS or local server) over an SSH tunnel. You maintain full ownership of your data; NeXgen does not provide or host these services for you.

The AI-guided setup (`INIT.md`) configures the correct mode for your environment.

## Installation profiles

The framework fits two shapes of usage. The installer (`INIT.md`) asks and picks the right one.

- **MINIMAL.** One CLI on one machine (e.g., only Claude Code on your laptop, or [OpenCode](https://opencode.ai) for a DeepSeek-based single-CLI setup). You get the knowledge vault, the bootstrap rules, lazy skills, and the discipline of writing memory through one door. There is no provisioner to run, no doctor to schedule, no cross-machine sync. Mount the MCP servers and skills you want directly in your CLI by hand. Best for solo users who just want AgentOps governance on top of a single agent.
- **MULTI.** Two or more CLIs and/or two or more machines. The unified Python provisioner (`agent_sync.py`), the doctor, and the healthcheck come online and keep every CLI and machine aligned to the canonical source in the vault. Best for a workstation + laptop setup, or for running multiple CLIs side by side.

MULTI propagation is a locked, fail-closed transaction. The pull must prove the
data fresh against one authoritative remote before runtime files are regenerated;
publishing is always a separate command. See
[`docs/sync-contract.md`](docs/sync-contract.md).

You can start MINIMAL and switch to MULTI later. The canonical files in the vault do not change between profiles.

## Installation

You don't need to fill out configuration files manually.

1. Clone the repository:
   ```bash
   git clone https://github.com/matteopasseri407/NeXgen-Engine.git ~/KnowledgeVault
   cd ~/KnowledgeVault
   ```
   > Optional preflight: `bash install.sh` checks prerequisites, verifies the scaffold, detects your CLIs, and prints the next step. It writes nothing and is safe to re-run.
2. Open `INIT.md`.
3. Paste its contents into a **filesystem-capable agent CLI** (Claude Code, Codex, OpenCode, Antigravity) opened in this folder, not a plain web chat (claude.ai / gemini), which cannot write files.
4. The agent will ask how many CLIs and machines you have, your hardware, and your deployment mode, then configure the vault automatically.

Prefer fewer questions and more autonomy? `AI-INSTALLER.md` is the same install with minimal back-and-forth: paste it instead of `INIT.md` and the agent runs the steps itself rather than interviewing you one question at a time.

## Prerequisites

- Git
- Python 3.11+ with PyYAML (`pip install pyyaml`), or Python 3.10 with `tomli` too (`pip install pyyaml tomli`)
- Node.js (for `npx`, needed if you mount MCP servers or external skills)
- Optional: [OpenCode](https://opencode.ai) as one of the supported CLIs
- `jq` and `curl` on Linux/Mac (only needed for the MULTI profile sync and health scripts)

## Platform status

**Why is this Alpha?**
Linux is the daily-driven platform and the most tested, but the framework is still in Alpha because cross-platform support and core orchestrators are actively settling. Specifically:
- **Windows Support:** The core provisioner (`agent_sync.py`) and the MCP config generator (`render.py`, via a per-server `windows:` override block in the manifest) both have a Windows dialect, and CI runs the full pytest suite on `windows-latest` (job `engine-tests-windows`) on every push. That proves the shared code paths, not a physical machine: a couple of runtime paths (e.g. the Antigravity instructions file) are still inferred by analogy with Linux rather than confirmed live, and the vendor adapters and Windows launcher below still need that physical verification.
- **AI Council:** The deterministic orchestrator (`council.py`) supports `opencode`, `agy`, `codex`, `claude`, and `ollama` seats. Its optional routing adapter proposes exact locally verified models and efforts, with declared fallbacks, without letting an external workflow rewrite private cross-machine data or auto-invoke a seat. A human explicitly chooses the seat count and models. Vendor adapters and the Windows launcher still need physical cross-platform verification.

MINIMAL profile is the safer starting point on Windows today. macOS follows the Linux code paths but has seen less real-world use.

## License

PolyForm Noncommercial License 1.0.0. Free for any noncommercial use, including reading, running, forking, and modifying it. See `LICENSE` for the full text. Any commercial use, of the original software or a derivative, needs a separate license from the author: see `COMMERCIAL.md`.

## Support

This project is free to use. Some optional links (like the OpenCode one above) are referral links that fund maintenance at no extra cost to you: see `SUPPORT.md` for the one place they're declared.

---

# NeXgen Engine (Italiano) - Alpha

Un control layer AgentOps basato su Git, per le CLI agentiche di sviluppo — in parole povere, un regolamento condiviso e una memoria per tool AI agentici come Claude Code, utile tanto per lavori non di programmazione (note, ricerca, documenti di carriera) quanto per progetti software. Nota: Questo progetto è attualmente in fase Alpha.

Istruzioni condivise, configurazione MCP generata automaticamente, controlli anti-drift, disciplina sui segreti e memoria degli agenti condivisa tra più macchine.
Tutto file di testo dentro un repo Git, non un servizio in cloud.

Usi Claude Code, Codex, OpenCode o Antigravity, magari più di una CLI, magari su due macchine diverse.
Ogni CLI legge le sue istruzioni di bootstrap da un file diverso, ha una propria configurazione MCP e non sa niente delle altre.
Basta cambiare qualcosa in una perché le altre si disallineino, quasi sempre senza che nessuno se ne accorga finché non si rompe qualcosa.
NeXgen Engine mette tutte le CLI davanti a un'unica fonte canonica e ti dà un modo per controllare se se ne sono allontanate.

## A chi serve

Fai girare almeno una CLI agentica sulla tua macchina e vuoi il vault vero, non una demo.
Se ne usi più di una, o lo stesso setup su più macchine, è lì che il framework rende di più: il provisioner e lo script doctor descritti sotto servono esattamente a quello.
Se invece hai una sola CLI su una sola macchina, ti restano comunque il knowledge vault e la disciplina del bootstrap, senza dover far girare nessuno strumento di sync.

Lo stai valutando per più di una persona (qualche collega, una piccola azienda)? Il modello di sicurezza e identità oggi è mono-utente. Leggi [`docs/team.md`](docs/team.md) e, se stai valutando un backend Cloud-Server condiviso, [`docs/org-deployment.md`](docs/org-deployment.md) prima di adottarlo come infrastruttura condivisa. La postura di sicurezza e come segnalare un problema sono in [`SECURITY.md`](SECURITY.md).

## Percorso demo

1. Clona il repo e lancia il preflight: `bash install.sh --check` su Linux/Mac, oppure `.\install.ps1 -Check` da PowerShell su Windows. Controlla i prerequisiti, verifica lo scaffold del vault ed elenca quali CLI agentiche trova sulla tua macchina. Non scrive nulla.
2. Apri `INIT.md` e incollalo in una CLI agentica capace di scrivere file (Claude Code, Codex, OpenCode, Antigravity): una chat web non va bene, perché i file non li può scrivere. L'agente ti fa qualche domanda (quante CLI, quante macchine, Local-Only o Cloud-Server) e scrive `99-INDEX/USER-PROFILE.md`.
3. L'agente monta i server MCP e le skill per la CLI che hai scelto, seguendo i manifest in `03-INFRA/`.
4. Se sei sul profilo MULTI (2+ CLI o macchine), lancia `agent-sync apply` per propagare la configurazione canonica, poi `agent-doctor` per vedere il controllo di conformità vero e proprio: oltre 30 check dal vivo sulle CLI in esecuzione, sui servizi VPS e sulla gestione dei segreti, con un pass, warn o fail riga per riga. Su Windows il primo `apply` aggiunge anche la cartella dei comandi al PATH utente: apri un nuovo terminale, così `agent-sync`, `agent-doctor`, `vault-groom` e `vault-push` si risolvono come comandi semplici.
5. A quel punto cambia qualcosa a mano (una entry MCP fuori posto, un file di config modificato fuori dal vault) e rilancia `agent-doctor`. Quello è il controllo di drift che funziona.

## Cosa non fa

Nessuna UI, nessuna dashboard in cloud, nessun motore di memoria proprietario.
Non è in competizione con un RAG builder o un workflow orchestrator.
Parte dal presupposto che tu abbia già le idee chiare su quali agenti e strumenti usare, e gli dà un terreno comune e verificabile su cui girare.

**Cosa NON fa NeXgen:** NeXgen governa la configurazione — una fonte canonica, derivati generati, rilevamento del drift, scritture da una sola porta. **Non** si mette tra l'agente e i suoi tool a runtime: `agent-doctor` non può bloccare una chiamata fatta con argomenti inventati ma plausibili. Quel confine è gestito dall'harness della tua CLI (modalità permessi, richieste di conferma all'utente) e dalla validazione lato server dei server MCP stessi (es. il lock `expected_hash` nel `vault-library`).

## Concetti base

- **Infrastruttura come codice per l'AI.** I file manifest definiscono tool, permessi e regole di comportamento. Uno script Python unificato (`agent_sync.py`) genera la configurazione corretta per le diverse CLI.
- **Memoria basata su Git.** Gli agenti leggono e scrivono file Markdown. Ogni modifica è tracciata, verificabile e facile da annullare.
- **Giardinaggio del vault (opzionale, a richiesta).** `vault-groom.sh`/`.ps1` fa passare un playbook di grooming a un'LLM per segnalare note vecchie, duplicate o morte. L'invocazione semplice (o `preview`) è sempre in sola lettura. `vault-groom apply` è la corsia protetta: propone una tranche, te la mostra per intero e solo dopo il tuo `yes` parte la passata di scrittura, dentro un clone usa-e-getta del vault senza remote configurati, che quindi non può fisicamente fare push. Un audit meccanico confronta poi i commit reali con la tranche approvata, in entrambe le direzioni, e solo una run del tutto pulita viene promossa (fast-forward) nel tuo vault vero; tutto il resto resta in quarantena nel clone, col vault intatto. Funziona con quella che hai già tra `claude`, `codex` o `agy` (`GROOM_RUNNER`). Un workflow n8n opzionale si limita a ricordarti che è ora ogni 14 giorni — la passata di grooming vera e propria non è mai schedulata né gira incustodita.
- **Consiglio AI deterministico (Alpha).** Un orchestratore locale (`council.py`) che coordina più modelli per compiti di brainstorming o a staffetta. Usa codice Python esplicito per cedere il controllo, anziché affidare le regole di gestione a un LLM.
- **Rilevamento del drift.** Nel profilo MULTI lo script `agent-doctor` esegue oltre 30 controlli in sola lettura su configurazione viva delle CLI, collegamento al vault, skill e gestione dei segreti, riportando pass, warn o fail riga per riga (exit code non-zero in caso di fallimenti). Rileva drift e configurazioni sbagliate; non sta nel percorso di esecuzione. In MINIMAL non c'è doctor: una CLI su una macchina si verifica a vista.
- **Coerenza tra macchine (opzionale).** Nel profilo MULTI il sistema forza gli agenti a comportarsi in modo identico su hardware diverso (ad esempio, una workstation Windows e un portatile Linux) tramite un provisioner. In MINIMAL c'è una sola macchina, quindi il provisioner è no-op e non viene installato.

## Architettura: I Tre Piani

NeXgen Engine separa le operazioni in tre piani distinti:

1. **Comportamento:** Una singola policy operativa (`AGENTS.md`) collegata a ogni runtime.
2. **Configurazione:** Un manifest MCP astratto, compilato nei dialetti specifici di ogni CLI da uno script generatore.
3. **Memoria:** Un vault in puro Markdown, scritto tramite percorsi serializzati.

Le scritture passano attraverso una sola porta per tipologia. Le note vengono scritte esclusivamente tramite un tool server che serializza le richieste con un lock e un controllo sull'hash atteso, impedendo agli agenti di sovrascrivere il lavoro altrui.

**Le skill restano lazy per scelta.** La conoscenza dei tool e la policy
restano nel bootstrap e nel manifest MCP. I playbook opzionali vivono fuori
dalle root di discovery eager e si aprono solo quando servono. Vedi
[`docs/lazy-skills.md`](docs/lazy-skills.md).

## Tool Condivisi tramite MCP (Modulari e ottimizzati per Free-Tier)

Gli agenti condividono l'infrastruttura invece di reinventarla. Alcuni servizi girano in singola istanza, in un ambiente che installi e possiedi tu (non un servizio gestito centralmente da questo progetto o dal suo autore), e tutti gli agenti vi accedono tramite Model Context Protocol (MCP):

> **Nota importante:** Questi tool specifici sono completamente intercambiabili. Sono stati scelti perché girano comodamente e a costo zero su una **VPS Oracle Cloud Always Free** (4 core ARM Ampere, 24GB di RAM, 200GB di SSD) — un tier che chiunque può attivare per sé. Possono essere sostituiti con alternative Enterprise in base alle necessità.

- **Ricerca Semantica (a carico tuo):** il contratto MCP `vault-library` (`semantic_search`, vedi `manifest.yaml`) è pronto da chiamare, e la governance di retrieval in `AGENTS.md` vi instrada. A differenza dei tre tool sotto, **nessun codice di deploy per il backend di ricerca è incluso in questo repo** — `03-INFRA/deploy/` non ha una cartella `semantic-search/`. Costruisci e ospita tu un servizio dietro quel contratto (un livello di retrieval self-hosted su embedding statici + BM25 è una forma collaudata) se vuoi che questa corsia risponda davvero; senza, gli agenti ripiegano sulla ricerca lessicale secondo la governance.
- **Web Scraping:** Un'istanza Firecrawl self-hosted che installi tu (inclusa in `03-INFRA/deploy/firecrawl/`), che funge da corsia read-only predefinita.
- **OCR Locale:** Un servizio OCR self-hosted che installi tu (incluso in `03-INFRA/deploy/ocr/`), che estrae testo da screenshot, log e documenti scansionati localmente.
- **Browser Visibile:** Per i task interattivi (form, login, controlli su pagine), gli agenti si collegano a una finestra Chrome reale e visibile tramite protocollo DevTools. **Agli agenti è severamente vietato eseguire browser headless all'insaputa dell'utente.**

## Cosa NON abbiamo costruito (di proposito)

Non abbiamo scritto un motore di memoria proprietario. Markdown, Git e un semplice tool server offrono già una memoria durevole e auditabile che umani e agenti possono leggere. 
Non ci sono complesse "negoziazioni tra agenti", né pianificatori Swarm A* autonomi, né CRDT, né database secondari. Lo sforzo è andato interamente sul livello *sopra* lo storage: la governance operativa e i binari di sicurezza.

## Contenuto

| Directory | Scopo |
|---|---|
| `03-INFRA/` | Il motore. Contiene le regole base (`AGENTS.md`), le definizioni dei server MCP e gli script di validazione (`agent-sync`, `agent-doctor`). |
| `99-INDEX/` | Il livello di identità. Informa gli agenti sull'hardware, il sistema operativo e il contesto attuale (`USER-PROFILE.md`). |
| `01-NOTES/` | Spazio di lavoro standard per la documentazione. |
| `02-PROJECTS/` | Tracciamento dei progetti e log operativi. |
| `04-NOW/` | Priorità attive. Evita che gli agenti si disperdano su task non rilevanti. |

## Modalità di deployment

1. **Locale.** Gira interamente sulla tua macchina. Usa i tool nativi delle CLI e modelli locali. Adatto per test e setup mono-utente.
2. **Cloud-Server.** Si collega a uno stack remoto (come n8n per l'orchestrazione, Firecrawl per lo scraping e OCR dedicato) installato e gestito nel **tuo ambiente privato** (VPS o server locale) tramite tunnel SSH. Mantieni il pieno controllo dei tuoi dati; NeXgen non fornisce né ospita questi servizi per te.

Il setup guidato dall'AI (`INIT.md`) configurerà la modalità adatta al tuo ambiente.

## Profili di installazione

Il framework si adatta a due forme d'uso. L'installer (`INIT.md`) chiede e sceglie quella giusta.

- **MINIMAL.** Una CLI su una macchina (es. solo Claude Code sul portatile, oppure [OpenCode](https://opencode.ai) per un setup single-CLI basato su DeepSeek). Ottieni il knowledge vault, le regole del bootstrap, le skill lazy e la disciplina della scrittura memoria tramite una sola porta. Non c'è provisioner da lanciare, nessun doctor da schedulare, niente sync tra macchine. Monti MCP server e skill a mano nella tua CLI. Indicato per chi lavora da solo e vuole governance AgentOps sopra un singolo agente.
- **MULTI.** Due o più CLI e/o due o più macchine. Il provisioner unificato in Python (`agent_sync.py`), il doctor e l'healthcheck entrano in funzione e tengono ogni CLI e ogni macchina allineata alla fonte canonica del vault. Indicato per un setup desktop + portatile, o per girare più CLI in parallelo.

Nel profilo MULTI la propagazione è una transazione con lock e blocco sicuro in caso di errore.
Il pull deve dimostrare che i dati sono aggiornati rispetto a un unico remote autorevole prima di rigenerare i file runtime.
La pubblicazione resta sempre un comando separato.
Il contratto completo è in [`docs/sync-contract.md`](docs/sync-contract.md).

Puoi partire da MINIMAL e passare a MULTI in seguito. I file canonici del vault non cambiano tra i profili.

## Installazione

Non devi compilare i file di configurazione a mano.

1. Clona il repository:
   ```bash
   git clone https://github.com/matteopasseri407/NeXgen-Engine.git ~/KnowledgeVault
   cd ~/KnowledgeVault
   ```
   > Preflight opzionale: `bash install.sh` controlla i prerequisiti, verifica lo scaffold, rileva le tue CLI e stampa il passo successivo. Non scrive nulla ed è sicuro da ri-lanciare.
2. Apri `INIT.md`.
3. Incolla il contenuto in una **CLI agentica capace di scrivere file** (Claude Code, Codex, OpenCode, Antigravity) aperta in questa cartella, non una chat web (claude.ai / gemini), che non può scrivere file.
4. L'agente ti chiederà quante CLI e macchine hai, il tuo hardware e la modalità di deployment, poi configurerà il vault in automatico.

Preferisci meno domande e più autonomia? `AI-INSTALLER.md` è la stessa installazione con il minimo indispensabile di domande: incollalo al posto di `INIT.md` e l'agente esegue i passi da solo invece di intervistarti uno alla volta.

## Prerequisiti

- Git
- Python 3.11+ con PyYAML (`pip install pyyaml`), oppure Python 3.10 con anche `tomli` (`pip install pyyaml tomli`)
- Node.js (per `npx`, necessario se monti server MCP o skill esterne)
- Opzionale: [OpenCode](https://opencode.ai) come una delle CLI supportate
- `jq` e `curl` su Linux/Mac (solo per il profilo MULTI, necessari per sync e health)

## Stato per piattaforma

**Perché siamo in Alpha?**
Linux è la piattaforma usata quotidianamente e la più testata, ma il framework è in Alpha perché il supporto cross-platform e gli orchestratori principali si stanno ancora stabilizzando. Nello specifico:
- **Supporto Windows:** Sia il provisioner principale (`agent_sync.py`) sia il generatore di config MCP (`render.py`, tramite un blocco `windows:` di override per-server nel manifest) hanno un dialetto Windows, e la CI esegue l'intera suite pytest su `windows-latest` (job `engine-tests-windows`) a ogni push. Questo dimostra che il codice condiviso funziona, non che è stato verificato su una macchina fisica: un paio di percorsi runtime (es. il file di istruzioni di Antigravity) restano dedotti per analogia con Linux piuttosto che confermati dal vivo, e gli adapter dei vendor e il launcher Windows citati sotto necessitano ancora di quella verifica fisica.
- **Consiglio AI:** L'orchestratore deterministico (`council.py`) supporta i seat `opencode`, `agy`, `codex`, `claude` e `ollama`.
L'adattatore opzionale di routing propone modelli ed effort verificabili localmente, con fallback dichiarati, senza far riscrivere a un workflow esterno i dati privati cross-machine o invocare un seat in automatico.
L'umano sceglie esplicitamente quanti seat chiamare e quali modelli usare.
I test automatici coprono il flusso dei quattro mode.
Gli adapter dei vendor e il launcher Windows devono ancora essere verificati fisicamente su entrambe le piattaforme.

Il profilo MINIMAL è il punto di partenza più sicuro su Windows oggi. macOS segue gli stessi percorsi di codice di Linux ma ha visto meno uso reale.

## Licenza

PolyForm Noncommercial License 1.0.0. Gratuita per qualsiasi uso non commerciale, incluso leggerla, eseguirla, forkarla e modificarla. Vedi `LICENSE` per il testo completo. Qualsiasi uso commerciale, del software originale o di un derivato, richiede una licenza separata dall'autore: vedi `COMMERCIAL.md`.

## Supporto

Questo progetto è gratuito da usare. Alcuni link opzionali (come quello di OpenCode sopra) sono link referral che finanziano la manutenzione senza costi aggiuntivi per te: vedi `SUPPORT.md` per l'unico punto in cui sono dichiarati.
