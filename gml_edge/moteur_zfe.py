"""Module 1 — Moteur ZFE (Geo) : rejoue une trace GPS contre le polygone ZFE, sans aucun accès réseau."""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

from .geo import Classification, ErreurDonnees, EtatZone, ZoneZFE
from .journal import affichable
from .stockage import JournalLocal


@dataclass
class PointGPS:
    id: object
    horodatage: str
    lat: float
    lon: float
    attendu: str | None = None


@dataclass
class ResumeZFE:
    points_lus: int = 0
    points_traites: int = 0
    points_ignores: int = 0
    alertes: list[dict] = field(default_factory=list)
    pre_alertes: list[dict] = field(default_factory=list)
    verifications: list[dict] = field(default_factory=list)
    compteurs_zone: dict = field(default_factory=dict)
    duree_calcul_max_us: float = 0.0


def charger_json(chemin: Path) -> object:
    try:
        with open(chemin, encoding="utf-8") as fichier:
            return json.load(fichier)
    except FileNotFoundError:
        raise ErreurDonnees(f"Fichier introuvable : {affichable(chemin)}") from None
    except (json.JSONDecodeError, UnicodeDecodeError) as erreur:
        raise ErreurDonnees(f"JSON invalide dans {affichable(chemin)} : {erreur}") from None
    except OSError as erreur:
        raise ErreurDonnees(f"Lecture impossible de {affichable(chemin)} : {erreur}") from None


def _coordonnee(valeur: object, nom: str, borne: float) -> float:
    # bool est un sous-type de int en Python : on l'exclut explicitement.
    if isinstance(valeur, bool) or not isinstance(valeur, (int, float)):
        raise ValueError(f"{nom} absente ou non numérique ({valeur!r})")
    valeur = float(valeur)
    if not math.isfinite(valeur) or abs(valeur) > borne:
        raise ValueError(f"{nom} hors plage ({valeur})")
    return valeur


def _lire_lat_lon(entree: object) -> tuple[float, float]:
    if not isinstance(entree, dict):
        raise ValueError(f"entrée inattendue ({entree!r})")
    return _coordonnee(entree.get("lat"), "latitude", 90), _coordonnee(entree.get("lon"), "longitude", 180)


def charger_polygone(chemin: Path, nom: str = "Lyon", **parametres: float) -> ZoneZFE:
    donnees = charger_json(chemin)
    if not isinstance(donnees, list):
        raise ErreurDonnees(f"{affichable(chemin)} doit contenir une liste de sommets {{lat, lon}}.")
    sommets = []
    for rang, entree in enumerate(donnees, start=1):
        try:
            sommets.append(_lire_lat_lon(entree))
        except ValueError as erreur:
            # Un sommet faux déformerait la zone : on refuse le polygone plutôt que de deviner.
            raise ErreurDonnees(f"Sommet n°{rang} invalide dans {affichable(chemin)} : {erreur}") from None
    return ZoneZFE(nom, sommets, **parametres)


def _secondes(horodatage: str) -> float | None:
    try:
        h, m, s = (float(partie) for partie in horodatage.split(":"))
        return h * 3600 + m * 60 + s
    except (AttributeError, ValueError):
        return None


class SurveillantZFE:
    """Machine à états HORS_ZONE → APPROCHE → DANS_ZFE. Une alerte n'est émise qu'au changement d'état :
    le chauffeur n'est pas harcelé tant qu'il reste dans la même situation."""

    def __init__(self, zone: ZoneZFE, journal: logging.Logger, stockage: JournalLocal | None = None) -> None:
        self.zone = zone
        self.journal = journal
        self.stockage = stockage
        self.etat = EtatZone.HORS_ZONE
        self.resume = ResumeZFE()

    def traiter(self, point: PointGPS) -> Classification:
        debut = time.perf_counter()
        classement = self.zone.classer(point.lat, point.lon)
        duree_us = (time.perf_counter() - debut) * 1e6
        self.resume.points_traites += 1
        self.resume.duree_calcul_max_us = max(self.resume.duree_calcul_max_us, duree_us)

        if classement.rejet_boite:
            methode = "Bounding Box : rejet immédiat"
        elif classement.etat is EtatZone.DANS_ZFE and classement.distance_frontiere_m == 0.0:
            methode = "Ray Casting : dedans"
        else:
            methode = f"frontière à {classement.distance_frontiere_m:.0f} m"
        self.journal.info(
            "#%-3s %s (%.5f, %.5f) -> %-9s [%s] %.0f µs",
            point.id, point.horodatage, point.lat, point.lon, classement.etat.value, methode, duree_us,
        )

        self._signaler_transition(point, classement)
        self.etat = classement.etat
        self._verifier_attendu(point, classement)
        return classement

    def _signaler_transition(self, point: PointGPS, classement: Classification) -> None:
        nouvel_etat = classement.etat
        if nouvel_etat is self.etat:
            return
        detail = {"point": point.id, "horodatage": point.horodatage, "lat": point.lat, "lon": point.lon, "zone": self.zone.nom}

        if nouvel_etat is EtatZone.DANS_ZFE:
            self.journal.critical(
                "[ALERT ZFE] Entrée dans la ZFE « %s » — point #%s à %s (%.5f, %.5f). "
                "Alerte HUD + vocale déclenchée localement, sans réseau.",
                self.zone.nom, point.id, point.horodatage, point.lat, point.lon,
            )
            self.resume.alertes.append(detail)
            self._stocker("ALERT_ZFE", point, detail)
        elif nouvel_etat is EtatZone.APPROCHE and self.etat is EtatZone.HORS_ZONE:
            detail["distance_m"] = round(classement.distance_frontiere_m or 0.0)
            self.journal.warning(
                "[PRE-ALERT ZFE] ZFE « %s » à %d m — point #%s à %s. Préparer l'itinéraire de contournement.",
                self.zone.nom, detail["distance_m"], point.id, point.horodatage,
            )
            self.resume.pre_alertes.append(detail)
            self._stocker("PRE_ALERT_ZFE", point, detail)
        elif self.etat is EtatZone.DANS_ZFE:
            self.journal.info("[INFO ZFE] Sortie de la ZFE « %s » — point #%s à %s.", self.zone.nom, point.id, point.horodatage)
            self._stocker("SORTIE_ZFE", point, detail)

    def _stocker(self, type_evenement: str, point: PointGPS, detail: dict) -> None:
        if self.stockage is not None:
            self.stockage.enregistrer_evenement("ZFE", type_evenement, point.horodatage, detail)

    def _verifier_attendu(self, point: PointGPS, classement: Classification) -> None:
        if not point.attendu:
            return
        attendu_dedans = point.attendu.strip().upper().startswith("IN")
        obtenu_dedans = classement.etat is EtatZone.DANS_ZFE
        self.resume.verifications.append(
            {
                "point": point.id,
                "attendu": point.attendu,
                "obtenu": classement.etat.value,
                "distance_frontiere_m": None if classement.distance_frontiere_m is None else round(classement.distance_frontiere_m),
                "conforme": attendu_dedans == obtenu_dedans,
            }
        )


def executer_zfe(
    chemin_polygone: Path,
    chemin_trace: Path,
    journal: logging.Logger,
    stockage: JournalLocal | None = None,
    temps_reel: bool = False,
    acceleration: float = 5.0,
    marge_pre_alerte_m: float = 500.0,
    tolerance_frontiere_m: float = 10.0,
) -> ResumeZFE:
    zone = charger_polygone(chemin_polygone, marge_pre_alerte_m=marge_pre_alerte_m, tolerance_frontiere_m=tolerance_frontiere_m)
    b = zone.boite
    journal.info(
        "Polygone ZFE « %s » chargé : %d sommets, Bounding Box lat [%.4f ; %.4f] lon [%.4f ; %.4f], pré-alerte %d m.",
        zone.nom, len(zone.sommets), b.lat_min, b.lat_max, b.lon_min, b.lon_max, marge_pre_alerte_m,
    )

    trace = charger_json(chemin_trace)
    if not isinstance(trace, list):
        raise ErreurDonnees(f"{affichable(chemin_trace)} doit contenir une liste de points GPS.")
    journal.info("Trace GPS chargée : %d points. Mode %s.", len(trace), "temps réel" if temps_reel else "rapide")

    surveillant = SurveillantZFE(zone, journal, stockage)
    instant_precedent: float | None = None
    for rang, entree in enumerate(trace, start=1):
        surveillant.resume.points_lus += 1
        identifiant = entree.get("id", rang) if isinstance(entree, dict) else rang
        try:
            lat, lon = _lire_lat_lon(entree)
        except ValueError as erreur:
            # Fix GPS perdu (tunnel) ou trame corrompue : on garde le dernier état connu et on continue.
            surveillant.resume.points_ignores += 1
            journal.warning("[GPS] Point #%s ignoré (%s) — dernier état conservé : %s.", identifiant, erreur, surveillant.etat.value)
            continue

        horodatage = str(entree.get("timestamp", "--:--:--"))
        if temps_reel:
            instant = _secondes(horodatage)
            if instant is not None and instant_precedent is not None and instant > instant_precedent:
                time.sleep(min((instant - instant_precedent) / acceleration, 3.0))
            instant_precedent = instant if instant is not None else instant_precedent

        attendu = entree.get("expected")
        surveillant.traiter(PointGPS(identifiant, horodatage, lat, lon, attendu if isinstance(attendu, str) else None))

    resume = surveillant.resume
    resume.compteurs_zone = dict(zone.compteurs)
    return resume
