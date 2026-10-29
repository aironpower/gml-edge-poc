"""Module 2 — Moteur Safety (Physics) : isoler les conduites dangereuses dans le bruit de l'accéléromètre.

Chaîne de traitement, en un seul passage et en mémoire constante (O(N) temps, O(1) mémoire) :
  1. lecture tolérante du CSV (les lignes corrompues sont écartées et comptées, jamais fatales) ;
  2. filtre passe-bas du 1er ordre, réglé en hertz et non en nombre d'échantillons ;
  3. seuil avec hystérésis (déclenchement à 2,5 m/s², relâche à 1,5 m/s²) ;
  4. durée minimale : un pic plus court que 0,3 s est un choc de chaussée, pas un freinage.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .geo import ErreurDonnees
from .journal import affichable
from .stockage import JournalLocal

G = 9.80665
LIMITE_PHYSIQUE_MS2 = 4 * G  # au-delà : saturation du capteur ou choc sur la tablette, pas la conduite
COLONNES = ("timestamp", "acc_x", "acc_y", "acc_z")
REJETS_DETAILLES_MAX = 10  # à 100 Hz, un capteur défaillant inonderait le journal


@dataclass(frozen=True)
class Echantillon:
    horodatage: float  # secondes Unix (décimales acceptées pour 100 Hz)
    ax: float
    ay: float
    az: float


@dataclass(frozen=True)
class RegleDetection:
    type: str
    libelle: str
    axe: str  # "x" (latéral) ou "y" (longitudinal)
    sens: int  # -1 : valeurs négatives (freinage), +1 : positives, 0 : valeur absolue (virage)
    seuil_ms2: float
    relache_ms2: float
    seuil_critique_ms2: float

    def intensite(self, ax: float, ay: float) -> float:
        valeur = ax if self.axe == "x" else ay
        return abs(valeur) if self.sens == 0 else valeur * self.sens


REGLES = (
    # Exigée par l'énoncé : décélération supérieure à 2,5 m/s² sur l'axe Y (acc_y < -2,5).
    RegleDetection("FREINAGE_VIOLENT", "Freinage violent", "y", -1, 2.5, 1.5, 4.0),
    # Preuves de comportement complémentaires demandées par l'assureur (« freinages, virages »).
    RegleDetection("ACCELERATION_BRUSQUE", "Accélération brusque", "y", +1, 2.5, 1.5, 4.0),
    RegleDetection("VIRAGE_BRUSQUE", "Virage brusque", "x", 0, 3.0, 2.0, 4.5),
)
PENALITES = {"ELEVE": 10, "CRITIQUE": 20}
SEUIL_ELIGIBILITE_BONUS = 80


@dataclass
class RapportLecture:
    lignes_lues: int = 0
    lignes_valides: int = 0
    motifs_rejet: Counter = field(default_factory=Counter)

    @property
    def lignes_rejetees(self) -> int:
        return sum(self.motifs_rejet.values())


def lire_accelerometre(chemin: Path, journal: logging.Logger, rapport: RapportLecture) -> Iterator[Echantillon]:
    """Lit le CSV ligne à ligne (jamais en entier en mémoire) et ne produit que des échantillons valides."""
    try:
        # errors="replace" : un octet corrompu invalide sa ligne, pas tout le fichier.
        fichier = open(chemin, encoding="utf-8-sig", errors="replace", newline="")
    except FileNotFoundError:
        raise ErreurDonnees(f"Fichier introuvable : {affichable(chemin)}") from None
    except OSError as erreur:
        raise ErreurDonnees(f"Lecture impossible de {affichable(chemin)} : {erreur}") from None

    with fichier:
        lecteur = csv.reader(fichier)
        entete = next(lecteur, None)
        if entete is None:
            raise ErreurDonnees(f"{affichable(chemin)} est vide.")
        noms = [nom.strip().lower() for nom in entete]
        manquantes = [nom for nom in COLONNES if nom not in noms]
        if manquantes:
            raise ErreurDonnees(f"Colonnes manquantes dans {affichable(chemin)} : {', '.join(manquantes)} (attendu : {', '.join(COLONNES)}).")
        indices = [noms.index(nom) for nom in COLONNES]

        def rejeter(numero: int, motif: str, detail: str) -> None:
            rapport.motifs_rejet[motif] += 1
            if rapport.lignes_rejetees <= REJETS_DETAILLES_MAX:
                journal.warning("[CSV] Ligne %d ignorée : %s (%s).", numero, motif, detail)
            elif rapport.lignes_rejetees == REJETS_DETAILLES_MAX + 1:
                journal.warning("[CSV] Rejets suivants comptés sans détail (voir qualite_donnees).")

        dernier_horodatage = -math.inf
        numero = 1
        while True:
            try:
                ligne = next(lecteur)
            except StopIteration:
                break
            except csv.Error as erreur:
                numero += 1
                rapport.lignes_lues += 1
                rejeter(numero, "ligne CSV malformée", str(erreur))
                continue
            numero += 1
            if not any(cellule.strip() for cellule in ligne):
                continue  # ligne vide : rien à signaler
            rapport.lignes_lues += 1
            if len(ligne) < len(noms):
                rejeter(numero, "colonnes manquantes", ",".join(ligne))
                continue
            try:
                t, ax, ay, az = (float(ligne[i]) for i in indices)
            except ValueError:
                rejeter(numero, "valeur non numérique", ",".join(ligne))
                continue
            if not all(math.isfinite(v) for v in (t, ax, ay, az)):
                rejeter(numero, "valeur non finie", ",".join(ligne))
                continue
            if t <= dernier_horodatage:
                rejeter(numero, "horodatage non croissant", f"{t:.2f} après {dernier_horodatage:.2f}")
                continue
            if max(abs(ax), abs(ay), abs(az)) > LIMITE_PHYSIQUE_MS2:
                rejeter(numero, "valeur hors plage physique (> 4 g)", ",".join(ligne))
                continue
            dernier_horodatage = t
            rapport.lignes_valides += 1
            yield Echantillon(t, ax, ay, az)


class FiltrePasseBas:
    """Filtre RC numérique du 1er ordre. Le coefficient est recalculé à chaque pas à partir de l'intervalle
    réel entre deux mesures : le même réglage fonctionne à 1 Hz (jeu de l'énoncé) et à 100 Hz (tablette)."""

    def __init__(self, frequence_coupure_hz: float) -> None:
        self.constante_temps_s = 1.0 / (2 * math.pi * frequence_coupure_hz)
        self.valeur: float | None = None

    def filtrer(self, mesure: float, dt: float) -> float:
        if self.valeur is None:
            self.valeur = mesure
        else:
            alpha = dt / (self.constante_temps_s + dt)
            self.valeur += alpha * (mesure - self.valeur)
        return self.valeur


@dataclass
class _EvenementEnCours:
    debut: float
    fin: float
    pic: float
    pic_brut: float
    instant_pic: float


class AnalyseurConduite:
    def __init__(
        self,
        journal: logging.Logger,
        regles: Iterable[RegleDetection] = REGLES,
        frequence_coupure_hz: float = 5.0,
        duree_min_s: float = 0.3,
    ) -> None:
        self.journal = journal
        self.regles = tuple(regles)
        self.frequence_coupure_hz = frequence_coupure_hz
        self.duree_min_s = duree_min_s
        self._filtres = {"x": FiltrePasseBas(frequence_coupure_hz), "y": FiltrePasseBas(frequence_coupure_hz)}
        self._en_cours: dict[str, _EvenementEnCours] = {}
        self._precedent: float | None = None
        self._periode = 0.0
        self.premier: float | None = None
        self.evenements: list[dict] = []
        self.pics_brefs_ignores = 0

    def ajouter(self, e: Echantillon) -> None:
        if self.premier is None:
            self.premier = e.horodatage
        dt = 0.0 if self._precedent is None else e.horodatage - self._precedent
        if dt > 0:
            self._periode = dt
        self._precedent = e.horodatage
        ax = self._filtres["x"].filtrer(e.ax, dt)
        ay = self._filtres["y"].filtrer(e.ay, dt)

        for regle in self.regles:
            intensite = regle.intensite(ax, ay)
            en_cours = self._en_cours.get(regle.type)
            if en_cours is None:
                if intensite > regle.seuil_ms2:
                    brut = regle.intensite(e.ax, e.ay)
                    self._en_cours[regle.type] = _EvenementEnCours(e.horodatage, e.horodatage, intensite, brut, e.horodatage)
            elif intensite >= regle.relache_ms2:
                en_cours.fin = e.horodatage
                en_cours.pic_brut = max(en_cours.pic_brut, regle.intensite(e.ax, e.ay))
                if intensite > en_cours.pic:
                    en_cours.pic, en_cours.instant_pic = intensite, e.horodatage
            else:
                self._clore(regle)

    def terminer(self) -> None:
        for regle in self.regles:
            if regle.type in self._en_cours:
                self._clore(regle)

    @property
    def duree_analysee_s(self) -> float:
        if self.premier is None or self._precedent is None:
            return 0.0
        return self._precedent - self.premier + self._periode

    def _clore(self, regle: RegleDetection) -> None:
        en_cours = self._en_cours.pop(regle.type)
        # Chaque échantillon représente une période d'acquisition : un seul point à 1 Hz couvre 1 s.
        duree = en_cours.fin - en_cours.debut + self._periode
        if duree < self.duree_min_s:
            self.pics_brefs_ignores += 1
            self.journal.debug(
                "[SAFETY] Pic bref ignoré (%s, %.2f s < %.2f s) : choc de chaussée.", regle.libelle, duree, self.duree_min_s
            )
            return
        severite = "CRITIQUE" if en_cours.pic >= regle.seuil_critique_ms2 else "ELEVE"
        signe = -1 if regle.sens < 0 else 1
        evenement = {
            "type": regle.type,
            "libelle": regle.libelle,
            "horodatage": en_cours.instant_pic,
            "heure_utc": _iso(en_cours.instant_pic),
            "duree_s": round(duree, 2),
            "axe": f"acc_{regle.axe}",
            "pic_filtre_ms2": round(signe * en_cours.pic, 2),
            "pic_brut_ms2": round(signe * en_cours.pic_brut, 2),
            "pic_g": round(en_cours.pic_brut / G, 3),
            "severite": severite,
            "penalite": PENALITES[severite],
        }
        self.evenements.append(evenement)
        self.journal.warning(
            "[SAFETY] %s détecté à %s : pic %+.2f m/s² (filtré %+.2f, %.2f g), durée %.2f s, sévérité %s → -%d pts.",
            regle.libelle, evenement["heure_utc"], evenement["pic_brut_ms2"], evenement["pic_filtre_ms2"],
            evenement["pic_g"], duree, severite, evenement["penalite"],
        )


def construire_score(analyseur: AnalyseurConduite, rapport: RapportLecture, source: str, vehicule_id: str) -> dict:
    """Format `gml.daily_score/1.0` : uniquement des agrégats et des événements datés, jamais le flux brut."""
    evenements = analyseur.evenements
    donnees_exploitables = rapport.lignes_valides > 0
    score = max(0, 100 - sum(e["penalite"] for e in evenements)) if donnees_exploitables else None
    nombre = Counter(e["type"] for e in evenements)
    return {
        "format": "gml.daily_score/1.0",
        "vehicule_id": vehicule_id,
        "date": _iso(analyseur.premier)[:10] if analyseur.premier is not None else None,
        "genere_le": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "periode": {
            "debut_utc": _iso(analyseur.premier) if analyseur.premier is not None else None,
            "duree_analysee_s": round(analyseur.duree_analysee_s, 2),
        },
        "parametres": {
            "unite": "m/s²",
            "frequence_coupure_filtre_hz": analyseur.frequence_coupure_hz,
            "duree_min_evenement_s": analyseur.duree_min_s,
            "regles": [
                {"type": r.type, "axe": f"acc_{r.axe}", "seuil_ms2": r.seuil_ms2, "relache_ms2": r.relache_ms2, "seuil_critique_ms2": r.seuil_critique_ms2}
                for r in analyseur.regles
            ],
            "penalites": PENALITES,
            "seuil_eligibilite_bonus": SEUIL_ELIGIBILITE_BONUS,
        },
        "qualite_donnees": {
            "lignes_lues": rapport.lignes_lues,
            "lignes_valides": rapport.lignes_valides,
            "lignes_rejetees": rapport.lignes_rejetees,
            "motifs_rejet": dict(rapport.motifs_rejet),
            "pics_brefs_ignores": analyseur.pics_brefs_ignores,
        },
        "synthese": {
            "score_securite": score,
            "niveau": _niveau(score),
            "eligible_bonus_assurance": score is not None and score >= SEUIL_ELIGIBILITE_BONUS,
            "freinages_violents": nombre["FREINAGE_VIOLENT"],
            "accelerations_brusques": nombre["ACCELERATION_BRUSQUE"],
            "virages_brusques": nombre["VIRAGE_BRUSQUE"],
        },
        "evenements": evenements,
    }


def ecrire_json_atomique(chemin: Path, contenu: dict) -> None:
    """Écrit dans un fichier temporaire puis renomme : une coupure de courant ne laisse jamais un JSON tronqué."""
    chemin.parent.mkdir(parents=True, exist_ok=True)
    temporaire = chemin.with_suffix(chemin.suffix + ".tmp")
    with open(temporaire, "w", encoding="utf-8") as fichier:
        json.dump(contenu, fichier, ensure_ascii=False, indent=2)
        fichier.write("\n")
    os.replace(temporaire, chemin)


def executer_safety(
    chemin_csv: Path,
    chemin_sortie: Path,
    journal: logging.Logger,
    stockage: JournalLocal | None = None,
    vehicule_id: str = "GML-TRUCK-001",
    frequence_coupure_hz: float = 5.0,
    duree_min_s: float = 0.3,
) -> dict:
    rapport = RapportLecture()
    analyseur = AnalyseurConduite(journal, frequence_coupure_hz=frequence_coupure_hz, duree_min_s=duree_min_s)
    journal.info("Analyse de %s (filtre passe-bas %.1f Hz, durée minimale %.2f s).", affichable(chemin_csv), frequence_coupure_hz, duree_min_s)

    for echantillon in lire_accelerometre(chemin_csv, journal, rapport):
        analyseur.ajouter(echantillon)
    analyseur.terminer()

    score = construire_score(analyseur, rapport, Path(chemin_csv).name, vehicule_id)
    if rapport.lignes_valides == 0:
        journal.error("[SAFETY] Aucune mesure exploitable : score non calculé (le fichier est tout de même produit).")
    ecrire_json_atomique(chemin_sortie, score)

    if stockage is not None:
        for evenement in analyseur.evenements:
            stockage.enregistrer_evenement("SAFETY", evenement["type"], evenement["heure_utc"], evenement)
        if score["date"]:
            stockage.enregistrer_score(score["date"], vehicule_id, score)
    return score


def _niveau(score: int | None) -> str | None:
    if score is None:
        return None
    for seuil, niveau in ((90, "A"), (75, "B"), (60, "C")):
        if score >= seuil:
            return niveau
    return "D"


def _iso(horodatage: float) -> str:
    return datetime.fromtimestamp(horodatage, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
