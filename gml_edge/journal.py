"""Journalisation : console (colorée si possible) et fichier `sorties/gml_edge.log`."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

NOM_JOURNAL = "gml_edge"

_COULEURS = {
    logging.DEBUG: "\033[2m",  # atténué
    logging.WARNING: "\033[33m",  # jaune
    logging.ERROR: "\033[31m",  # rouge
    logging.CRITICAL: "\033[1;37;41m",  # blanc gras sur fond rouge : l'alerte ZFE
}
_RAZ = "\033[0m"
_FORMAT = "%(asctime)s | %(levelname)-8s | %(message)s"


class _FormateurCouleur(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        texte = super().format(record)
        couleur = _COULEURS.get(record.levelno)
        return f"{couleur}{texte}{_RAZ}" if couleur else texte


def _couleurs_disponibles() -> bool:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    if os.name == "nt":
        os.system("")  # active l'interprétation des séquences ANSI dans la console Windows
    return True


def configurer_journal(fichier: Path | None = None, verbeux: bool = False) -> logging.Logger:
    journal = logging.getLogger(NOM_JOURNAL)
    journal.setLevel(logging.DEBUG)
    for ancien in list(journal.handlers):
        ancien.close()
        journal.removeHandler(ancien)
    journal.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbeux else logging.INFO)
    formateur = _FormateurCouleur if _couleurs_disponibles() else logging.Formatter
    console.setFormatter(formateur(_FORMAT, datefmt="%H:%M:%S"))
    journal.addHandler(console)

    if fichier is not None:
        fichier.parent.mkdir(parents=True, exist_ok=True)
        sortie_fichier = logging.FileHandler(fichier, encoding="utf-8")
        sortie_fichier.setLevel(logging.DEBUG)
        sortie_fichier.setFormatter(logging.Formatter(_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
        journal.addHandler(sortie_fichier)

    return journal


def affichable(chemin: Path | str) -> str:
    """Chemin relatif au répertoire courant quand c'est possible : journaux lisibles en démonstration."""
    try:
        return str(Path(chemin).resolve().relative_to(Path.cwd()))
    except ValueError:
        return str(chemin)
