"""Boucle conversationnelle Claude (Messages API) avec function calling.

`run_agent` envoie l'historique + un instantané de l'état de l'utilisateur, exécute les outils
demandés en boucle jusqu'à `end_turn`, et renvoie le texte final destiné à l'utilisateur.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from anthropic import Anthropic

from app import auth, repository
from app.config import get_settings
from app.tools import TOOLS, handle_tool

MAX_TOOL_ITERATIONS = 8

_PERSONA = """Tu es un coach sportif personnel sur Telegram. Tu remplaces un vrai coach : expert, \
bienveillant, motivant et concret. Tu tutoies par défaut (sauf préférence contraire).

Ta mission :
- Discuter, conseiller, répondre aux questions sur l'entraînement, la technique, la récupération.
- Construire et ajuster un programme sur-mesure en discutant.
- Apprendre et MÉMORISER durablement ce qui compte (fréquence, créneaux, préférences de
  communication, objectifs, blessures...) via les outils. Dès qu'une info durable apparaît dans la
  conversation, persiste-la avec l'outil adapté plutôt que de la garder seulement dans le fil.
- Lors du premier échange, mène un court onboarding (fréquence, jours/horaires habituels, \
préférences de comm, objectifs) puis appelle update_profile(onboarding_done=true).

Proactivité : tu relances l'utilisateur au bon moment pour savoir s'il a fait sa séance, mais tu ne \
spammes jamais. Quand il répond à une relance, enregistre l'issue via log_session (done / skipped / \
rescheduled avec une date). S'il dit qu'il ira un autre jour, utilise rescheduled.

Style : messages courts, naturels, ton de coach. Pas de listes interminables sauf pour un programme."""


def _client() -> Anthropic:
    return auth.build_client()


def build_state_snapshot(conn: sqlite3.Connection, user: sqlite3.Row) -> str:
    """Décrit l'état courant de l'utilisateur, injecté dans le system prompt à chaque tour."""
    days = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
    slots = repository.list_slots(user["id"], conn=conn)
    schedule = (
        ", ".join(f"{days[s['weekday']]} {s['time']} ({s['activity'] or 'séance'})" for s in slots)
        or "non défini"
    )
    facts = repository.list_facts(user["id"], conn=conn)
    facts_txt = "; ".join(f"{f['key']}: {f['value']}" for f in facts) or "aucun"
    program = repository.get_active_program(user["id"], conn=conn)
    open_checkins = repository.list_open_checkins(user["id"], conn=conn)
    open_txt = (
        "; ".join(
            f"#{c['id']} {c['due_date']} {c['activity'] or 'séance'}" for c in open_checkins
        )
        or "aucune"
    )

    return (
        "\n\n=== État actuel de l'utilisateur ===\n"
        f"Nom: {user['name'] or 'inconnu'}\n"
        f"Fuseau: {user['timezone']}\n"
        f"Fréquence: {user['training_frequency'] or 'inconnue'}\n"
        f"Onboarding terminé: {'oui' if user['onboarding_done'] else 'non'}\n"
        f"Préférences comm: {user['comm_prefs']}\n"
        f"Créneaux: {schedule}\n"
        f"Programme actif: {'oui' if program else 'non'}\n"
        f"Faits mémorisés: {facts_txt}\n"
        f"Relances en attente de réponse: {open_txt}\n"
        "Quand l'utilisateur répond à une relance sans préciser laquelle, vise la plus récente."
    )


def run_agent(
    conn: sqlite3.Connection,
    user: sqlite3.Row,
    messages: list[dict[str, Any]],
    *,
    client: Anthropic | None = None,
    model: str | None = None,
) -> str:
    """Exécute un tour complet (avec boucle d'outils) et renvoie le texte final."""
    client = client or _client()
    model = model or get_settings().model
    # En OAuth, le 1er bloc system doit être l'identité Claude Code ; le persona vient ensuite.
    system = [
        {"type": "text", "text": auth.CLAUDE_CODE_IDENTITY},
        {"type": "text", "text": _PERSONA + build_state_snapshot(conn, user)},
    ]

    convo = list(messages)
    # On accumule le texte de TOUS les tours : le modèle écrit souvent son message destiné à
    # l'utilisateur dans le même tour que l'appel d'outil, il ne faut donc pas le perdre.
    text_parts: list[str] = []

    for _ in range(MAX_TOOL_ITERATIONS):
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            system=system,
            tools=TOOLS,
            messages=convo,
        )

        iter_text = ""
        tool_results = []
        for block in response.content:
            if block.type == "text":
                iter_text += block.text
            elif block.type == "tool_use":
                result = handle_tool(user["id"], block.name, block.input or {}, conn)
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": result}
                )
        if iter_text.strip():
            text_parts.append(iter_text.strip())

        if response.stop_reason != "tool_use":
            break

        # Rejoue le tour : on réinjecte l'appel d'outil de l'assistant + les résultats.
        convo.append({"role": "assistant", "content": _serialize_blocks(response.content)})
        convo.append({"role": "user", "content": tool_results})

    return "\n\n".join(text_parts).strip()


def _serialize_blocks(content: list[Any]) -> list[dict[str, Any]]:
    """Convertit les blocs de réponse en blocs réinjectables dans la conversation."""
    serialized: list[dict[str, Any]] = []
    for block in content:
        if block.type == "text":
            serialized.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            serialized.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
            )
    return serialized
