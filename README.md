# NeXgen Engine (Beta)

[![CI](https://github.com/matteopasseri407/NeXgen-Engine/actions/workflows/ci.yml/badge.svg)](https://github.com/matteopasseri407/NeXgen-Engine/actions/workflows/ci.yml)
[![Latest version](https://img.shields.io/github/v/release/matteopasseri407/NeXgen-Engine?display_name=tag&label=latest%20version)](https://github.com/matteopasseri407/NeXgen-Engine/releases/latest)
[![License: PolyForm Noncommercial 1.0.0](https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-blue)](LICENSE)

**Configure Claude Code, Codex, OpenCode, and Antigravity from one Git repo you can diff and revert.**

NeXgen Engine is a Git-based framework for managing shared instructions, tool configuration, and version-controlled working memory across AI tools such as Claude Code. It supports software projects as well as notes, research, and professional documents. The project is currently in Beta; the version badge above always points at the latest release.

Instructions, generated tool configuration, configuration checks, secrets guidance, and shared memory are stored as plain files in a Git repository rather than in a hosted service.

If you use Claude Code, Codex, OpenCode, or Antigravity, each tool has its own bootstrap file and MCP configuration.
When you use more than one tool or more than one machine, those files can diverge without an obvious warning.
NeXgen keeps a canonical source and provides checks for differences between that source and the generated files.

## Who this is for

NeXgen is for people who use one or more AI tools on their own machines and want a working, version-controlled setup.
The MULTI profile is useful when you run several tools or maintain the same setup on more than one machine.
It provides the provisioner and validation scripts described below.
For a single tool on a single machine, the MINIMAL profile provides the shared rules and version-controlled knowledge base without the synchronization layer.

If you are evaluating it for more than one person, note that the current identity and security model is designed for a single user.
Read [`docs/team.md`](docs/team.md) and, if you are considering a shared Cloud-Server backend, [`docs/org-deployment.md`](docs/org-deployment.md) before using it as shared infrastructure.
The security model and reporting instructions are in [`SECURITY.md`](SECURITY.md).

## Demo path

1. Clone the repository and run the preflight: `bash install.sh --check` on Linux/Mac, or `.\install.ps1 -Check` from PowerShell on Windows. It checks prerequisites, verifies the vault scaffold, and lists which supported AI tools it finds on your machine. It writes nothing.
2. Open `INIT.md` and paste it into a filesystem-capable CLI such as Claude Code, Codex, OpenCode, or Antigravity. A web chat cannot write files. The setup asks how many tools and machines you plan to use, whether you want Local-Only or Cloud-Server mode, and then writes `99-INDEX/USER-PROFILE.md`. If you already run CLIs configured with their own MCP servers, skills, or configs, Step 1.5 takes over that existing setup: it inventories what is there and lets you adopt it into the canonical source or start fresh.
3. The agent mounts the MCP servers and skills for your chosen CLI(s), following the manifests in `03-INFRA/`.
4. If you are using the MULTI profile, run `agent-sync apply` to propagate the canonical configuration, then run `agent-doctor` to perform more than 30 live checks against your tools, VPS services, and secrets handling. Each check reports `pass`, `warn`, or `fail`. On Windows, the first `apply` also adds the commands directory to the user PATH. Open a new terminal afterwards so `agent-sync`, `agent-doctor`, `vault-groom`, and `vault-push` resolve as commands.
5. Make a manual change, such as an extra MCP entry or an edited configuration file, and run `agent-doctor` again to see the difference reported.

## Scope and limitations

No graphical interface, hosted dashboard, or proprietary memory store is included.
The project is not a RAG builder or a workflow orchestrator.
It assumes you have already chosen the tools you want to use and provides a shared, auditable foundation for configuring them.

The public-engine safety gates are for maintainers who publish changes to this repository.
Normal users push only their private vault data.
GitHub pull requests, CI, release signing, and repository-level branch controls protect that publication path.

NeXgen manages configuration through one canonical source, generated files, configuration checks, and separate write paths for different data types.
It does not intercept tool calls at runtime.
`agent-doctor` cannot block an otherwise valid-looking but incorrect argument, so runtime permissions and server-side validation remain responsible for that boundary.

## Core concepts

- **Configuration as code for AI tools.** Manifest files define tools, permissions, and behavior. The Python script `agent_sync.py` generates the configuration required by each supported CLI, with `--revert` (undo a CLI's config from its own backup) and `--adopt` (read-only draft manifest entries for servers it finds outside the manifest).
- **Version-controlled memory.** The agents read and write Markdown files. Every change is stored in Git, can be reviewed with a diff, and can be reverted. Writes are compare-and-swap: whole-note, or per-section (`update_section`), so two agents editing different sections of the same note both land instead of colliding.
- **Link hygiene as discipline.** A deterministic, stdlib-only structural map of the vault (`vault-map`: broken wikilinks with relocation hints, orphan notes, hubs) is wired into the flows rather than left as a periodic check: every memory write returns an advisory list of unresolved wikilinks it just introduced (never blocking — deliberate forward links are legitimate), the grooming pass treats orphans and broken links as first-class cleanup candidates, `agent-doctor` keeps a warn-only backstop, and a read-only `map_overview` tool gives agents a token-bounded compass before broad tasks.
- **Cross-CLI command skills.** Declare a skill once and it surfaces as an explicitly invocable command on every supported runtime (`/name`, `$name` on Codex). Seven starter commands ship with the engine: `vault-doctor`, `vault-close`, `vault-save`, `vault-council`, `vault-groom`, `vault-update`, and `vault-map`.
- **Vault grooming, optional and manual.** `vault-groom.sh` and `vault-groom.ps1` use an LLM and a grooming playbook to flag stale, duplicate, or disconnected notes. A normal run and `preview` are read-only. `apply` shows the proposed changes and requires an explicit `yes` before writing in a disposable clone with no remote. An audit compares the result with the approved changes before promotion. If the audit fails, the original vault is left untouched. An optional n8n workflow sends a reminder every 14 days, but it never runs grooming unattended.
- **AI Council, Beta.** The local orchestrator `council.py` coordinates multiple models for brainstorming and relay tasks. The routing logic is implemented in Python, with explicit human selection of seats and models.
- **Configuration checks.** In MULTI profile, `agent-doctor` runs more than 30 read-only checks against the live configuration, vault wiring, skills, and secrets handling. It reports `pass`, `warn`, or `fail` for each check and returns a non-zero exit code when it finds an error. In MINIMAL, a single tool on a single machine is checked directly and no doctor is installed.
- **Cross-platform synchronization, optional.** In MULTI profile, the provisioner keeps generated files aligned across different machines, such as a Windows workstation and a Linux laptop. In MINIMAL, the provisioner is not installed.

## Architecture: The Three Planes

NeXgen Engine separates operations into three distinct planes:

1. **Behavior:** A single operating policy (`AGENTS.md`) linked into every runtime.
2. **Configuration:** An abstract MCP manifest compiled into each CLI's specific dialect by a generator script.
3. **Memory:** A plain-Markdown vault, written through serialized paths. 

Writes go through one door per kind of thing. Knowledge notes are written only through a memory tool server that serializes with a lock and an expected-hash check, preventing agents from overwriting each other's work.

**Optional skills are loaded only when needed.** Tool awareness and policy remain in the
bootstrap and MCP manifest. Optional task playbooks live outside automatic discovery roots and are opened only when needed. See
[`docs/lazy-skills.md`](docs/lazy-skills.md).

## Shared services via MCP

The tools can use shared services instead of each requiring a separate setup.
You deploy and manage these services in an environment you own, such as a VPS or local server.
The tools reach them through the Model Context Protocol (MCP).

> **Note:** These services are examples, not fixed dependencies. They were selected because they can run on an **Oracle Cloud Always Free VPS** with 4 ARM Ampere cores, 24 GB of RAM, and 200 GB of SSD storage. You can replace them with other services.

- **Semantic search, configured separately.** The `vault-library` MCP contract exposes `semantic_search`, and the repository includes the manifest and retrieval rules for using it. The search backend and its deployment code are not included in this repository, but [`03-INFRA/deploy/semantic-search-recipe.md`](03-INFRA/deploy/semantic-search-recipe.md) is a complete build specification (embedding model, hybrid ranking algorithm, weights, reranker, resource footprint) precise enough for an AI coding agent to build a compatible backend from scratch. Without a compatible backend, tools fall back to lexical search.
- **Web scraping.** You can deploy a Firecrawl instance using the files in `03-INFRA/deploy/firecrawl/`. It is the default read-only path for web content.
- **Local OCR.** You can deploy an OCR service using the files in `03-INFRA/deploy/ocr/` to extract text from screenshots, logs, and scanned documents locally.
- **Visible browser.** For forms, logins, and other interactive tasks, tools attach to a real Chrome window through the DevTools protocol. They must not use a headless browser for interactive work.

## Design boundaries

NeXgen uses Markdown, Git, and a small MCP server for durable memory that both people and tools can read.
It does not include a proprietary memory database, an autonomous multi-tool planner, a CRDT layer, or a second database.
The project focuses on configuration, versioned memory, and safety checks above the storage layer.

## Where this fits

If you mainly need to fan one set of rules and MCP config out to many tools, [ruler](https://github.com/intellectronica/ruler) and [rulesync](https://github.com/dyoshikawa/rulesync) are more mature and render to roughly 30 targets, including the four CLIs NeXgen supports. Start there if config fan-out is all you want. If you want Markdown memory an agent can read and write, [Cline's Memory Bank](https://github.com/cline/cline) convention and [basic-memory](https://github.com/basicmachines-co/basic-memory) popularized that idea and are further along.

NeXgen Engine sits where those two ideas meet, and adds one thing on the memory side. It renders MCP config per CLI *and* keeps working memory as plain Markdown in Git, where every write is a compare-and-swap: the memory server rejects a replace unless the caller's SHA-256 hash matches the current content — whole-note, or a single heading's section, so concurrent edits to different parts of one note both land — and each accepted write is its own Git commit that also reports any dead wikilinks it just introduced. That vault, the per-tool config, and a single AGENTS.md are carried between machines by a fail-closed sync, with a doctor that reports drift between the canonical source and the generated files, and a deterministic link-hygiene map over the memory itself.

It is a solo project under a noncommercial license, Linux stable and Windows in beta. It does not claim to be first at any of these pieces: the reason to look is the specific combination, and the write discipline on the memory.

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

The setup in `INIT.md` selects the mode that fits your environment.

## Installation profiles

The framework fits two shapes of usage. The installer (`INIT.md`) asks and picks the right one.

- **MINIMAL.** One CLI on one machine, such as Claude Code on a laptop or [OpenCode](https://opencode.ai) in a DeepSeek-based setup. You get the knowledge vault, bootstrap rules, optional skills, and a defined path for writing memory. There is no provisioner, scheduled doctor, or cross-machine synchronization. Add the MCP servers and skills you want directly to the CLI. This profile is intended for one user and one machine.
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

If you prefer fewer setup questions, use `AI-INSTALLER.md` instead of `INIT.md`. It follows the same process with only the required inputs.

## Prerequisites

- Git
- Python 3.11+ with PyYAML (`pip install pyyaml`), or Python 3.10 with `tomli` too (`pip install pyyaml tomli`)
- Node.js (for `npx`, needed if you mount MCP servers or external skills)
- Optional: [OpenCode](https://opencode.ai) as one of the supported CLIs
- `jq` and `curl` on Linux/Mac (only needed for the MULTI profile sync and health scripts)

## Platform status

**Linux: released.** Linux is the most extensively tested platform in this version. The provisioner, doctor, grooming, council, and synchronization tools have been exercised end to end on Fedora and pass CI. macOS uses the same POSIX code paths but has seen less real-world use.

**Known limitations.** Cross-platform support and the core orchestrators are still settling:
- **Windows: physically verified, not yet a cold install.** The provisioner (`agent_sync.py`), MCP renderer (`render.py`), PowerShell command shims, doctor, and Antigravity consumer path have run on real Windows hardware, in addition to the full `windows-latest` CI suite. The first guided install surfaced two real gaps (`vault-mcp` not bundled, Firecrawl not installable), both fixed and covered by `vault-mcp-smoke` since 0.5.0. Existing-install realignment has also been exercised repeatedly. What's still missing is an unassisted cold install, without the maintainer present to diagnose failures, which is the closer analog to a stranger's first experience. This unassisted cold install is a GA/1.0 onboarding gate, not a blocker for Beta: it exercises first-install UX, a General Availability concern rather than a maturity signal for existing installs. Until that happens, MINIMAL remains the more cautious starting point on Windows.
- **AI Council:** The deterministic orchestrator (`council.py`) supports `opencode`, `codex`, `claude`, and `ollama` seats; `agy` (Antigravity) is a recognized `cli` value but is currently refused as a passive seat: a live relay run (2026-07-15) found it ignores both the model selection and the given prompt, reading real local files instead of answering. Using `agy` interactively to call into Council itself is unaffected. See `docs/council.md`'s "Current limitations" for the finding and the conditions to re-enable it. Its optional routing adapter proposes exact locally verified models and efforts, with declared fallbacks, without letting an external workflow rewrite private cross-machine data or auto-invoke a seat. A human explicitly chooses the seat count and models.

## License

PolyForm Noncommercial License 1.0.0. Free for any noncommercial use, including reading, running, forking, and modifying it. See `LICENSE` for the full text. Any commercial use, of the original software or a derivative, needs a separate license from the author: see `COMMERCIAL.md`.

## Support

This project is free to use. Some optional links (like the OpenCode one above) are referral links that fund maintenance at no extra cost to you: see `SUPPORT.md` for the one place they're declared.

---

# NeXgen Engine, versione italiana, Beta

**Configura Claude Code, Codex, OpenCode e Antigravity da un unico repo Git che puoi diffare e revertare.**

NeXgen Engine è un framework basato su Git per gestire istruzioni condivise, configurazione dei tool e memoria di lavoro versionata tra più strumenti AI, come Claude Code.
Il progetto è in fase Beta; il badge a inizio pagina indica sempre la release più recente.
Può essere usato per progetti software, note, ricerca e documenti professionali.

Le istruzioni, la configurazione generata dei tool, i controlli sulle differenze, le regole per i segreti e la memoria condivisa sono file di testo dentro un repository Git, non dati conservati in un servizio ospitato.

Se usi Claude Code, Codex, OpenCode o Antigravity, ogni strumento ha il proprio file di bootstrap e la propria configurazione MCP.
Quando usi più strumenti o più macchine, questi file possono divergere senza un avviso evidente.
NeXgen mantiene una fonte canonica e controlla le differenze tra quella fonte e i file generati.

## A chi serve

NeXgen è pensato per chi usa uno o più strumenti AI sulle proprie macchine e vuole un setup funzionante e versionato.
Il profilo MULTI è utile quando usi più strumenti o mantieni lo stesso setup su più macchine.
Fornisce il provisioner e gli script di controllo descritti sotto.
Con un solo strumento su una sola macchina, il profilo MINIMAL offre regole condivise e una base di conoscenza versionata senza il livello di sincronizzazione.

Se lo stai valutando per più persone, tieni presente che il modello di identità e sicurezza è ancora pensato per un solo utente.
Prima di usarlo come infrastruttura condivisa, leggi [`docs/team.md`](docs/team.md) e, se stai pensando a un backend Cloud-Server comune, [`docs/org-deployment.md`](docs/org-deployment.md).
La postura di sicurezza e le istruzioni per segnalare problemi sono in [`SECURITY.md`](SECURITY.md).

## Percorso demo

1. Clona il repository ed esegui il preflight: `bash install.sh --check` su Linux o macOS, oppure `.\install.ps1 -Check` da PowerShell su Windows.
   Il comando controlla i prerequisiti, verifica la struttura del vault e mostra quali strumenti AI supportati trova.
   Non modifica nulla.
2. Apri `INIT.md` e incollalo in una CLI capace di modificare file, come Claude Code, Codex, OpenCode o Antigravity.
   Una chat web non può modificare il repository.
   La procedura chiede quanti strumenti e quante macchine vuoi usare, oltre alla modalità Local-Only o Cloud-Server, poi compila `99-INDEX/USER-PROFILE.md`.
   Se usi già delle CLI configurate con server MCP, skill o config tue, lo Step 1.5 prende in carico il setup esistente, fa l'inventario di quello che c'è e ti fa scegliere se adottarlo nella fonte canonica oppure ripartire da zero.
3. L'agente monta i server MCP e le skill per le CLI scelte, usando i manifest presenti in `03-INFRA/`.
4. Se usi il profilo MULTI, cioè almeno due CLI o due macchine, esegui `agent-sync apply` per propagare la configurazione canonica.
   Poi esegui `agent-doctor` per controllare lo stato reale, con oltre 30 verifiche su CLI, servizi VPS e gestione dei segreti.
   Ogni verifica restituisce `pass`, `warn` o `fail`.
   Su Windows, il primo `apply` aggiunge anche la cartella dei comandi al PATH dell'utente, quindi dopo devi aprire un nuovo terminale per usare direttamente `agent-sync`, `agent-doctor`, `vault-groom` e `vault-push`.
5. Modifica qualcosa fuori dal vault, per esempio una voce MCP o un file di configurazione, poi esegui di nuovo `agent-doctor`.
   Vedrai il controllo delle differenze in azione.

## Cosa fa e cosa non fa

NeXgen non è un'applicazione con interfaccia grafica, non offre una dashboard online e non include un motore di memoria proprietario.
Non è un builder RAG e non è un orchestratore di workflow.
Parte dal presupposto che tu abbia già scelto gli agenti e gli strumenti da usare, poi fornisce loro una base comune, versionata e verificabile.

NeXgen gestisce la configurazione attraverso una fonte canonica, file derivati generati automaticamente, controlli sulle differenze e percorsi separati per i diversi tipi di scrittura.
Non si mette però tra l'agente e i suoi tool mentre lavorano.
Per esempio, `agent-doctor` non può bloccare una chiamata che contiene argomenti plausibili ma sbagliati.
I controlli a runtime spettano all'harness della CLI, con i suoi permessi e le richieste di conferma, e ai server MCP, che validano le richieste lato server, per esempio con il lock `expected_hash` di `vault-library`.

## Concetti base

- **Infrastruttura come codice per gli agenti.** I manifest descrivono tool, permessi e regole di comportamento.
  Lo script Python unificato `agent_sync.py` genera poi il file di configurazione corretto per ogni CLI, con `--revert` (ripristino della config di una CLI dal suo backup) e `--adopt` (bozze read-only di voci manifest per i server trovati fuori dal manifest).
- **Memoria versionata in Git.** Gli agenti leggono e scrivono file Markdown.
  Ogni modifica entra nella storia del repository, si può controllare con un diff e si può annullare.
  Le scritture sono compare-and-swap: a nota intera oppure per singola sezione (`update_section`), così due agenti che modificano sezioni diverse della stessa nota atterrano entrambi invece di scontrarsi.
- **Igiene dei collegamenti come disciplina.** Una mappa strutturale deterministica del vault (`vault-map`: wikilink rotti con suggerimento di ricollocazione, note orfane, hub) è cablata nei flussi invece che lasciata come controllo periodico: ogni scrittura di memoria restituisce l'elenco advisory dei wikilink irrisolti appena introdotti (mai bloccante — il link "in avanti" deliberato è legittimo), il grooming tratta orfani e link rotti come candidati di pulizia di prima classe, `agent-doctor` tiene un paracadute warn-only e il tool read-only `map_overview` dà agli agenti una bussola a budget di token prima dei task larghi.
- **Comandi cross-CLI come skill.** Dichiari una skill una volta e diventa un comando invocabile su ogni runtime supportato (`/nome`, `$nome` su Codex). Sette comandi starter inclusi: `vault-doctor`, `vault-close`, `vault-save`, `vault-council`, `vault-groom`, `vault-update` e `vault-map`.
- **Grooming del vault, opzionale e manuale.** `vault-groom.sh` e `vault-groom.ps1` usano un playbook e un LLM per trovare note obsolete, duplicate o scollegate.
  L'esecuzione semplice, così come `preview`, è sempre in sola lettura.
  Con `vault-groom apply`, lo strumento propone una tranche di modifiche, la mostra per intero e avvia la scrittura solo dopo che hai digitato `yes`.
  La scrittura avviene in un clone usa e getta del vault, senza remote configurato, quindi da quel clone non è possibile fare push.
  Un audit confronta il risultato con la tranche approvata e promuove il lavoro nel vault reale solo se tutto torna.
  Se qualcosa non torna, il clone resta in quarantena e il vault originale non viene toccato.
  Puoi usare la CLI che hai già tra `claude`, `codex` e `agy`, tramite `GROOM_RUNNER`.
  Un workflow n8n opzionale ti ricorda ogni 14 giorni di eseguire il grooming, ma non avvia mai il lavoro al posto tuo.
- **Consiglio AI deterministico, in Beta.** `council.py` è un orchestratore locale per coordinare più modelli in attività di brainstorming o relay.
  Le regole di passaggio sono scritte in Python, non affidate a un altro LLM.
- **Controllo delle differenze.** Nel profilo MULTI, `agent-doctor` esegue oltre 30 verifiche in sola lettura sulla configurazione delle CLI, sul collegamento al vault, sulle skill e sulla gestione dei segreti.
  Per ogni voce mostra `pass`, `warn` o `fail` e restituisce un exit code diverso da zero se trova errori.
  Rileva configurazioni fuori posto, ma non blocca l'esecuzione degli agenti.
  Nel profilo MINIMAL non c'è un doctor, perché una sola CLI su una sola macchina si controlla direttamente.
- **Coerenza tra macchine, opzionale.** Nel profilo MULTI il provisioner mantiene lo stesso comportamento su macchine diverse, per esempio una workstation Windows e un portatile Linux.
  Nel profilo MINIMAL, con una sola macchina, il provisioner non serve e non viene installato.

## Architettura: i tre piani

NeXgen separa il sistema in tre piani:

1. **Comportamento.** Una sola policy operativa, `AGENTS.md`, collegata a ogni ambiente in cui gira una CLI.
2. **Configurazione.** Un manifest MCP astratto, trasformato dal generatore nel formato richiesto da ciascuna CLI.
3. **Memoria.** Un vault in Markdown, con le scritture serializzate per evitare conflitti.

Ogni tipo di scrittura passa dalla propria porta.
Le note, per esempio, vengono scritte solo tramite un server MCP che usa un lock e controlla l'hash atteso, così un agente non può sovrascrivere per errore il lavoro di un altro.

**Le skill vengono caricate solo quando servono.** Le regole e la conoscenza dei tool restano nel bootstrap e nel manifest MCP.
I playbook opzionali vivono fuori dalle cartelle di discovery automatica e vengono aperti solo per i task che ne hanno bisogno.
Vedi [`docs/lazy-skills.md`](docs/lazy-skills.md).

## Tool condivisi tramite MCP

Gli strumenti possono usare gli stessi servizi invece di configurarli da capo ogni volta.
Sono servizi che installi e gestisci tu in un ambiente di tua proprietà, non servizi offerti o amministrati dall'autore di NeXgen.
Gli agenti li raggiungono tramite il Model Context Protocol, MCP.

> **Nota:** questi servizi sono esempi, non dipendenze fisse.
> Sono stati scelti perché possono girare su una **VPS Oracle Cloud Always Free** con 4 core ARM Ampere, 24 GB di RAM e 200 GB di SSD.
> Puoi sostituirli con altri servizi.

- **Ricerca semantica, da configurare a parte.** Il contratto MCP `vault-library` espone già `semantic_search`, il manifest `manifest.yaml` lo dichiara e la governance di retrieval in `AGENTS.md` sa come usarlo.
  Il repository, però, non contiene il backend di ricerca né il suo codice di deploy: in `03-INFRA/deploy/` non c'è una cartella `semantic-search/` con un compose funzionante.
  C'è però [`03-INFRA/deploy/semantic-search-recipe.md`](03-INFRA/deploy/semantic-search-recipe.md): una ricetta di build completa (modello di embedding, algoritmo di ranking ibrido, pesi, reranker, ingombro di risorse) precisa abbastanza perché un agente AI possa costruire da zero un backend compatibile.
  Se vuoi usare questa funzione, devi costruire e gestire un servizio compatibile con quel contratto.
  In sua assenza, gli agenti ricadono sulla ricerca lessicale prevista dalla governance.
- **Web scraping.** Puoi installare un'istanza di Firecrawl usando i file di deploy in `03-INFRA/deploy/firecrawl/`.
  È la corsia predefinita per le letture web in sola lettura.
- **OCR locale.** Puoi installare un servizio OCR usando i file in `03-INFRA/deploy/ocr/`, per estrarre testo da screenshot, log e documenti scansionati senza inviarli a un servizio esterno.
- **Browser visibile.** Per form, login e controlli interattivi, gli strumenti si collegano alla finestra Chrome reale tramite il protocollo DevTools.
  Non devono usare un browser headless per le attività interattive.

## Confini del progetto

NeXgen usa Markdown, Git e un piccolo server MCP per una memoria durevole che persone e strumenti possono leggere.
Non include un database di memoria proprietario, un pianificatore autonomo multi-tool, un livello CRDT o un secondo database.
Il progetto si concentra sulla configurazione, sulla memoria versionata e sui controlli di sicurezza sopra lo storage.

## Come si colloca

Se ti serve soprattutto distribuire un set di regole e config MCP a molti strumenti, [ruler](https://github.com/intellectronica/ruler) e [rulesync](https://github.com/dyoshikawa/rulesync) sono più maturi e generano per una trentina di target, incluse le quattro CLI che NeXgen supporta. Parti da lì se ti basta il fan-out della configurazione. Se vuoi una memoria Markdown che un agente legge e scrive, la convenzione [Memory Bank di Cline](https://github.com/cline/cline) e [basic-memory](https://github.com/basicmachines-co/basic-memory) hanno reso popolare l'idea e sono più avanti.

NeXgen Engine sta dove queste due idee si incontrano, e aggiunge una cosa sul lato memoria. Genera la config MCP per ogni CLI *e* tiene la memoria di lavoro come Markdown puro in Git, dove ogni scrittura è un compare-and-swap: il server di memoria rifiuta un replace se l'hash SHA-256 di chi scrive non combacia col contenuto attuale — a nota intera o per singola sezione, così modifiche concorrenti a parti diverse della stessa nota atterrano entrambe — e ogni scrittura accettata diventa un suo commit Git, che segnala anche gli eventuali wikilink morti appena introdotti. Quel vault, la config per-strumento e un unico AGENTS.md vengono portati tra le macchine da un sync che fallisce chiuso, con un doctor che segnala il drift tra la sorgente canonica e i file generati, e una mappa deterministica di igiene dei collegamenti sopra la memoria stessa.

È un progetto solo-maintainer con licenza noncommerciale, Linux stabile e Windows in beta. Non pretende di essere il primo su nessuno di questi pezzi: il motivo per guardarlo è la combinazione specifica, e la disciplina di scrittura sulla memoria.

## Contenuto

| Directory | Scopo |
|---|---|
| `03-INFRA/` | Il motore, con le regole base (`AGENTS.md`), i manifest dei server MCP e gli script di validazione (`agent-sync`, `agent-doctor`). |
| `99-INDEX/` | Il livello di identità, con le informazioni su hardware, sistema operativo e contesto di deployment (`USER-PROFILE.md`). |
| `01-NOTES/` | Lo spazio di lavoro per la documentazione. |
| `02-PROJECTS/` | Il tracciamento dei progetti e delle attività. |
| `04-NOW/` | Le priorità attive, per evitare che gli agenti si disperdano in aree non pertinenti. |

## Modalità di deployment

1. **Local-Only.** Tutto gira sulla tua macchina, usando i tool nativi delle CLI e, se vuoi, modelli locali.
   È la modalità adatta per i test e per un setup personale.
2. **Cloud-Server.** Il vault si collega a uno stack remoto, per esempio n8n per l'orchestrazione, Firecrawl per lo scraping e un servizio OCR dedicato.
   Lo stack gira in un ambiente privato che installi e amministri tu, come una VPS o un server locale, e viene raggiunto tramite tunnel SSH.
   NeXgen non fornisce né ospita questi servizi, quindi i dati restano sotto il tuo controllo.

La procedura in `INIT.md` seleziona la modalità più adatta al tuo ambiente.

## Profili di installazione

Il setup guidato da `INIT.md` ti chiede quale dei due profili descrive meglio il tuo caso.

- **MINIMAL.** Una sola CLI su una sola macchina, per esempio Claude Code sul portatile oppure [OpenCode](https://opencode.ai) in un setup basato su DeepSeek.
  Ottieni il vault versionato, le regole di bootstrap, le skill caricate quando servono e un percorso definito per la scrittura della memoria.
  Non devi avviare un provisioner, programmare un doctor o sincronizzare più macchine.
  Monti manualmente nella CLI i server MCP e le skill che vuoi usare.
  È il profilo pensato per una persona che usa una sola CLI su una sola macchina.
- **MULTI.** Due o più CLI, oppure due o più macchine.
  Il provisioner Python `agent_sync.py`, il doctor e l'healthcheck mantengono ogni ambiente allineato alla fonte canonica nel vault.
  È il profilo adatto a una configurazione desktop più portatile o a chi usa più CLI in parallelo.

Nel profilo MULTI, la propagazione avviene come una transazione con lock e si interrompe in modo sicuro se qualcosa non torna.
Prima di rigenerare i file runtime, il pull deve dimostrare che i dati arrivano dal remote autorevole e sono aggiornati.
La pubblicazione è sempre un comando separato.
Il contratto completo è in [`docs/sync-contract.md`](docs/sync-contract.md).

Puoi iniziare con MINIMAL e passare a MULTI in seguito.
I file canonici del vault restano gli stessi in entrambi i profili.

## Installazione

Non devi preparare a mano i file di configurazione.

1. Clona il repository:
   ```bash
   git clone https://github.com/matteopasseri407/NeXgen-Engine.git ~/KnowledgeVault
   cd ~/KnowledgeVault
   ```
   > Preflight facoltativo: `bash install.sh` controlla i prerequisiti, verifica la struttura del vault, rileva le CLI installate e mostra il passo successivo.
   > Non scrive nulla ed è sicuro da eseguire più volte.
2. Apri `INIT.md`.
3. Incolla il contenuto in una **CLI capace di modificare file**, come Claude Code, Codex, OpenCode o Antigravity, aperta nella cartella del repository.
   Non usare una chat web come claude.ai o gemini, perché non può scrivere i file del progetto.
4. L'agente ti chiederà quante CLI e quante macchine vuoi usare, quali sono le caratteristiche del tuo computer e quale modalità di deployment preferisci.
   Poi configurerà il vault in automatico.

Se vuoi ridurre al minimo le domande, usa `AI-INSTALLER.md` al posto di `INIT.md`.
La procedura segue gli stessi passaggi e chiede solo le informazioni indispensabili.

## Prerequisiti

- Git.
- Python 3.11 o superiore con PyYAML, installabile con `pip install pyyaml`.
- Python 3.10 con PyYAML e `tomli`, installabili con `pip install pyyaml tomli`.
- Node.js, necessario per `npx` se vuoi montare server MCP o skill esterne.
- [OpenCode](https://opencode.ai), opzionale, come una delle CLI supportate.
- `jq` e `curl` su Linux o macOS, necessari solo per il sync e gli healthcheck del profilo MULTI.

## Stato per piattaforma

**Linux: rilasciato.** È la piattaforma usata ogni giorno e quella su cui il progetto è stato provato di più.
In questa versione, provisioner, doctor, grooming, council e sync sono stati verificati end to end su Fedora e passano la CI.
macOS segue gli stessi percorsi POSIX, ma ha ricevuto meno verifiche nell'uso reale.

**Limiti noti.** Il supporto multipiattaforma e gli orchestratori principali non sono ancora considerati definitivi.
- **Windows: verificato due volte su hardware fisico, manca ancora un'installazione "a freddo".** `agent_sync.py`, il generatore della configurazione MCP `render.py`, tramite un blocco di override `windows:` per ogni server nel manifest, e i launcher PowerShell includono un dialetto Windows.
  La CI esegue l'intera suite pytest su `windows-latest`, compresi i test PowerShell del grooming, nel job `engine-tests-windows` a ogni push.
  Oltre alla CI, il motore ha ormai girato per intero su hardware Windows reale: un'installazione guidata completa (profilo MULTI, tre CLI: Claude Code, Codex e Antigravity, più il deploy dello stack Cloud-Server su una VPS) su una macchina pulita, e il riallineamento di un'installazione esistente all'ultima release.
  La transazione bloccata di `agent-sync apply` (pull più propagazione) è girata per davvero su Windows in entrambi i casi, non solo in CI.
  La prima installazione guidata ha fatto emergere due lacune reali (`vault-mcp` non incluso nel bundle, Firecrawl non installabile), entrambe corrette e coperte dal job CI `vault-mcp-smoke` a partire dalla 0.5.0.
  Quello che manca ancora: un'installazione lasciata correre senza che il manutentore intervenga sugli errori, il test più vicino a come la vivrebbe davvero un utente nuovo.
  I percorsi del provisioner, degli shim PowerShell, del doctor e del consumer Antigravity sono stati verificati direttamente su Windows; manca ancora il collaudo a freddo senza assistenza del manutentore. Questa installazione a freddo non assistita è un gate di onboarding per la GA/1.0, non un blocco per la Beta: verifica la UX di prima installazione, una questione da General Availability più che un segnale di maturità per le installazioni esistenti.
  Finché non arriva un'installazione a freddo, MINIMAL resta il punto di partenza più prudente su Windows.
- **Consiglio AI.** L'orchestratore deterministico `council.py` supporta i seat `opencode`, `codex`, `claude` e `ollama`; `agy` (Antigravity) è un valore `cli` riconosciuto ma oggi rifiutato come seat passivo: una relay dal vivo (2026-07-15) ha trovato che ignora sia la selezione del modello sia il prompt dato, leggendo file locali reali invece di rispondere. Usare `agy` in modo interattivo per invocare il Council non è toccato da questo. Dettagli e condizioni per riabilitarlo in `docs/council.md`, sezione "Current limitations".
  Il routing opzionale propone modelli ed effort verificati localmente, con fallback espliciti.
  Non permette a un workflow esterno di riscrivere dati privati tra più macchine o di avviare automaticamente un seat.
  La scelta del numero di seat e dei modelli resta sempre esplicita e umana.
  I test automatici coprono il flusso dei quattro mode.

## Licenza

PolyForm Noncommercial License 1.0.0.
Il progetto è gratuito per qualsiasi uso non commerciale, compresi lettura, esecuzione, fork e modifiche.
Il testo completo è in `LICENSE`.
Qualsiasi uso commerciale del software originale o di un suo derivato richiede una licenza separata dell'autore, come spiegato in `COMMERCIAL.md`.

## Supporto

Il progetto è gratuito.
Alcuni link opzionali, incluso quello di OpenCode, sono referral link che aiutano a finanziare la manutenzione senza costi aggiuntivi per te.
Sono dichiarati tutti in `SUPPORT.md`.
