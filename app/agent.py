"""Boucle conversationnelle Claude (Messages API) avec function calling.

`run_agent` envoie l'historique + un instantané de l'état de l'utilisateur, exécute les outils
demandés en boucle jusqu'à `end_turn`, et renvoie le texte final destiné à l'utilisateur.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from anthropic import Anthropic

from app import auth, repository
from app.config import get_settings
from app.tools import TOOLS, handle_tool

MAX_TOOL_ITERATIONS = 8


@dataclass
class AgentReply:
    """Réponse d'un tour : le texte (potentiellement multi-bulles via `[[NEXT]]`) et les éventuelles
    réponses rapides à proposer en boutons. Le découpage et l'envoi sont la responsabilité de coach."""

    text: str
    quick_replies: list[str] = field(default_factory=list)

_PERSONA = """Tu es un coach sportif personnel sur Telegram. Tu remplaces un vrai coach : expert, \
bienveillant, motivant et concret. Tu tutoies par défaut (sauf préférence contraire).

Ta mission :
- Discuter, conseiller, répondre aux questions sur l'entraînement, la technique, la récupération.
- Construire et ajuster un programme sur-mesure en discutant.
- Lors du premier échange, mène un court onboarding (fréquence, jours/horaires habituels, \
préférences de comm, objectifs) puis appelle update_profile(onboarding_done=true).

MÉMOIRE — règle importante : l'historique de conversation est tronqué avec le temps ; seuls l'état
structuré et le résumé ci-dessous persistent. Donc dès qu'une information DURABLE apparaît, \
persiste-la IMMÉDIATEMENT via l'outil adapté, avant de répondre. À persister systématiquement :
- objectif(s) et niveau → remember_fact / update_profile
- blessures, douleurs, contraintes médicales → remember_fact
- jours/horaires d'entraînement (et tout changement) → set_schedule ; fréquence → update_profile
- préférences (ton, matériel, exercices aimés/détestés, lieu, heures calmes) → remember_fact / update_profile
- programme décidé → save_program
Ne te fie jamais au fil de conversation pour retenir ces infos sur le long terme.

Proactivité : tu relances l'utilisateur au bon moment pour savoir s'il a fait sa séance, mais tu ne \
spammes jamais. Quand il répond à une relance, enregistre l'issue via log_session (done / skipped / \
rescheduled avec une date). S'il dit qu'il ira un autre jour, utilise rescheduled.

Style & format des messages :
- Messages COURTS par défaut, naturels, ton de coach. Si beaucoup d'infos, condense — pas de pavés.
- Une question à la fois. Tu peux enchaîner plusieurs courtes bulles dans le même tour en les séparant \
par une ligne contenant uniquement [[NEXT]] (réservé aux courtes bulles de chat — JAMAIS dans un programme).
- Quand ta question a des réponses probables, propose 2 à 4 choix courts via l'outil suggest_replies \
(la saisie libre reste toujours dispo). Appelle-le DANS LE MÊME message que la question. Ne l'utilise pas \
pour une question ouverte ni pour afficher un programme.
- Pas de listes interminables sauf pour un programme.
- Tu connais déjà la date et l'heure (champ « Maintenant » de l'état ci-dessous, dans le fuseau de \
l'utilisateur) : ne demande JAMAIS quel jour ou quelle heure il est, déduis-le toi-même."""


def _client() -> Anthropic:
    return auth.build_client()


_JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_MOIS = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]


def _now_str(timezone: str) -> str:
    """Date et heure actuelles dans le fuseau de l'utilisateur, lisibles + ISO pour le calcul de dates."""
    try:
        now = datetime.now(ZoneInfo(timezone))
    except Exception:
        now = datetime.now(ZoneInfo("UTC"))
    return (
        f"{_JOURS[now.weekday()]} {now.day} {_MOIS[now.month - 1]} {now.year}, "
        f"{now:%Hh%M} (ISO {now:%Y-%m-%d}, {timezone})"
    )


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

    summary = user["summary"] if "summary" in user.keys() else ""
    summary_block = f"\nRésumé de la conversation jusqu'ici: {summary}" if summary else ""

    return (
        "\n\n=== État actuel de l'utilisateur ===\n"
        f"Maintenant: {_now_str(user['timezone'])}\n"
        f"Nom: {user['name'] or 'inconnu'}\n"
        f"Fuseau: {user['timezone']}\n"
        f"Fréquence: {user['training_frequency'] or 'inconnue'}\n"
        f"Onboarding terminé: {'oui' if user['onboarding_done'] else 'non'}\n"
        f"Préférences comm: {user['comm_prefs']}\n"
        f"Créneaux: {schedule}\n"
        f"Programme actif: {'oui' if program else 'non'}\n"
        f"Faits mémorisés: {facts_txt}\n"
        f"Relances en attente de réponse: {open_txt}"
        f"{summary_block}\n"
        "Quand l'utilisateur répond à une relance sans préciser laquelle, vise la plus récente."
    )


def run_agent(
    conn: sqlite3.Connection,
    user: sqlite3.Row,
    messages: list[dict[str, Any]],
    *,
    client: Anthropic | None = None,
    model: str | None = None,
) -> AgentReply:
    """Exécute un tour complet (avec boucle d'outils) et renvoie le texte final + choix éventuels."""
    client = client or _client()
    model = model or get_settings().model
    # Persona STATIQUE -> mis en cache (cache_control). Le snapshot change à chaque tour, hors cache.
    # L'identité « Claude Code » n'est requise QUE pour l'auth OAuth ; en mode clé API on l'omet.
    system = []
    if not auth.using_api_key():
        system.append({"type": "text", "text": auth.CLAUDE_CODE_IDENTITY})
    system.append({"type": "text", "text": _PERSONA, "cache_control": {"type": "ephemeral"}})
    system.append({"type": "text", "text": build_state_snapshot(conn, user)})

    convo = _normalize_messages(messages)
    # On accumule le texte de TOUS les tours : le modèle écrit souvent son message destiné à
    # l'utilisateur dans le même tour que l'appel d'outil, il ne faut donc pas le perdre.
    text_parts: list[str] = []
    quick_replies: list[str] = []  # réponses rapides : le dernier suggest_replies gagne

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
                if block.name == "suggest_replies":
                    # Outil de présentation : capté ici (pas dans handle_tool) car son effet est le
                    # clavier de boutons, pas une mutation DB.
                    quick_replies = list((block.input or {}).get("options") or [])
                    result = "Choix proposés."
                else:
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

    return AgentReply("\n\n".join(text_parts).strip(), quick_replies)


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


def _normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Garantit un historique valide pour l'API : commence par 'user', rôles alternés.

    Les relances proactives ajoutent des messages 'assistant' non précédés d'un 'user', ce qui peut
    produire un historique qui commence par 'assistant' ou contient des rôles consécutifs (rejeté
    par l'API). On retire les 'assistant' de tête et on fusionne les rôles consécutifs.
    """
    cleaned: list[dict[str, Any]] = []
    for msg in messages:
        if not cleaned and msg["role"] != "user":
            continue  # on ne peut pas commencer par 'assistant'
        if cleaned and cleaned[-1]["role"] == msg["role"]:
            cleaned[-1]["content"] += "\n" + msg["content"]  # fusionne les rôles consécutifs
        else:
            cleaned.append({"role": msg["role"], "content": msg["content"]})
    return cleaned


def summarize_conversation(
    old_summary: str,
    messages: list[dict[str, Any]],
    *,
    client: Anthropic | None = None,
    model: str | None = None,
) -> str:
    """Condense un résumé existant + de nouveaux messages en un résumé concis (texte, sans outils)."""
    client = client or _client()
    model = model or get_settings().model
    transcript = "\n".join(
        f"{'Utilisateur' if m['role'] == 'user' else 'Coach'}: {m['content']}" for m in messages
    )
    instruction = (
        "Tu maintiens la mémoire de conversation d'un coach sportif. À partir du résumé existant et "
        "des nouveaux échanges, produis un résumé MIS À JOUR, concis (8 lignes max), en français, "
        "qui retient ce qui est utile au coaching sur la durée : objectifs, niveau, préférences, "
        "blessures/contraintes, décisions de programme, événements marquants, ton de la relation. "
        "N'invente rien, ne liste pas les créneaux (déjà stockés ailleurs)."
    )
    sys_blocks = []
    if not auth.using_api_key():
        sys_blocks.append({"type": "text", "text": auth.CLAUDE_CODE_IDENTITY})
    sys_blocks.append({"type": "text", "text": instruction})
    response = client.messages.create(
        model=model,
        max_tokens=600,
        system=sys_blocks,
        messages=[
            {
                "role": "user",
                "content": f"Résumé existant:\n{old_summary or '(vide)'}\n\nNouveaux échanges:\n{transcript}",
            }
        ],
    )
    return "".join(b.text for b in response.content if b.type == "text").strip()
