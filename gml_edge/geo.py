"""Géométrie embarquée du moteur ZFE : Bounding Box, Ray Casting et distance à la frontière.

Convention : les sommets sont des tuples (lat, lon) en degrés décimaux. Pour le Ray Casting,
la longitude joue le rôle de l'axe x et la latitude celui de l'axe y.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

RAYON_TERRE_M = 6_371_000.0
METRES_PAR_DEGRE_LAT = math.pi * RAYON_TERRE_M / 180.0  # ≈ 111 195 m


class ErreurDonnees(Exception):
    """Fichier d'entrée inexploitable : le moteur s'arrête proprement, sans trace d'erreur Python."""


class EtatZone(str, Enum):
    HORS_ZONE = "HORS_ZONE"
    APPROCHE = "APPROCHE"  # hors de la ZFE mais à moins de `marge_pre_alerte_m` de sa frontière
    DANS_ZFE = "DANS_ZFE"


@dataclass(frozen=True)
class BoiteEnglobante:
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

    @classmethod
    def depuis_sommets(cls, sommets: list[tuple[float, float]]) -> BoiteEnglobante:
        lats = [lat for lat, _ in sommets]
        lons = [lon for _, lon in sommets]
        return cls(min(lats), max(lats), min(lons), max(lons))

    def contient(self, lat: float, lon: float) -> bool:
        # Quatre comparaisons : c'est tout le coût d'un point éloigné de la ZFE.
        return self.lat_min <= lat <= self.lat_max and self.lon_min <= lon <= self.lon_max

    def elargie(self, marge_m: float) -> BoiteEnglobante:
        """Boîte agrandie de `marge_m` mètres de chaque côté (pour la pré-alerte)."""
        d_lat = marge_m / METRES_PAR_DEGRE_LAT
        lat_centre = math.radians((self.lat_min + self.lat_max) / 2)
        d_lon = marge_m / (METRES_PAR_DEGRE_LAT * math.cos(lat_centre))
        return BoiteEnglobante(
            self.lat_min - d_lat, self.lat_max + d_lat, self.lon_min - d_lon, self.lon_max + d_lon
        )


def point_dans_polygone(lat: float, lon: float, sommets: list[tuple[float, float]]) -> bool:
    """Ray Casting (règle pair-impair) : on lance un rayon horizontal vers l'est depuis le point
    et on compte les arêtes qu'il traverse. Nombre impair = dedans.

    Une seule boucle sur les arêtes : O(n) en temps, O(1) en mémoire.
    """
    dedans = False
    j = len(sommets) - 1
    for i in range(len(sommets)):
        lat_i, lon_i = sommets[i]
        lat_j, lon_j = sommets[j]
        # L'arête enjambe-t-elle la latitude du point ? (le test strict évite de compter deux fois un sommet)
        if (lat_i > lat) != (lat_j > lat):
            lon_croisement = lon_i + (lat - lat_i) * (lon_j - lon_i) / (lat_j - lat_i)
            if lon < lon_croisement:
                dedans = not dedans
        j = i
    return dedans


def distance_frontiere_m(lat: float, lon: float, sommets: list[tuple[float, float]]) -> float:
    """Distance en mètres entre le point et l'arête la plus proche du polygone.

    Projection équirectangulaire locale centrée sur le point : l'erreur reste inférieure
    au mètre à l'échelle d'une agglomération, bien en dessous de la précision GPS.
    """
    cos_lat = math.cos(math.radians(lat))

    def vers_metres(s_lat: float, s_lon: float) -> tuple[float, float]:
        return (
            (s_lon - lon) * METRES_PAR_DEGRE_LAT * cos_lat,
            (s_lat - lat) * METRES_PAR_DEGRE_LAT,
        )

    distance_min = math.inf
    ax, ay = vers_metres(*sommets[-1])
    for sommet in sommets:
        bx, by = vers_metres(*sommet)
        # Le point est l'origine du repère : on cherche le point de [A, B] le plus proche de (0, 0).
        dx, dy = bx - ax, by - ay
        longueur2 = dx * dx + dy * dy
        t = 0.0 if longueur2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / longueur2))
        distance_min = min(distance_min, math.hypot(ax + t * dx, ay + t * dy))
        ax, ay = bx, by
    return distance_min


@dataclass
class Classification:
    etat: EtatZone
    distance_frontiere_m: float | None  # None quand la Bounding Box a suffi à conclure
    rejet_boite: bool


class ZoneZFE:
    """Polygone ZFE prêt à l'emploi : les boîtes englobantes sont calculées une seule fois au chargement."""

    def __init__(
        self,
        nom: str,
        sommets: list[tuple[float, float]],
        marge_pre_alerte_m: float = 500.0,
        tolerance_frontiere_m: float = 10.0,
    ) -> None:
        if sommets and sommets[0] == sommets[-1]:
            sommets = sommets[:-1]  # le sommet de fermeture est implicite pour le Ray Casting
        if len(set(sommets)) < 3:
            raise ErreurDonnees(f"Le polygone « {nom} » doit compter au moins 3 sommets distincts.")
        self.nom = nom
        self.sommets = sommets
        self.marge_pre_alerte_m = marge_pre_alerte_m
        self.tolerance_frontiere_m = tolerance_frontiere_m
        self.boite = BoiteEnglobante.depuis_sommets(sommets)
        self.boite_pre_alerte = self.boite.elargie(max(marge_pre_alerte_m, tolerance_frontiere_m))
        self.compteurs = {"points": 0, "rejets_boite": 0, "ray_casting": 0}

    def classer(self, lat: float, lon: float) -> Classification:
        self.compteurs["points"] += 1

        # Étape 1 — Bounding Box élargie : la grande majorité des points d'une tournée est écartée ici en O(1).
        if not self.boite_pre_alerte.contient(lat, lon):
            self.compteurs["rejets_boite"] += 1
            return Classification(EtatZone.HORS_ZONE, None, rejet_boite=True)

        # Étape 2 — Ray Casting, seulement si le point est dans la boîte stricte (sinon il est forcément dehors).
        if self.boite.contient(lat, lon):
            self.compteurs["ray_casting"] += 1
            if point_dans_polygone(lat, lon, self.sommets):
                return Classification(EtatZone.DANS_ZFE, 0.0, rejet_boite=False)

        # Étape 3 — Le point est dehors mais proche : distance à la frontière pour la pré-alerte.
        distance = distance_frontiere_m(lat, lon, self.sommets)
        if distance <= self.tolerance_frontiere_m:
            # Incertitude GPS : à quelques mètres de la frontière, on applique le principe de précaution.
            etat = EtatZone.DANS_ZFE
        elif distance <= self.marge_pre_alerte_m:
            etat = EtatZone.APPROCHE
        else:
            etat = EtatZone.HORS_ZONE
        return Classification(etat, distance, rejet_boite=False)
