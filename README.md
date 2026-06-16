# gym-coach

Coach sportif IA **proactif** sur Telegram, propulsé par Claude (Messages API).

Il discute, conseille, construit un programme sur-mesure, **mémorise** durablement le profil de
chaque utilisateur (fréquence, créneaux, préférences, programme, faits perso) et **relance au bon
moment sans spammer** : une seule relance par séance attendue, report automatique si l'utilisateur
dit « j'irai demain ».

Il accepte aussi les **messages vocaux** : transcription locale via faster-whisper (sans clé API),
puis traités comme du texte.

## Stack

- Python 3.14, géré par [uv](https://docs.astral.sh/uv/)
- `anthropic` (Messages API) + boucle tool-use maison
- FastAPI (webhook Telegram) + APScheduler (moteur de proactivité)
- SQLite (mode WAL)
- Déploiement Docker via Dokploy

## Développement local

```bash
uv sync                       # installe les dépendances
cp .env.example .env          # puis renseigner les clés
uv run ruff check .           # lint
uv run pytest                 # tests
uv run uvicorn app.main:app --reload   # lance le service
```

Variables d'environnement : voir `.env.example`.

## Mise en service Telegram

1. Créer un bot via [@BotFather](https://t.me/BotFather) → récupérer `TELEGRAM_BOT_TOKEN`.
2. Renseigner `PUBLIC_URL` (domaine public HTTPS) et `WEBHOOK_SECRET`.
3. Au démarrage, le service enregistre automatiquement le webhook
   (`POST {PUBLIC_URL}/webhook/{WEBHOOK_SECRET}`).

## Déploiement Dokploy

Le service est conteneurisé (`Dockerfile`, base `python:3.14-slim` + uv) et décrit par
`docker-compose.yml`. Le volume nommé `coach_data` monté sur `/data` conserve la base SQLite entre
les redéploiements.

Étapes :

1. **Bot Telegram** : créer le bot via [@BotFather](https://t.me/BotFather), récupérer le token.
2. **Application Dokploy** : créer une app de type *Compose* (ou *Docker*) pointant sur ce dépôt.
3. **Domaine** : attribuer un domaine HTTPS (Traefik s'occupe du certificat). Ce domaine = `PUBLIC_URL`.
4. **Variables d'environnement** (dans Dokploy) :
   - `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`
   - `PUBLIC_URL` (= le domaine attribué), `WEBHOOK_SECRET` (chaîne longue aléatoire)
   - optionnel : `MODEL`, `TICK_MINUTES`, `DEFAULT_TZ`
5. **Déployer**. Au démarrage, le service enregistre seul son webhook Telegram
   (`POST {PUBLIC_URL}/webhook/{WEBHOOK_SECRET}`). Vérifier `GET {PUBLIC_URL}/health` → `{"status":"ok"}`.

### Vérification end-to-end

- Écrire au bot sur Telegram → l'onboarding démarre (fréquence, créneaux, préférences) et le profil
  est persisté (vérifiable en redémarrant le conteneur : la mémoire survit).
- Forcer une évaluation de proactivité sans attendre le tick :
  `POST {PUBLIC_URL}/internal/tick?secret={WEBHOOK_SECRET}`.
- Répondre « j'irai demain » à une relance → aucune nouvelle relance le jour même, relance le lendemain.
