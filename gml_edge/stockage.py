"""Journal local SQLite : stocker hors-ligne, transmettre plus tard par lots compressés (store-and-forward).

Seuls les événements et les scores agrégés sont conservés : le flux brut à 100 Hz ne quitte jamais la tablette.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS evenements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    module      TEXT    NOT NULL,              -- ZFE ou SAFETY
    type        TEXT    NOT NULL,              -- ex. ALERT_ZFE, FREINAGE_VIOLENT
    horodatage  TEXT    NOT NULL,              -- instant de l'événement (source capteur)
    donnees     TEXT    NOT NULL,              -- détail JSON
    cree_le     TEXT    NOT NULL,
    transmis    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_evenements_transmis ON evenements (transmis);
CREATE TABLE IF NOT EXISTS scores_journaliers (
    date        TEXT NOT NULL,
    vehicule_id TEXT NOT NULL,
    score       TEXT NOT NULL,                 -- contenu de daily_score.json
    transmis    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (date, vehicule_id)
);
"""


@dataclass
class Lot:
    ids_evenements: list[int]
    cles_scores: list[tuple[str, str]]
    octets_json: int
    octets_gzip: int
    contenu_gzip: bytes

    @property
    def vide(self) -> bool:
        return not self.ids_evenements and not self.cles_scores


class JournalLocal:
    def __init__(self, chemin: Path | str) -> None:
        if chemin != ":memory:":
            Path(chemin).parent.mkdir(parents=True, exist_ok=True)
        self._connexion = sqlite3.connect(chemin)
        # WAL : une coupure d'alimentation du camion ne corrompt pas la base.
        self._connexion.execute("PRAGMA journal_mode=WAL")
        self._connexion.executescript(_SCHEMA)

    def __enter__(self) -> JournalLocal:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.fermer()

    def fermer(self) -> None:
        self._connexion.close()

    def enregistrer_evenement(self, module: str, type_evenement: str, horodatage: str, donnees: dict) -> None:
        with self._connexion:
            self._connexion.execute(
                "INSERT INTO evenements (module, type, horodatage, donnees, cree_le) VALUES (?, ?, ?, ?, ?)",
                (module, type_evenement, horodatage, json.dumps(donnees, ensure_ascii=False), _maintenant()),
            )

    def enregistrer_score(self, date: str, vehicule_id: str, score: dict) -> None:
        with self._connexion:
            self._connexion.execute(
                "INSERT INTO scores_journaliers (date, vehicule_id, score) VALUES (?, ?, ?) "
                "ON CONFLICT (date, vehicule_id) DO UPDATE SET score = excluded.score, transmis = 0",
                (date, vehicule_id, json.dumps(score, ensure_ascii=False)),
            )

    def preparer_lot(self, vehicule_id: str) -> Lot:
        evenements = self._connexion.execute(
            "SELECT id, module, type, horodatage, donnees FROM evenements WHERE transmis = 0 ORDER BY id"
        ).fetchall()
        scores = self._connexion.execute(
            "SELECT date, vehicule_id, score FROM scores_journaliers WHERE transmis = 0 ORDER BY date"
        ).fetchall()
        charge = {
            "vehicule_id": vehicule_id,
            "genere_le": _maintenant(),
            "evenements": [
                {"module": m, "type": t, "horodatage": h, "donnees": json.loads(d)} for _, m, t, h, d in evenements
            ],
            "scores_journaliers": [json.loads(s) for _, _, s in scores],
        }
        brut = json.dumps(charge, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        compresse = gzip.compress(brut)
        return Lot(
            ids_evenements=[ligne[0] for ligne in evenements],
            cles_scores=[(d, v) for d, v, _ in scores],
            octets_json=len(brut),
            octets_gzip=len(compresse),
            contenu_gzip=compresse,
        )

    def marquer_transmis(self, lot: Lot) -> None:
        with self._connexion:
            self._connexion.executemany("UPDATE evenements SET transmis = 1 WHERE id = ?", [(i,) for i in lot.ids_evenements])
            self._connexion.executemany(
                "UPDATE scores_journaliers SET transmis = 1 WHERE date = ? AND vehicule_id = ?", lot.cles_scores
            )

    def compter_en_attente(self) -> int:
        (nombre,) = self._connexion.execute("SELECT COUNT(*) FROM evenements WHERE transmis = 0").fetchone()
        return nombre


def _maintenant() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
