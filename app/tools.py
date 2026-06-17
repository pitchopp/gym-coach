"""Outils (function calling) exposés à Claude + leurs handlers.

Chaque handler exécute une mutation/lecture sur la base et renvoie une courte chaîne de confirmation
réinjectée comme tool_result. C'est par ces outils que le coach mémorise durablement.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app import repository

WEEKDAY_HINT = "0=lundi, 1=mardi, 2=mercredi, 3=jeudi, 4=vendredi, 5=samedi, 6=dimanche"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "update_profile",
        "description": (
            "Met à jour le profil durable de l'utilisateur. N'inclure que les champs connus. "
            "Appeler onboarding_done=true une fois la fréquence, les créneaux et les préférences "
            "de communication recueillis."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "timezone": {"type": "string", "description": "ex: Europe/Paris"},
                "training_frequency": {"type": "string", "description": "ex: 4x/semaine"},
                "comm_prefs": {
                    "type": "object",
                    "description": (
                        "Préférences de communication. Champs libres, ex: "
                        '{"tone": "tutoiement, motivant", '
                        '"quiet_hours": {"start": "22:00", "end": "08:00"}}'
                    ),
                },
                "onboarding_done": {"type": "boolean"},
            },
        },
    },
    {
        "name": "set_schedule",
        "description": (
            "Définit (remplace entièrement) les créneaux d'entraînement récurrents hebdomadaires. "
            f"weekday : {WEEKDAY_HINT}. time au format 24h 'HH:MM'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "slots": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "weekday": {"type": "integer", "minimum": 0, "maximum": 6},
                            "time": {"type": "string"},
                            "activity": {"type": "string"},
                        },
                        "required": ["weekday", "time"],
                    },
                }
            },
            "required": ["slots"],
        },
    },
    {
        "name": "log_session",
        "description": (
            "Enregistre l'issue d'une séance attendue (suite à une relance ou spontanément). "
            "status=done si la séance a été faite, skipped si annulée sans report, "
            "rescheduled si reportée (fournir reschedule_to au format YYYY-MM-DD). "
            "Sans checkin_id, cible la dernière relance en attente."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["done", "skipped", "rescheduled"]},
                "reschedule_to": {"type": "string", "description": "YYYY-MM-DD si rescheduled"},
                "activity": {"type": "string"},
                "note": {"type": "string"},
                "checkin_id": {"type": "integer"},
            },
            "required": ["status"],
        },
    },
    {
        "name": "save_program",
        "description": "Enregistre/met à jour le programme d'entraînement sur-mesure (markdown).",
        "input_schema": {
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
        },
    },
    {
        "name": "get_program",
        "description": "Récupère le programme d'entraînement actif de l'utilisateur.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "remember_fact",
        "description": (
            "Mémorise un fait durable utile au coaching (blessure, objectif, matériel, "
            "préférence d'exercice...). Clé courte, valeur descriptive."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
            "required": ["key", "value"],
        },
    },
    {
        "name": "recall_facts",
        "description": "Liste tous les faits mémorisés sur l'utilisateur.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        # Outil de PRÉSENTATION (pas de mutation DB) : capté directement par run_agent pour attacher
        # un reply keyboard au message. Il n'a donc volontairement pas de handler dans handle_tool.
        "name": "suggest_replies",
        "description": (
            "Propose 2 à 4 réponses rapides en boutons Telegram quand ta question a des réponses "
            "probables ; l'utilisateur garde toujours la saisie libre. À appeler dans le même "
            "message que la question. N'utilise pas pour une question ouverte."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "options": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["options"],
        },
    },
]


def handle_tool(
    user_id: int, name: str, payload: dict[str, Any], conn: sqlite3.Connection
) -> str:
    """Exécute l'outil `name` et renvoie un texte de confirmation (tool_result)."""
    if name == "update_profile":
        repository.update_user(user_id, conn=conn, **payload)
        return "Profil mis à jour : " + ", ".join(payload.keys())

    if name == "set_schedule":
        slots = payload.get("slots", [])
        repository.set_schedule(user_id, slots, conn=conn)
        return f"{len(slots)} créneau(x) enregistré(s)."

    if name == "log_session":
        return _handle_log_session(user_id, payload, conn)

    if name == "save_program":
        repository.save_program(user_id, payload["content"], conn=conn)
        return "Programme enregistré."

    if name == "get_program":
        program = repository.get_active_program(user_id, conn=conn)
        return program["content"] if program else "Aucun programme actif."

    if name == "remember_fact":
        repository.remember_fact(user_id, payload["key"], payload["value"], conn=conn)
        return f"Fait mémorisé : {payload['key']}."

    if name == "recall_facts":
        facts = repository.list_facts(user_id, conn=conn)
        if not facts:
            return "Aucun fait mémorisé."
        return "\n".join(f"- {f['key']}: {f['value']}" for f in facts)

    return f"Outil inconnu : {name}"


def _handle_log_session(user_id: int, payload: dict[str, Any], conn: sqlite3.Connection) -> str:
    status = payload["status"]
    checkin_id = payload.get("checkin_id")
    checkin = None
    if checkin_id:
        checkin = conn.execute(
            "SELECT * FROM checkins WHERE id = ? AND user_id = ?", (checkin_id, user_id)
        ).fetchone()
    if checkin is None:
        checkin = repository.latest_open_checkin(user_id, conn=conn)

    reschedule_to = payload.get("reschedule_to") if status == "rescheduled" else None

    if checkin is None:
        # Pas de relance en attente : on ne fait que tracer un report éventuel.
        if status == "rescheduled" and reschedule_to:
            repository.create_checkin(
                user_id,
                due_date=reschedule_to,
                due_time="18:00",
                activity=payload.get("activity"),
                conn=conn,
            )
            return f"Séance (re)planifiée pour le {reschedule_to}."
        return "Aucune relance en attente ; rien à clôturer."

    repository.resolve_checkin(
        checkin["id"], status, note=payload.get("note"), reschedule_to=reschedule_to, conn=conn
    )

    if status == "rescheduled" and reschedule_to:
        repository.create_checkin(
            user_id,
            due_date=reschedule_to,
            due_time=checkin["due_time"],
            activity=checkin["activity"] or payload.get("activity"),
            conn=conn,
        )
        return f"Séance reportée au {reschedule_to} ; je relancerai ce jour-là."

    labels = {"done": "faite", "skipped": "annulée"}
    return f"Séance marquée comme {labels.get(status, status)}."
