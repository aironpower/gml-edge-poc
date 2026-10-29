"""Signal accélérométrique synthétique à 100 Hz, avec une vérité terrain connue.

Le jeu de l'énoncé ne compte que 10 lignes à 1 Hz. Pour prouver que le filtrage tient à la cadence réelle
de la tablette, on génère un trajet bruité (vibrations moteur et chaussée, nids-de-poule, freinages normaux)
dans lequel on cache des freinages violents dont on connaît l'instant exact.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterator
from dataclasses import dataclass

from .safety import G, Echantillon


@dataclass(frozen=True)
class EvenementSimule:
    type: str  # FREINAGE_VIOLENT, FREINAGE_NORMAL ou NID_DE_POULE
    debut: float
    duree_s: float
    amplitude_ms2: float

    def contribution(self, t: float) -> tuple[float, float]:
        """Accélération ajoutée (axe y, axe z) à l'instant t."""
        if not self.debut <= t < self.debut + self.duree_s:
            return 0.0, 0.0
        forme = math.sin(math.pi * (t - self.debut) / self.duree_s)  # demi-sinusoïde
        if self.type == "NID_DE_POULE":
            return -self.amplitude_ms2 * forme, 1.5 * self.amplitude_ms2 * forme
        return -self.amplitude_ms2 * forme, 0.0


class TrajetSimule:
    def __init__(
        self,
        duree_s: float = 600.0,
        frequence_hz: float = 100.0,
        freinages_violents: int = 3,
        freinages_normaux: int = 8,
        nids_de_poule: int = 25,
        graine: int = 42,
        debut: float = 1_678_880_000.0,
    ) -> None:
        self.duree_s = duree_s
        self.frequence_hz = frequence_hz
        self.debut = debut
        self._graine = graine
        alea = random.Random(graine)

        # Chaque événement occupe un créneau de 8 s distinct : ils ne se chevauchent jamais.
        nombre_creneaux = int(duree_s // 8)
        demandes = [("FREINAGE_VIOLENT", freinages_violents), ("FREINAGE_NORMAL", freinages_normaux), ("NID_DE_POULE", nids_de_poule)]
        total = sum(n for _, n in demandes)
        if total > nombre_creneaux - 1:
            raise ValueError(f"Trajet trop court : {total} événements pour {nombre_creneaux - 1} créneaux de 8 s.")
        creneaux = alea.sample(range(1, nombre_creneaux), total)

        self.evenements: list[EvenementSimule] = []
        for type_evenement, nombre in demandes:
            for _ in range(nombre):
                instant = debut + creneaux.pop() * 8 + alea.uniform(0, 4)
                if type_evenement == "FREINAGE_VIOLENT":
                    evenement = EvenementSimule(type_evenement, instant, alea.uniform(1.2, 2.5), alea.uniform(3.2, 5.0))
                elif type_evenement == "FREINAGE_NORMAL":
                    evenement = EvenementSimule(type_evenement, instant, alea.uniform(2.0, 4.0), alea.uniform(0.8, 1.6))
                else:
                    evenement = EvenementSimule(type_evenement, instant, alea.uniform(0.02, 0.05), alea.uniform(3.0, 6.0))
                self.evenements.append(evenement)
        self.evenements.sort(key=lambda e: e.debut)

    @property
    def freinages_violents(self) -> list[EvenementSimule]:
        return [e for e in self.evenements if e.type == "FREINAGE_VIOLENT"]

    def echantillons(self) -> Iterator[Echantillon]:
        alea = random.Random(self._graine + 1)
        periode = 1.0 / self.frequence_hz
        a_venir = iter(self.evenements)
        actif = None
        prochain = next(a_venir, None)
        for rang in range(int(self.duree_s * self.frequence_hz)):
            t = self.debut + rang * periode
            # Vibrations : chaussée ~12 Hz, moteur ~30 Hz, plus un bruit blanc de capteur.
            vibration = 0.8 * math.sin(2 * math.pi * 12 * t) + 0.5 * math.sin(2 * math.pi * 30 * t)
            ax = 0.4 * math.sin(2 * math.pi * 12 * t + 1.0) + alea.gauss(0, 0.3)
            ay = vibration + alea.gauss(0, 0.3)
            az = G + 0.6 * vibration + alea.gauss(0, 0.4)
            while prochain is not None and prochain.debut <= t:
                actif, prochain = prochain, next(a_venir, None)
            if actif is not None:
                dy, dz = actif.contribution(t)
                ay += dy
                az += dz
            yield Echantillon(round(t, 2), ax, ay, az)
