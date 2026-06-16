-- Résumé glissant de la conversation : mémoire moyen/long terme sans renvoyer tout l'historique.
ALTER TABLE users ADD COLUMN summary TEXT NOT NULL DEFAULT '';
-- id du dernier message déjà intégré au résumé (les messages d'id supérieur sont envoyés verbatim).
ALTER TABLE users ADD COLUMN summary_through_id INTEGER NOT NULL DEFAULT 0;
