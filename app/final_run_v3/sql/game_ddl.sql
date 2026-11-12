-- GameComponent persisted entity: games (SERIAL-equivalent via AUTOINCREMENT).
CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    score INTEGER NOT NULL DEFAULT 0,
    game_state TEXT NOT NULL DEFAULT 'Playing'
);
