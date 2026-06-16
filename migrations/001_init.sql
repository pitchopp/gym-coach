-- Schéma initial du coach. Conçu multi-utilisateurs ; 1 chat Telegram = 1 utilisateur.

CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_chat_id    INTEGER NOT NULL UNIQUE,
    name                TEXT,
    timezone            TEXT NOT NULL DEFAULT 'Europe/Paris',
    -- JSON libre : {"tone": "...", "quiet_hours": {"start": "22:00", "end": "08:00"}}
    comm_prefs          TEXT NOT NULL DEFAULT '{}',
    training_frequency  TEXT,                          -- ex: "4x/semaine"
    onboarding_done     INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Séances récurrentes attendues (créneaux hebdomadaires).
CREATE TABLE IF NOT EXISTS schedule_slots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    weekday     INTEGER NOT NULL,                      -- 0=lundi .. 6=dimanche
    time        TEXT NOT NULL,                         -- "HH:MM"
    activity    TEXT,                                  -- ex: "jambes", "cardio"
    active      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_slots_user ON schedule_slots(user_id, active);

-- Une ligne = une séance attendue à une date donnée. Cœur de l'anti-spam et du report.
CREATE TABLE IF NOT EXISTS checkins (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    slot_id         INTEGER REFERENCES schedule_slots(id) ON DELETE SET NULL,
    due_date        TEXT NOT NULL,                     -- "YYYY-MM-DD" (tz utilisateur)
    due_time        TEXT NOT NULL,                     -- "HH:MM"
    activity        TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',   -- pending|asked|done|skipped|rescheduled
    asked_at        TEXT,
    responded_at    TEXT,
    reschedule_to   TEXT,                              -- date de report éventuelle
    note            TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Un seul check-in par (user, date, slot) -> matérialisation idempotente.
CREATE UNIQUE INDEX IF NOT EXISTS idx_checkin_unique
    ON checkins(user_id, due_date, slot_id);
CREATE INDEX IF NOT EXISTS idx_checkin_status ON checkins(user_id, status, due_date);

CREATE TABLE IF NOT EXISTS programs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content     TEXT NOT NULL,                         -- markdown
    version     INTEGER NOT NULL DEFAULT 1,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_program_user ON programs(user_id, active);

-- Historique de conversation (on charge les N derniers messages comme contexte).
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,                         -- user|assistant
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, id);

-- Mémoire libre clé/valeur ("blessure genou", "objectif prise de masse"...).
CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, key)
);
