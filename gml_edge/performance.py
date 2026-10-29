"""Mesures de faisabilité sur matériel contraint (Zebra ET40 : Snapdragon 660, 150 Mo de RAM pour l'application)."""

from __future__ import annotations

import gzip
import json
import logging
import random
import time
import tracemalloc

from .geo import ZoneZFE, distance_frontiere_m, point_dans_polygone
from .safety import AnalyseurConduite, RapportLecture, construire_score
from .simulation import TrajetSimule

HEURES_CONDUITE_PAR_JOUR = 8
JOURS_OUVRES_PAR_MOIS = 22


def milliers(nombre: float) -> str:
    """Séparateur de milliers par une espace, comme l'exige l'énoncé : 1 000."""
    return f"{nombre:,.0f}".replace(",", " ")


def _journal_muet() -> logging.Logger:
    muet = logging.getLogger("gml_edge.muet")
    muet.propagate = False
    if not muet.handlers:
        muet.addHandler(logging.NullHandler())
    return muet


def densifier(sommets: list[tuple[float, float]], points_par_arete: int) -> list[tuple[float, float]]:
    """Ajoute des sommets sur chaque arête : simule un contour ZFE réel, bien plus détaillé que celui de l'énoncé."""
    resultat = []
    for (lat_a, lon_a), (lat_b, lon_b) in zip(sommets, sommets[1:] + sommets[:1]):
        for k in range(points_par_arete):
            f = k / points_par_arete
            resultat.append((lat_a + f * (lat_b - lat_a), lon_a + f * (lon_b - lon_a)))
    return resultat


def _microsecondes_par_point(fonction, points: list[tuple[float, float]]) -> float:
    debut = time.perf_counter()
    for lat, lon in points:
        fonction(lat, lon)
    return (time.perf_counter() - debut) / len(points) * 1e6


def mesurer_zfe(sommets: list[tuple[float, float]], journal: logging.Logger, nombre_points: int = 100_000) -> None:
    alea = random.Random(7)
    # Tournée régionale : un carré d'environ 60 km de côté centré sur Lyon.
    points = [(alea.uniform(45.50, 46.04), alea.uniform(4.45, 5.22)) for _ in range(nombre_points)]
    echantillon_lent = points[:2_000]  # la version sans Bounding Box est trop lente pour 100 000 points

    journal.info("ZFE — %s positions GPS aléatoires dans un carré de ~60 km autour de Lyon :", milliers(nombre_points))
    for libelle, contour in (("polygone de l'énoncé", sommets), ("contour détaillé", densifier(sommets, 400))):
        zone = ZoneZFE("Lyon", contour)

        def sans_boite(lat: float, lon: float) -> None:
            if not point_dans_polygone(lat, lon, contour):
                distance_frontiere_m(lat, lon, contour)

        avec = _microsecondes_par_point(zone.classer, points)
        sans = _microsecondes_par_point(sans_boite, echantillon_lent)
        rejets = zone.compteurs["rejets_boite"] / zone.compteurs["points"]
        journal.info(
            "  %-21s %5s sommets : %6.2f µs/point avec Bounding Box (%.1f %% écartés en O(1)), %7.2f µs sans (x%.0f).",
            libelle, milliers(len(contour)), avec, 100 * rejets, sans, sans / avec,
        )
    # Pire cas : chaque point exige Ray Casting + distance sur le contour détaillé. Le GPS livre 1 position/s.
    journal.info("  Pire cas à 1 position GPS/s : %.3f %% d'un cœur CPU de ce poste.", sans / 1e6 * 100)


def mesurer_safety(journal: logging.Logger, duree_s: float = 1200.0) -> None:
    trajet = TrajetSimule(duree_s=duree_s, freinages_violents=5, freinages_normaux=20, nids_de_poule=80)
    echantillons = list(trajet.echantillons())  # généré à l'avance : on ne chronomètre que le moteur

    analyseur = AnalyseurConduite(journal=_journal_muet())
    debut = time.perf_counter()
    for e in echantillons:
        analyseur.ajouter(e)
    analyseur.terminer()
    duree_calcul = time.perf_counter() - debut

    tracemalloc.start()
    analyseur_memoire = AnalyseurConduite(journal=_journal_muet())
    for e in echantillons:
        analyseur_memoire.ajouter(e)
    analyseur_memoire.terminer()
    _, pic_memoire = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    fenetres = [(v.debut, v.debut + v.duree_s) for v in trajet.freinages_violents]
    detectes = [e for e in analyseur.evenements if e["type"] == "FREINAGE_VIOLENT"]
    vrais = sum(1 for e in detectes if any(a <= e["horodatage"] <= b for a, b in fenetres))
    nids = sum(1 for v in trajet.evenements if v.type == "NID_DE_POULE")
    normaux = sum(1 for v in trajet.evenements if v.type == "FREINAGE_NORMAL")

    journal.info(
        "Safety — %d min de conduite simulée à 100 Hz (%s échantillons, %d nids-de-poule, %d freinages normaux) :",
        duree_s / 60, milliers(len(echantillons)), nids, normaux,
    )
    journal.info(
        "  Freinages violents cachés : %d ; détectés : %d dont %d au bon instant ; faux positifs : %d ; pics brefs écartés : %d.",
        len(fenetres), len(detectes), vrais, len(detectes) - vrais, analyseur.pics_brefs_ignores,
    )
    journal.info(
        "  %.2f s de calcul, soit %s fois plus vite que le temps réel ; mémoire du moteur : %.1f Ko, quelle que soit la durée.",
        duree_calcul, milliers(duree_s / duree_calcul), pic_memoire / 1024,
    )

    octets_csv = sum(len(f"{e.horodatage:.2f},{e.ax:.3f},{e.ay:.3f},{e.az:.3f}\n") for e in echantillons)
    rapport = RapportLecture(lignes_lues=len(echantillons), lignes_valides=len(echantillons))
    score = construire_score(analyseur, rapport, "simulation", "GML-TRUCK-001")
    octets_score = len(gzip.compress(json.dumps(score, separators=(",", ":")).encode("utf-8")))
    brut_jour = octets_csv / duree_s * 3600 * HEURES_CONDUITE_PAR_JOUR
    journal.info("Volume 4G (journée de %d h, %d jours par mois) :", HEURES_CONDUITE_PAR_JOUR, JOURS_OUVRES_PAR_MOIS)
    journal.info(
        "  flux brut 100 Hz : %s Mo/jour, %.1f Go/mois ; daily_score compressé : %.1f Ko/jour, %s Ko/mois (%s fois moins).",
        milliers(brut_jour / 1e6), brut_jour * JOURS_OUVRES_PAR_MOIS / 1e9, octets_score / 1024,
        milliers(octets_score * JOURS_OUVRES_PAR_MOIS / 1024), milliers(brut_jour / octets_score),
    )
