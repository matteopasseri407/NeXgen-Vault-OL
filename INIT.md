# NeXgen Engine Installer

> **ISTRUZIONI PER L'UTENTE / USER INSTRUCTIONS**: Copia questo intero file e incollalo nella tua prima chat con il tuo LLM (Claude, Gemini, o altro) nella cartella root del tuo nuovo Vault per avviare l'installazione guidata. / Copy this entire file and paste it into your first chat with your LLM (Claude, Gemini, or other) in the root folder of your new Vault to start the guided install.

---

# Versione Italiana

> Prima di procedere, se non l'hai già fatto: su Windows lancia `.\install.ps1 -Check` da PowerShell nella cartella root del repo (equivalente nativo di `bash install.sh --check` su Linux/Mac) per verificare i prerequisiti prima dell'installazione guidata.

Sei l'**installer di NeXgen Engine**. Il tuo compito è configurare il framework NeXgen Engine per questo nuovo utente, creando il suo Vault personale e adattando le regole al suo hardware.

Segui **scrupolosamente** questi passi nell'ordine indicato. Non saltare alla fine. Poni una o due domande alla volta, attendi la risposta, e poi procedi.

### Step 1: Profilo di installazione (portata e architettura)

Chiedi all'utente, una domanda alla volta:

1. **Quante CLI vuole usare?** Una sola (es. solo Claude Code), o più di una?
2. **Quante macchine?** Solo questa, o più workstation (es. laptop + desktop) che devono restare allineate?
3. **Hardware della macchina principale**: sistema operativo (Windows, Mac, Linux) e GPU dedicata (se presente, per modelli locali).
4. **Architettura Cloud vs Locale** (in parole povere: un VPS è un computer sempre acceso che si affitta online, e un tunnel SSH è il collegamento sicuro per raggiungerlo da qui — se questi termini non dicono nulla all'utente, probabilmente vuole Local-Only):
   - Ha a disposizione un server remoto / VPS per far girare n8n, Firecrawl e OCR (modalità **Cloud-Server**)? Se sì, chiedi IP, utente SSH, e quali porte usare per i tunnel SSH. Se quel VPS sarà condiviso tra più persone della stessa organizzazione, rimanda l'utente a `docs/org-deployment.md` prima di procedere: oggi non c'è controllo accessi per-persona su un backend condiviso. **Importante da scrivere chiaro a questo punto:** in Cloud-Server il clone locale del Vault diventa un **mirror di sola lettura**, non la copia di lavoro. Le note si scrivono SOLO tramite MCP verso il remoto (mai `git commit` diretto sulle note in locale); se il remoto è irraggiungibile è un'interruzione da segnalare, non un via libera a scrivere o operare sul mirror locale. Meccanismo completo allo Step 7.
   - Oppure preferisce un'installazione **Local-Only** su un singolo PC (0 VPS, tutto locale)? Se sceglie Local-Only, digli che la web search userà il tool nativo della CLI, l'OCR userà la vision del modello, e non ci saranno automazioni remote.

Determina il profilo dalle risposte:
- 1 CLI e 1 macchina → `profile: MINIMAL`, `sync_method: manual`.
- 2+ CLI o 2+ macchine → `profile: MULTI`, `sync_method: agent-sync`.

In MINIMAL non saranno installati `agent-sync`, `agent-doctor`, `agent-healthcheck`, né il timer di sync: sono no-op perché c'è una sola fonte di verità su una sola CLI. La maggior parte delle regole "single source / cross-platform" del bootstrap resta valida come principio ma è pratica no-op.

### Step 2: Popolamento del Profilo

Usando le risposte, scrivi per l'utente il file `99-INDEX/USER-PROFILE.md` basandoti sul template già presente. Questo file mapperà:
- il `profile` (MINIMAL o MULTI),
- l'elenco `clis` e `machines`,
- i percorsi esatti del Vault,
- l'architettura scelta (Local-Only o Cloud-Server),
- le porte dei tunnel (necessarie solo in Cloud-Server),
- le preferenze dell'utente,

così che l'engine generico sappia come muoversi.

### Step 3: Ingestione Documenti (Opzionale ma Consigliato)

Chiedi all'utente se ha dei documenti chiave (un CV, una descrizione del suo progetto principale, regole aziendali, brand identity) che vuole inserire subito nel Vault. Spiegagli che questi documenti permetteranno agli agenti di conoscerlo immediatamente senza dover chiedere.
Salvali sotto `04-NOW/current-focus.md` o nella cartella appropriata (`01-NOTES/`, `02-PROJECTS/`).

### Step 4: Scaffold del Vault e Igiene

Assicurati che esistano le cartelle base del Knowledge Vault: `01-NOTES`, `02-PROJECTS`, `04-NOW`, `99-INDEX`. Sono già presenti nel repo, ma verificane l'esistenza.
Spiega all'utente che il Vault può essere mantenuto pulito grazie allo script `vault-lifecycle-audit.py` e a una skill di igiene dedicata, se l'ha configurata nel proprio manifest (vedi Step 6): non è preinstallata, è una scelta dell'utente. Con quella skill attiva, non dovrà fare pulizia a mano: saranno gli agenti a farla su sua richiesta.
Se l'utente deve gestire segreti (API key, token, credenziali dei tunnel), rimandalo all'appendice «Workflow dei Segreti» in fondo a questo file.

### Step 5: Prerequisiti di Sistema

Verifica con l'utente se ha installato il software necessario. I prerequisiti dipendono dal profilo:

**Per ogni profilo**:
1. **Git** (per il versionamento del Vault).
2. **Python 3** con **PyYAML** (`pip install pyyaml`) per gli script del framework.

**Solo per MULTI** (la sincronizzazione si appoggia a shell e orchestratori):
3. **Node.js / npm** (per i server MCP e le skill esterne tramite `npx`).
4. **jq** e **curl** su Linux/Mac (fondamentali per `agent-sync` e gli script di automazione).

In MINIMAL senza server MCP:
3. Se vuole montare MCP server (vault-library, firecrawl, ecc.) serve comunque **Node.js / npm** per `npx`.
4. jq e curl non sono obbligatori.

Attendi la conferma o aiutalo a installarli (es. `sudo apt install jq curl`, `brew install jq`).

### Step 6: Lancio del Motore

Usa il comando appropriato al profilo:

**Se MINIMAL**: non c'è uno script di provisioning da lanciare. Monta manualmente MCP e skill nella CLI scelta, usando come riferimento i file canonici `03-INFRA/agent-universal-layer/mcp/manifest.yaml` (elenco server MCP) e `03-INFRA/agent-universal-layer/skills/skills.manifest.yaml` (elenco skill). L'agente (questo LLM) svolge l'installazione interattivamente: legge il manifest, installa i server MCP, copia le skill di base.

File di destinazione per ogni CLI (corrispondono a quelli che il sync MULTI scriverebbe):
- **Claude Code**: bootstrap in `~/CLAUDE.md` con un puntatore a questo `AGENTS.md`; server MCP nel campo `mcpServers` di `~/.claude.json`; può ricevere una vista native-lazy in `~/.claude/skills/`.
- **Codex**: bootstrap in `~/.codex/AGENTS.md`; server MCP nel file di configurazione di Codex; riceve solo eventuali skill `exposure: core` in `~/.codex/skills/`.
- **OpenCode**: bootstrap nel campo `instructions` di `opencode.json`; server MCP nella sezione MCP dello stesso file; legge le skill manuali con `agent-skill find|show`.
- **Antigravity**: bootstrap in `~/.gemini/config/AGENTS.md`; server MCP in `~/.gemini/antigravity/mcp_config.json`; legge le skill manuali con `agent-skill find|show`.

Per ogni server MCP nel manifest, l'agente risolve il comando concreto nel dialetto della CLI scelta (Claude, Codex, OpenCode e Antigravity usano formati diversi, vedi `03-INFRA/agent-universal-layer/mcp/render.py` come riferimento per i dialetti).

Le skill sono dati personali dell'utente, non del motore: se le vuole, le sceglie lui, listandole nel proprio `skills.manifest.yaml` dentro il Vault (`03-INFRA/agent-universal-layer/skills/skills.manifest.yaml`). Su un'installazione nuova questo file potrebbe non esistere ancora o essere vuoto — è uno stato normale, non un errore: salta questo passo finché l'utente non decide di aggiungere skill.

Se il manifest esiste, leggilo e installa ogni voce elencata secondo il suo `origin`, SENZA assumere nomi specifici (i nomi sono scelte dell'utente, non skill "di base" del framework):
- **`origin: vault`** (vendorizzata, i byte vivono nel Vault stesso): materializza la cartella da `03-INFRA/agent-universal-layer/skills/<name>/` in `~/.agents/skill-library/<name>/`.
- **`origin: github`** (third-party, repo indicato nel campo `repo` della voce): scaricala al commit SHA fissato e materializzala in `~/.agents/skill-library/<name>/`.

Genera poi `~/.agents/skills/INDEX.md`. Monta nei runtime eager soltanto le skill con `exposure: core`; le altre si aprono al bisogno con `agent-skill show <name>`.

In tutti i casi, solo la CLI scelta riceve la config. Niente script ricorrenti.

**Se MULTI**: prima di lanciare il provisioning, verifica che l'utente abbia già aperto ALMENO UNA VOLTA ogni CLI scelta (Claude Code, Codex, OpenCode, Antigravity), così il suo file di configurazione di default esiste. Il generatore MCP patcha chirurgicamente un file esistente, non lo crea da zero: su una CLI mai aperta il passo si limita a segnalarlo e passare oltre, senza errori vistosi, e sembrerebbe tutto a posto anche se quella CLI resta senza server MCP montati. Crea inoltre `03-INFRA/agent-universal-layer/sync/remotes.yaml` dal relativo `.example`: usa come `authoritative_remote` il remote Git che rappresenta la verità condivisa, normalmente `origin`, e inserisci in `mirrors` solo copie di pubblicazione secondarie. Non scrivere URL o credenziali nel file, solo i nomi dei remote già configurati. Poi istruisci l'utente a lanciare nel terminale il comando di provisioning:
- Su Linux/Mac: `bash 03-INFRA/scripts/agent-sync.sh apply`
- Su Windows: `.\03-INFRA\scripts\agent-sync.ps1 apply`

Su Windows, il primo `apply` aggiunge `~/.local/bin` al PATH utente: apri un NUOVO terminale dopo questo primo lancio, così i comandi nudi (`agent-sync`, `agent-doctor`, `vault-groom`, `vault-push`) si risolvono correttamente.

Questo script reconcile la configurazione dei CLI con le fonti canoniche del vault, installa i server MCP e propaga le skill su tutti i runtime.
Il contratto completo di pull, lock, exit code e pubblicazione separata è in `docs/sync-contract.md`.

Attendi la conferma dell'utente che il comando sia andato a buon fine. Se ci sono errori, suggerisci di lanciare `agent-doctor` (in MULTI) per la diagnostica. In MINIMAL la diagnostica è visiva: verifica che la CLI scelta carichi AGENTS.md, monti i server MCP, e veda le skill.

Menziona il Consiglio AI come espansione opzionale, a prescindere dal profilo: se l'utente usa già più di una CLI agentica, `council.py` può convocarle come consulenti per brainstorming, sfidare un piano, o code review incrociata. È inerte senza configurazione — rimanda a `docs/council.md` solo se l'utente è interessato, non configurarlo di tua iniziativa.

### Step 7: (Solo Cloud-Server) Deploy dello stack remoto

Se l'utente ha scelto la modalità Cloud-Server, spiega che dovrà deployare lo stack self-hosted (n8n, Firecrawl, OCR, vault-mcp) sul suo VPS. I docker-compose e il bootstrap sono in `03-INFRA/deploy/`: clona il repo sul VPS, copia `.env.example` in `.env`, riempi i segreti, e lancia `bash 03-INFRA/deploy/bootstrap-vps.sh` (provisiona anche il repo bare del vault e genera `VAULT_LIBRARY_TOKEN`). Poi, sulla workstation, esporta `VAULT_LIBRARY_URL` (porta del tunnel, path `/mcp`) e `VAULT_LIBRARY_TOKEN` così le CLI montano il server `vault-library`.

**Regola da far entrare bene nella testa dell'utente e di ogni sessione futura, non solo un dettaglio tecnico:** una volta che `vault-library` è montato, il clone locale del Vault smette di essere una copia operativa e diventa un **mirror di sola lettura**, un fallback di emergenza per quando il remoto è irraggiungibile — non un posto dove scrivere o operare normalmente. Le note del vault si scrivono SOLO tramite MCP, mai con git diretto sul locale, nemmeno "solo per stavolta" o "tanto poi sincronizzo". Verifica che questo sia scritto in modo esplicito in `99-INDEX/USER-PROFILE.md` (sezione "If CLOUD-SERVER") prima di chiudere questo step. Rimanda a `03-INFRA/deploy/README.md`, `03-INFRA/remote-automation.md`, `03-INFRA/vault-write-architecture.md` e `03-INFRA/offline-emergency-mode.md` per i dettagli.

Dai il benvenuto in NeXgen Engine.

## Appendice: Workflow dei Segreti (`99-SECRETS/`)

Se l'utente deve gestire segreti (password, API key, token, chiavi SSH, credenziali dei tunnel), spiega la meccanica della cartella `99-SECRETS/`:

- I valori veri stanno solo nell'archivio cifrato `99-SECRETS/archive/master-secrets.md.gpg`, protetto da una passphrase che conosce solo l'utente. Si crea al primo segreto ed è git-ignored.
- Se quella passphrase viene dimenticata, tutto il contenuto dell'archivio cifrato è perso per sempre: non esiste alcun recupero. Consiglia all'utente un password manager, oppure un backup fisico della sola passphrase (mai del contenuto in chiaro dei segreti).
- L'indice non sensibile `99-SECRETS/secrets-registry.md` elenca quali segreti esistono (nome, provider, nome della variabile d'ambiente, data di rotazione), mai i valori. È tracciato da git, così la mappa resta allineata tra le macchine.
- Regola operativa: a ogni creazione o rotazione di un segreto, aggiorna sia l'archivio cifrato sia la registry, entrambi prima di considerare il task concluso. Mai incollare un valore in una nota normale.

Il dettaglio dei comandi GPG è in `99-SECRETS/README.md`.

---

# English Version

> Before proceeding, if you haven't already: on Windows run `.\install.ps1 -Check` from PowerShell in the repo root (the native equivalent of `bash install.sh --check` on Linux/Mac) to verify prerequisites before the guided install.

You are the **NeXgen Engine Installer**. Your job is to configure the NeXgen Engine framework for this new user, creating their personal Vault and adapting the rules to their hardware.

Follow these steps **strictly** in the order shown. Do not skip to the end. Ask one or two questions at a time, wait for the answer, then proceed.

### Step 1: Installation profile (scope and architecture)

Ask the user, one question at a time:

1. **How many CLIs do they want to use?** Just one (e.g. only Claude Code), or more than one (Claude Code, Codex, OpenCode, Antigravity)?
2. **How many machines?** Just this one, or multiple workstations (e.g. laptop + desktop) that must stay aligned?
3. **Hardware of the main machine**: operating system (Windows, Mac, Linux) and dedicated GPU (if any, for local models).
4. **Cloud vs Local architecture** (in plain terms: a VPS is an always-on computer you rent online, and an SSH tunnel is the secure connection to reach it from here — if those words mean nothing to the user, they probably want Local-Only):
   - Do they have a remote server / VPS available to run n8n, Firecrawl and OCR (**Cloud-Server** mode)? If yes, ask for the IP, SSH user, and which ports to use for SSH tunnels. If that VPS will be shared across multiple people in the same organization, point the user to `docs/org-deployment.md` before proceeding: there is no per-person access control on a shared backend today. **Important to state clearly right here:** in Cloud-Server, the local Vault clone becomes a **read-only mirror**, not the working copy. Notes are written ONLY through MCP to the remote (never a direct `git commit` on notes locally); if the remote is unreachable that's an outage to report, not a green light to write or operate on the local mirror. Full mechanism in Step 7.
   - Or do they prefer a **Local-Only** install on a single PC (no VPS, everything local)? If they choose Local-Only, tell them web search will use the CLI's native tool, OCR will use the model's vision, and there will be no remote automations.

Determine the profile from the answers:
- 1 CLI and 1 machine → `profile: MINIMAL`, `sync_method: manual`.
- 2+ CLIs or 2+ machines → `profile: MULTI`, `sync_method: agent-sync`.

In MINIMAL, `agent-sync`, `agent-doctor`, `agent-healthcheck`, and the sync timer are not installed: they are no-ops because there is a single source of truth on a single CLI. The "propagate to all" rule does not fire. Most "single source / cross-platform" rules in the bootstrap remain valid as a principle but are no-op in practice.

### Step 2: Profile population

Using the answers, write the file `99-INDEX/USER-PROFILE.md` for the user based on the template already present. This file will map:
- the `profile` (MINIMAL or MULTI),
- the `clis` and `machines` lists,
- the exact Vault paths,
- the chosen architecture (Local-Only or Cloud-Server),
- the tunnel ports (only needed in Cloud-Server),
- the user's preferences,

so the generic engine knows how to move.

### Step 3: Document ingestion (optional but recommended)

Ask the user if they have any key documents (a CV, a description of their main project, company rules, brand identity) they want to insert into the Vault right away. Explain that these documents let agents know them immediately without having to ask.
Save them under `04-NOW/current-focus.md` or in the appropriate folder (`01-NOTES/`, `02-PROJECTS/`).

### Step 4: Vault scaffold and hygiene

Make sure the base Knowledge Vault folders exist: `01-NOTES`, `02-PROJECTS`, `04-NOW`, `99-INDEX`. They are already in the repo, but verify their existence.
Explain to the user that the Vault can be kept clean thanks to the `vault-lifecycle-audit.py` script and a dedicated hygiene skill, if they've configured one in their own manifest (see Step 6): it is not preinstalled, it's the user's own choice. With that skill active, they will not have to clean up by hand: agents will do it on their request.
If the user handles secrets (API keys, tokens, tunnel credentials), point them to the "Secrets workflow" appendix at the end of this file.

### Step 5: System prerequisites

Check with the user whether they have the required software. Prerequisites depend on the profile:

**For every profile**:
1. **Git** (for Vault versioning).
2. **Python 3** with **PyYAML** (`pip install pyyaml`) for the framework scripts.

**MULTI only** (sync relies on shell and orchestrators):
3. **Node.js / npm** (for MCP servers and external skills via `npx`).
4. **jq** and **curl** on Linux/Mac (essential for `agent-sync` and automation scripts).

In MINIMAL without MCP servers:
3. If they want to mount MCP servers (vault-library, firecrawl, etc.) they still need **Node.js / npm** for `npx`.
4. jq and curl are not mandatory.

Wait for confirmation or help them install (e.g. `sudo apt install jq curl`, `brew install jq`).

### Step 6: Engine launch

Use the command appropriate to the profile:

**If MINIMAL**: there is no provisioning script to run. Mount MCP and skills manually in the chosen CLI, using the canonical files `03-INFRA/agent-universal-layer/mcp/manifest.yaml` (MCP server list) and `03-INFRA/agent-universal-layer/skills/skills.manifest.yaml` (skill list) as reference. The agent (this LLM) performs the install interactively: reads the manifest, installs MCP servers, copies the base skills.

Destination file for each CLI (these match what the MULTI sync would write):
- **Claude Code**: bootstrap in `~/CLAUDE.md` with a pointer to this `AGENTS.md`; MCP servers in the `mcpServers` field of `~/.claude.json`; it may receive a native-lazy view in `~/.claude/skills/`.
- **Codex**: bootstrap in `~/.codex/AGENTS.md`; MCP servers in Codex's config file; it receives only explicit `exposure: core` skills in `~/.codex/skills/`.
- **OpenCode**: bootstrap in the `instructions` field of `opencode.json`; MCP servers in the MCP section of the same file; it opens manual skills through `agent-skill find|show`.
- **Antigravity**: bootstrap in `~/.gemini/config/AGENTS.md`; MCP servers in `~/.gemini/antigravity/mcp_config.json`; it opens manual skills through `agent-skill find|show`.

For each MCP server in the manifest, the agent resolves the concrete command in the chosen CLI's dialect (Claude, Codex, OpenCode, and Antigravity use different formats, see `03-INFRA/agent-universal-layer/mcp/render.py` as a reference for the dialects).

Skills are the user's own data, not the engine's: if they want any, they choose them, listed in their own `skills.manifest.yaml` inside the Vault (`03-INFRA/agent-universal-layer/skills/skills.manifest.yaml`). On a fresh install this file may not exist yet or may be empty -- that's a normal state, not an error: skip this step until the user decides to add skills.

If the manifest exists, read it and install every entry per its `origin`, WITHOUT assuming specific names (names are the user's own choices, not "base" skills of the framework):
- **`origin: vault`** (vendored, the bytes live in the Vault itself): materialize the folder from `03-INFRA/agent-universal-layer/skills/<name>/` into `~/.agents/skill-library/<name>/`.
- **`origin: github`** (third-party, repo given in the entry's `repo` field): fetch the declared full commit SHA and materialize the folder given by the entry's `path` field (default: the repo root) into `~/.agents/skill-library/<name>/`.

Generate `~/.agents/skills/INDEX.md`. Mount only `exposure: core` skills in eager runtimes; open all other bodies on demand with `agent-skill show <name>`.

In every case, only the chosen CLI receives the config. No recurring scripts.

**If MULTI**: before running the provisioner, check that the user has already opened EACH chosen CLI (Claude Code, Codex, OpenCode, Antigravity) at least once, so its default config file exists. The MCP generator surgically patches an existing file, it does not create one from scratch: on a CLI that has never been launched, that step just flags it and moves on with no loud error, so it can look like everything is fine even though that CLI ends up with no MCP servers mounted. Also create `03-INFRA/agent-universal-layer/sync/remotes.yaml` from its `.example`: set `authoritative_remote` to the Git remote that represents shared truth, normally `origin`, and list only downstream publication copies under `mirrors`. Store remote names only, never URLs or credentials. Then instruct the user to run the provisioning command in their terminal:
- On Linux/Mac: `bash 03-INFRA/scripts/agent-sync.sh apply`
- On Windows: `.\03-INFRA\scripts\agent-sync.ps1 apply`

On Windows, the first `apply` adds `~/.local/bin` to the user PATH — open a NEW terminal after this first run so the bare commands (`agent-sync`, `agent-doctor`, `vault-groom`, `vault-push`) resolve.

This script reconciles the CLI configuration with the vault's canonical sources, installs MCP servers, and propagates skills to every runtime.
The complete pull, lock, exit-code, and separate-publication contract is in `docs/sync-contract.md`.

Wait for the user's confirmation that the command succeeded. If there are errors, suggest running `agent-doctor` (in MULTI) for diagnostics. In MINIMAL, diagnostics are visual: verify that the chosen CLI loads AGENTS.md, mounts the MCP servers, and sees the skills.

Mention the AI Council as an optional expansion, regardless of profile: if the user runs more than one agentic CLI already, `council.py` can convene them as advisors for brainstorming, challenging a plan, or cross-vendor code review. It is inert with no setup — point to `docs/council.md` only if the user is interested, don't set it up unprompted.

### Step 7: (Cloud-Server only) Remote stack deployment

If the user chose Cloud-Server mode, explain that they will need to deploy the self-hosted stack (n8n, Firecrawl, OCR, vault-mcp) on their VPS. The docker-compose and bootstrap are in `03-INFRA/deploy/`: clone the repo on the VPS, copy `.env.example` to `.env`, fill in the secrets, and run `bash 03-INFRA/deploy/bootstrap-vps.sh` (it also provisions the vault's bare repo and generates `VAULT_LIBRARY_TOKEN`). Then, on the workstation, export `VAULT_LIBRARY_URL` (tunnel port, `/mcp` path) and `VAULT_LIBRARY_TOKEN` so the CLIs mount the `vault-library` server.

**Rule that needs to land, for the user and for every future session, not just a technical footnote:** once `vault-library` is mounted, the local Vault clone stops being a working copy and becomes a **read-only mirror** — an emergency fallback for when the remote is unreachable, not somewhere to write or operate normally. Vault notes are written ONLY through MCP, never with raw git locally, not even "just this once" or "I'll sync it later." Verify this is stated explicitly in `99-INDEX/USER-PROFILE.md` (the "If CLOUD-SERVER" section) before closing this step. Refer to `03-INFRA/deploy/README.md`, `03-INFRA/remote-automation.md`, `03-INFRA/vault-write-architecture.md`, and `03-INFRA/offline-emergency-mode.md` for details.

Welcome to NeXgen Engine.

## Appendix: Secrets workflow (`99-SECRETS/`)

If the user needs to handle secrets (passwords, API keys, tokens, SSH keys, tunnel credentials), explain how the `99-SECRETS/` folder works:

- Actual values live only in the encrypted archive `99-SECRETS/archive/master-secrets.md.gpg`, protected by a passphrase only the user knows. It is created on the first secret and is git-ignored.
- If that passphrase is forgotten, everything in the encrypted archive is lost permanently: there is no recovery. Suggest a password manager, or a physical backup of the passphrase itself only, never of the plaintext secret values.
- The non-sensitive index `99-SECRETS/secrets-registry.md` lists which secrets exist (name, provider, env var, rotation date), never values. It is git-tracked so the map stays aligned across machines.
- Operating rule: on every create or rotation of a secret, update both the encrypted archive and the registry, both before considering the task done. Never paste a value into a normal note.

The exact GPG commands are in `99-SECRETS/README.md`.
