-- Compteur de relances d'onboarding consécutives sans réponse (remis à 0 quand l'utilisateur écrit).
ALTER TABLE users ADD COLUMN onboarding_nudges INTEGER NOT NULL DEFAULT 0;
