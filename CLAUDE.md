# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Présentation

Coach sportif IA **proactif** sur Telegram, propulsé par Claude (Messages API). Il discute, construit
un programme sur-mesure, **mémorise** durablement le profil de chaque utilisateur et **relance au bon
moment sans spammer**. Accepte aussi les messages vocaux (transcription locale faster-whisper).

Stack : Python 3.14 (géré par **uv**), `anthropic` + boucle tool-use maison, FastAPI (webhook),
APScheduler (proactivité), SQLite (WAL), Docker via Dokploy.

## Commandes

```bash
uv sync                                  # installe les dépendances (depuis uv.lock)
uv run ruff check .                      # lint (règles E,F,I,UP,B ; line-length 110)
uv run ruff check . --fix                # corrige automatiquement
uv run pytest                            # tous les tests
uv run pytest tests/test_scheduler_logic.py             # un fichier
uv run pytest tests/test_scheduler_logic.py::test_nom   # un seul test
uv run uvicorn app.main:app --reload     # lance le service en local
```

Toujours préfixer par `uv run` (pas de `pip`/`venv` manuel — voir mémoire projet). pytest est en
`asyncio_mode = auto` : les tests async n'ont pas besoin de décorateur.

## Architecture

Flux d'un message : Telegram → `main.webhook` → `coach.handle_incoming` → `agent.run_agent`
(boucle Claude + outils) → réponse renvoyée sur Telegram. Le moteur de proactivité tourne en
parallèle via APScheduler (`main._tick` toutes les `TICK_MINUTES`).

Modules (`app/`), du plus central au périphérique :

- **`agent.py`** — boucle conversationnelle Claude. `run_agent` assemble le `system` (identité Claude
  Code en mode OAuth seulement + persona en cache + snapshot d'état non caché via `build_state_snapshot`,
  qui inclut date/heure courantes), puis route : passe sur `model_fast` (Haiku) avec l'outil
  `escalate_to_sonnet` ; si le modèle l'appelle, `_run_pass` est rejoué sur `model` (Sonnet) sans cet
  outil, depuis les messages d'origine. Un `model` explicite force une passe unique (tests). `_run_pass`
  exécute les `tool_use` jusqu'à `end_turn` (`MAX_TOOL_ITERATIONS=8`) et accumule le texte de **tous** les
  tours ; il abandonne la passe AVANT d'exécuter le moindre outil si `escalate_to_sonnet` apparaît (pas de
  mutation). La boucle Claude est **synchrone** ; les appelants la lancent dans un thread
  (`asyncio.to_thread`). `summarize_conversation` (sur `model`, le modèle fort) produit le résumé glissant.
- **`tools.py`** — outils (function calling) exposés à Claude : `update_profile`, `set_schedule`,
  `log_session`, `save_program`/`get_program`, `remember_fact`/`recall_facts`. Chaque handler mute la
  base et renvoie une courte chaîne de confirmation (tool_result). **C'est par ces outils que le coach
  mémorise durablement** — l'historique conversationnel n'est pas une source de vérité fiable.
- **`coach.py`** — orchestration. Un **verrou asyncio par chat_id** sérialise message entrant et
  relance proactive sur un même utilisateur. Gère la mémoire conversation 3 couches (voir ci-dessous),
  les relances (`handle_proactive`, `handle_onboarding_nudge`) et le repli en cas de panne
  (`notify_failure` / `SERVER_ERROR_REPLY`).
- **`scheduler.py`** — moteur de proactivité, **logique pure et testable** (horloge `Clock`
  injectable, envoi/rédaction injectés en callbacks). Matérialisation idempotente des check-ins
  quotidiens depuis les créneaux récurrents, anti-spam (un check-in `pending`→`asked` une seule fois),
  respect du délai de grâce et des heures calmes.
- **`repository.py`** — CRUD SQLite. Toutes les fonctions acceptent une `conn` explicite (tests) ou
  retombent sur la connexion globale.
- **`auth.py`** — authentification Claude. Si `ANTHROPIC_API_KEY` est défini → **clé API dédiée**
  (`build_client` renvoie `Anthropic(api_key=...)`, pas d'OAuth) ; sinon → **OAuth d'abonnement** avec
  refresh automatique et cooldown anti-429. `using_api_key()` indique le mode. Voir section dédiée.
- **`telegram.py`** — client API Bot (envoi, webhook, download de fichiers), `parse_update`.
- **`transcribe.py`** — faster-whisper local (CPU, int8), modèle chargé paresseusement, inférence
  sérialisée par verrou.
- **`db.py`** — connexion SQLite unique (WAL), migrations idempotentes.
- **`config.py`** — `Settings` lu depuis l'env (`.env` en local), caché par `lru_cache`.

### Modèle de proactivité (check-ins)

Le cœur anti-spam/report est la table `checkins` (statuts `pending|asked|done|skipped|rescheduled`).
Chaque tick : `materialize_day` crée un check-in `pending` par créneau dû du jour (idempotent via
index unique `(user_id, due_date, slot_id)`), puis `due_checkins` retient ceux échus (après
créneau + grâce, hors heures calmes). Une relance fait `pending`→`asked` (jamais relancé deux fois).
La réponse de l'utilisateur clôt le check-in via l'outil `log_session` ; un report (`rescheduled`)
crée un nouveau check-in à la date cible.

### Mémoire conversation (3 couches)

1. **État structuré** — profil/créneaux/programme/faits/check-ins ouverts, injecté à chaque tour par
   `build_state_snapshot`.
2. **Résumé glissant** (`users.summary`, `summary_through_id`) — quand les messages « vivants »
   dépassent `SUMMARY_TRIGGER`, les plus anciens sont condensés (`_maybe_summarize`, best-effort).
3. **Messages vivants** — les messages d'id > `summary_through_id` envoyés verbatim (cap
   `summary_trigger + summary_keep_recent`).

## Auth (deux modes)

**Mode clé API (prod actuelle, recommandé)** : si `ANTHROPIC_API_KEY` est défini, `build_client` renvoie
`Anthropic(api_key=...)`. Aucun refresh, aucune rotation, limites isolées de l'usage Claude Code perso —
c'est le mode qui évite les `429` sur `/oauth/token`. Dans ce mode, **ni** l'en-tête beta **ni** le bloc
d'identité « Claude Code » ne sont envoyés (`run_agent`/`summarize_conversation` les omettent via
`auth.using_api_key()`).

**Mode OAuth d'abonnement (repli)** : utilisé si `ANTHROPIC_API_KEY` est absent. Deux contraintes non
négociables pour que l'API accepte la requête :
- en-tête `anthropic-beta: oauth-2025-04-20` (posé par `auth.build_client`) ;
- le **1er bloc system DOIT être** exactement `CLAUDE_CODE_IDENTITY` (« You are Claude Code... »).

Le token d'accès (~8 h) est rafraîchi automatiquement et persisté sur `/data` (le refresh token tourne à
chaque rafraîchissement). Un cooldown après échec évite la rafale de 429. ⚠️ Les rate limits du refresh
OAuth sont **partagés** avec l'usage Claude Code perso → c'était la cause racine des pannes `429`, d'où la
bascule en clé API. Pour rétablir l'OAuth si besoin, voir la mémoire projet (« Récupération token OAuth »).

**Déploiement (Dokploy)** : la prod utilise `ANTHROPIC_API_KEY` (clé API dédiée). En repli OAuth, fournir
`CLAUDE_OAUTH_JSON`
(contenu JSON du Keychain « Claude Code-credentials ») comme variable d'environnement — il sert au
**seed initial** des creds si le fichier `OAUTH_CREDS_PATH` (`/data/claude_oauth.json`) est absent.
Le fichier vivant sur le volume `/data` faisant foi ensuite, `CLAUDE_OAUTH_JSON` n'est relu qu'au
premier démarrage sur un volume vierge.

## Conventions

- Migrations : fichiers `migrations/NNN_*.sql` appliqués une seule fois, dans l'ordre, idempotents
  (les `ALTER ... ADD COLUMN` en double sont tolérés). Ajouter une migration = nouveau fichier
  numéroté ; ne jamais éditer une migration déjà déployée.
- Un seul worker uvicorn (connexion SQLite partagée) ; les accès concurrents par utilisateur sont
  sérialisés par le verrou applicatif de `coach.py`.
- Le webhook acquitte **toujours** en 200 (un non-2xx ferait re-livrer l'update en boucle par
  Telegram) ; les erreurs sont loguées et l'utilisateur reçoit le message de repli.
- Code et commentaires en français.
