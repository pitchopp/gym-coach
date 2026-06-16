"""Tests de la mémoire conversation : normalisation des messages + résumé glissant."""

from __future__ import annotations

from app import agent, coach, repository


def test_normalize_drops_leading_assistant_and_merges():
    raw = [
        {"role": "assistant", "content": "relance proactive"},  # ne peut pas démarrer
        {"role": "assistant", "content": "2e assistant consécutif"},
        {"role": "user", "content": "salut"},
        {"role": "user", "content": "ça va ?"},  # consécutif user -> fusion
        {"role": "assistant", "content": "oui"},
    ]
    out = agent._normalize_messages(raw)
    assert [m["role"] for m in out] == ["user", "assistant"]
    assert "salut" in out[0]["content"] and "ça va" in out[0]["content"]


def test_live_messages_excludes_summarized(conn):
    u = repository.get_or_create_user(1, conn=conn)
    ids = []
    for i in range(5):
        repository.add_message(u["id"], "user", f"m{i}", conn=conn)
        ids.append(conn.execute("SELECT MAX(id) AS x FROM messages").fetchone()["x"])
    # On marque les 2 premiers comme résumés.
    repository.update_summary(u["id"], "résumé", ids[1], conn=conn)
    live = repository.live_messages(u["id"], ids[1], 50, conn=conn)
    assert [m["content"] for m in live] == ["m2", "m3", "m4"]


def _fake_settings(**overrides):
    from app.config import Settings

    defaults = dict(
        oauth_creds_path="x", oauth_seed_json="", model="test", telegram_bot_token="x",
        webhook_secret="s", public_url="", db_path=":memory:", tick_minutes=15,
        default_tz="Europe/Paris", checkin_grace_minutes=45, summary_keep_recent=20,
        summary_trigger=40, onboarding_idle_hours=20, onboarding_max_nudges=3,
        whisper_model="base", whisper_cache_dir="/tmp/w", whisper_language="fr",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_maybe_summarize_folds_old_messages(conn, monkeypatch):
    tuned = _fake_settings(summary_keep_recent=2, summary_trigger=4)
    monkeypatch.setattr(coach, "get_settings", lambda: tuned)
    # Évite tout appel réseau : résumé factice.
    monkeypatch.setattr(
        agent, "summarize_conversation", lambda old, msgs, **k: f"RESUME({len(msgs)})"
    )

    u = repository.get_or_create_user(1, conn=conn)
    for i in range(6):  # 6 messages vivants > trigger(4)
        repository.add_message(u["id"], "user", f"m{i}", conn=conn)

    coach._maybe_summarize(conn, u["id"])
    refreshed = repository.get_user(u["id"], conn=conn)
    # On garde les 2 plus récents -> 4 pliés dans le résumé.
    assert refreshed["summary"] == "RESUME(4)"
    live = repository.live_messages(u["id"], refreshed["summary_through_id"], 50, conn=conn)
    assert len(live) == 2
