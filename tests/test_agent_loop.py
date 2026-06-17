"""Test de la boucle tool-use de l'agent avec un client Anthropic mocké (déterministe)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app import agent, repository


@dataclass
class _Text:
    text: str
    type: str = "text"


@dataclass
class _ToolUse:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class _Response:
    content: list[Any]
    stop_reason: str


@dataclass
class _FakeMessages:
    scripted: list[_Response]
    calls: list[dict] = field(default_factory=list)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.scripted.pop(0)


class _FakeClient:
    def __init__(self, scripted: list[_Response]):
        self.messages = _FakeMessages(scripted)


def test_run_agent_executes_tool_then_returns_text(conn):
    user = repository.get_or_create_user(7, conn=conn)
    user = repository.get_user(user["id"], conn=conn)

    client = _FakeClient(
        [
            # 1er tour : Claude appelle un outil.
            _Response(
                content=[
                    _ToolUse(
                        id="t1",
                        name="update_profile",
                        input={"training_frequency": "4x/semaine", "onboarding_done": True},
                    )
                ],
                stop_reason="tool_use",
            ),
            # 2e tour : réponse finale en texte.
            _Response(content=[_Text("Parfait, c'est noté ! 💪")], stop_reason="end_turn"),
        ]
    )

    reply = agent.run_agent(
        conn, user, [{"role": "user", "content": "Je m'entraîne 4x par semaine"}], client=client, model="test"
    )

    assert reply.text == "Parfait, c'est noté ! 💪"
    assert reply.quick_replies == []
    refreshed = repository.get_user(user["id"], conn=conn)
    assert refreshed["onboarding_done"] == 1
    assert refreshed["training_frequency"] == "4x/semaine"
    # Deux appels API : avant et après l'exécution de l'outil.
    assert len(client.messages.calls) == 2
    # Le 2e appel contient bien le tool_result réinjecté.
    second = client.messages.calls[1]["messages"]
    assert any(
        isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result"
        for m in second
        if isinstance(m["content"], list)
    )


def test_run_agent_plain_text_no_tool(conn):
    user = repository.get_or_create_user(8, conn=conn)
    user = repository.get_user(user["id"], conn=conn)
    client = _FakeClient(
        [_Response(content=[_Text("Salut ! Comment puis-je t'aider ?")], stop_reason="end_turn")]
    )

    reply = agent.run_agent(
        conn, user, [{"role": "user", "content": "bonjour"}], client=client, model="test"
    )
    assert "Salut" in reply.text
    assert len(client.messages.calls) == 1


def test_now_str_inclut_iso_et_gere_fuseau_invalide():
    import re

    s = agent._now_str("Europe/Paris")
    assert "Europe/Paris" in s
    assert re.search(r"ISO \d{4}-\d{2}-\d{2}", s)  # date ISO exploitable par le modèle
    # Un fuseau invalide ne doit pas faire planter le snapshot (repli UTC).
    fallback = agent._now_str("Pas/Un/Fuseau")
    assert re.search(r"ISO \d{4}-\d{2}-\d{2}", fallback)


def test_run_agent_captures_suggest_replies(conn):
    user = repository.get_or_create_user(9, conn=conn)
    user = repository.get_user(user["id"], conn=conn)
    client = _FakeClient(
        [
            # 1er tour : Claude écrit la question ET propose des choix dans le même message.
            _Response(
                content=[
                    _Text("Tu t'entraînes combien de fois par semaine ?"),
                    _ToolUse(id="s1", name="suggest_replies", input={"options": ["2x", "3x", "4x"]}),
                ],
                stop_reason="tool_use",
            ),
            # 2e tour : rien à ajouter.
            _Response(content=[_Text("")], stop_reason="end_turn"),
        ]
    )

    reply = agent.run_agent(
        conn, user, [{"role": "user", "content": "salut"}], client=client, model="test"
    )
    assert reply.text == "Tu t'entraînes combien de fois par semaine ?"
    assert reply.quick_replies == ["2x", "3x", "4x"]
    # suggest_replies est capté par run_agent : il renvoie un tool_result et NE touche pas la base.
    second = client.messages.calls[1]["messages"]
    assert any(
        isinstance(m["content"], list)
        and m["content"][0].get("type") == "tool_result"
        and m["content"][0].get("content") == "Choix proposés."
        for m in second
        if isinstance(m["content"], list)
    )


def test_run_agent_escalade_vers_modele_fort(conn):
    from app.config import get_settings

    user = repository.get_or_create_user(10, conn=conn)
    user = repository.get_user(user["id"], conn=conn)
    client = _FakeClient(
        [
            # Passe rapide : le modèle réclame l'escalade.
            _Response(
                content=[_ToolUse(id="e1", name="escalate_to_sonnet", input={"reason": "programme"})],
                stop_reason="tool_use",
            ),
            # Passe forte : réponse finale.
            _Response(content=[_Text("Voici ton programme full-body 💪")], stop_reason="end_turn"),
        ]
    )

    # model=None -> chemin de routage (rapide puis escalade).
    reply = agent.run_agent(conn, user, [{"role": "user", "content": "fais-moi un programme"}], client=client)

    s = get_settings()
    assert reply.text == "Voici ton programme full-body 💪"
    assert len(client.messages.calls) == 2
    assert client.messages.calls[0]["model"] == s.model_fast  # passe rapide = Haiku
    assert client.messages.calls[1]["model"] == s.model  # escalade = modèle fort
    names_fast = {t["name"] for t in client.messages.calls[0]["tools"]}
    names_strong = {t["name"] for t in client.messages.calls[1]["tools"]}
    assert "escalate_to_sonnet" in names_fast  # outil proposé sur la passe rapide
    assert "escalate_to_sonnet" not in names_strong  # mais pas sur la passe forte


def test_run_agent_sans_escalade_reste_sur_haiku(conn):
    from app.config import get_settings

    user = repository.get_or_create_user(11, conn=conn)
    user = repository.get_user(user["id"], conn=conn)
    client = _FakeClient(
        [_Response(content=[_Text("Bien reçu, bravo 👍")], stop_reason="end_turn")]
    )

    reply = agent.run_agent(conn, user, [{"role": "user", "content": "c'est fait !"}], client=client)

    assert reply.text == "Bien reçu, bravo 👍"
    assert len(client.messages.calls) == 1  # une seule passe
    assert client.messages.calls[0]["model"] == get_settings().model_fast


def test_escalade_ignore_les_autres_outils_de_la_meme_reponse(conn):
    user = repository.get_or_create_user(12, conn=conn)
    user = repository.get_user(user["id"], conn=conn)
    client = _FakeClient(
        [
            # La passe rapide met update_profile ET escalate dans la même réponse : la mutation ne
            # doit PAS être exécutée (on abandonne la passe avant tout outil).
            _Response(
                content=[
                    _ToolUse(id="u1", name="update_profile", input={"training_frequency": "5x"}),
                    _ToolUse(id="e1", name="escalate_to_sonnet", input={}),
                ],
                stop_reason="tool_use",
            ),
            _Response(content=[_Text("ok")], stop_reason="end_turn"),
        ]
    )

    agent.run_agent(conn, user, [{"role": "user", "content": "x"}], client=client)

    refreshed = repository.get_user(user["id"], conn=conn)
    assert refreshed["training_frequency"] != "5x"  # update_profile non exécuté
    assert len(client.messages.calls) == 2  # escalade bien déclenchée
