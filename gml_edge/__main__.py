"""Point d'entrée : `python -m gml_edge <commande>`. Voir `python -m gml_edge --help`."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .geo import ErreurDonnees
from .journal import affichable, configurer_journal
from .moteur_zfe import ResumeZFE, executer_zfe
from .performance import mesurer_safety, mesurer_zfe, milliers
from .safety import executer_safety
from .simulation import TrajetSimule
from .stockage import JournalLocal

RACINE = Path(__file__).resolve().parent.parent
DONNEES = RACINE / "donnees"
ROBUSTESSE = DONNEES / "robustesse"
SORTIES = RACINE / "sorties"
VEHICULE = "GML-TRUCK-001"

CODE_OK, CODE_DONNEES, CODE_INATTENDU, CODE_INTERROMPU = 0, 2, 3, 130


def _titre(journal: logging.Logger, texte: str) -> None:
    journal.info("")
    journal.info("=" * 100)
    journal.info(texte)
    journal.info("=" * 100)


def _bilan_zfe(journal: logging.Logger, resume: ResumeZFE) -> None:
    c = resume.compteurs_zone
    journal.info("-" * 100)
    journal.info(
        "Bilan ZFE : %d points traités, %d ignorés ; %d pré-alerte(s), %d alerte(s) ; calcul le plus long %.0f µs.",
        resume.points_traites, resume.points_ignores, len(resume.pre_alertes), len(resume.alertes), resume.duree_calcul_max_us,
    )
    journal.info(
        "Bounding Box : %d point(s) sur %d écarté(s) en O(1) ; Ray Casting exécuté %d fois.",
        c.get("rejets_boite", 0), c.get("points", 0), c.get("ray_casting", 0),
    )
    if resume.verifications:
        journal.info("Contrôle du champ « expected » du jeu de données :")
        for v in resume.verifications:
            distance = "" if v["distance_frontiere_m"] in (None, 0) else f" (frontière à {v['distance_frontiere_m']} m)"
            journal.info(
                "  #%-3s attendu %-16s obtenu %-9s%s %s",
                v["point"], v["attendu"], v["obtenu"], distance, "OK" if v["conforme"] else "ÉCART — voir README § Point n°3",
            )


def _bilan_safety(journal: logging.Logger, score: dict, sortie: Path) -> None:
    q, s = score["qualite_donnees"], score["synthese"]
    journal.info("-" * 100)
    journal.info(
        "Bilan Safety : %s lignes lues, %s valides, %s rejetées ; %d pic(s) bref(s) écarté(s).",
        milliers(q["lignes_lues"]), milliers(q["lignes_valides"]), milliers(q["lignes_rejetees"]), q["pics_brefs_ignores"],
    )
    journal.info(
        "Freinages violents : %d ; accélérations brusques : %d ; virages brusques : %d.",
        s["freinages_violents"], s["accelerations_brusques"], s["virages_brusques"],
    )
    if s["score_securite"] is not None:
        journal.info(
            "Score sécurité du %s : %d/100 (niveau %s) — éligible au bonus assurance : %s.",
            score["date"], s["score_securite"], s["niveau"], "OUI" if s["eligible_bonus_assurance"] else "NON",
        )
    journal.info("Fichier produit : %s", affichable(sortie))


def commande_zfe(args: argparse.Namespace, journal: logging.Logger) -> int:
    _titre(journal, "MODULE 1 — MOTEUR ZFE (Geo) : Bounding Box + Ray Casting, 100 % hors-ligne")
    with JournalLocal(args.base) as stockage:
        resume = executer_zfe(
            args.polygone, args.trace, journal, stockage,
            temps_reel=args.temps_reel, acceleration=args.acceleration, marge_pre_alerte_m=args.pre_alerte,
        )
    _bilan_zfe(journal, resume)
    return CODE_OK


def commande_safety(args: argparse.Namespace, journal: logging.Logger) -> int:
    _titre(journal, "MODULE 2 — MOTEUR SAFETY (Physics) : filtrage, seuil 2,5 m/s², score journalier")
    with JournalLocal(args.base) as stockage:
        score = executer_safety(args.csv, args.sortie, journal, stockage, vehicule_id=VEHICULE)
    _bilan_safety(journal, score, args.sortie)
    return CODE_OK


def commande_synchro(args: argparse.Namespace, journal: logging.Logger) -> int:
    _titre(journal, "SYNCHRONISATION DIFFÉRÉE — SQLite local → lot JSON compressé (gzip) via 4G M2M")
    with JournalLocal(args.base) as stockage:
        lot = stockage.preparer_lot(VEHICULE)
        if lot.vide:
            journal.info("Rien à transmettre : tous les événements sont déjà synchronisés.")
            return CODE_OK
        journal.info(
            "Lot prêt : %d événement(s) et %d score(s) journalier(s) — %s octets JSON, %s octets compressés.",
            len(lot.ids_evenements), len(lot.cles_scores), milliers(lot.octets_json), milliers(lot.octets_gzip),
        )
        if args.hors_ligne:
            journal.warning("[RESEAU] 4G indisponible : le lot reste en file d'attente dans %s (rien n'est perdu).", affichable(args.base))
            return CODE_OK
        fichier_lot = args.base.parent / "dernier_lot.json.gz"
        fichier_lot.parent.mkdir(parents=True, exist_ok=True)
        fichier_lot.write_bytes(lot.contenu_gzip)
        stockage.marquer_transmis(lot)
        journal.info("[RESEAU] Lot transmis (simulation : copie dans %s) et marqué comme envoyé.", affichable(fichier_lot))
    return CODE_OK


def commande_demo(args: argparse.Namespace, journal: logging.Logger) -> int:
    args.temps_reel = True
    commande_zfe(args, journal)
    commande_safety(args, journal)
    args.hors_ligne = True
    commande_synchro(args, journal)
    args.hors_ligne = False
    commande_synchro(args, journal)
    return CODE_OK


def commande_robustesse(args: argparse.Namespace, journal: logging.Logger) -> int:
    """Enchaîne des entrées dégradées : chaque scénario doit se terminer proprement, aucun ne doit planter."""
    scenarios = [
        ("Trace GPS avec perte de signal (tunnel) et trames corrompues", "zfe", DONNEES / "lyon_polygon.json", ROBUSTESSE / "trace_gps_degradee.json"),
        ("CSV accéléromètre bruité et corrompu", "safety", ROBUSTESSE / "accelerometre_degrade.csv", None),
        ("Polygone ZFE invalide (2 sommets)", "zfe", ROBUSTESSE / "polygone_invalide.json", DONNEES / "truck_gps.json"),
        ("Trace GPS au JSON tronqué", "zfe", DONNEES / "lyon_polygon.json", ROBUSTESSE / "trace_tronquee.json"),
        ("CSV sans colonne acc_y", "safety", ROBUSTESSE / "colonne_manquante.csv", None),
        ("Fichier inexistant", "safety", ROBUSTESSE / "absent.csv", None),
    ]
    plantages = 0
    for rang, (libelle, module, entree, trace) in enumerate(scenarios, start=1):
        _titre(journal, f"ROBUSTESSE {rang}/{len(scenarios)} — {libelle}")
        try:
            with JournalLocal(":memory:") as stockage:
                if module == "zfe":
                    _bilan_zfe(journal, executer_zfe(entree, trace, journal, stockage))
                else:
                    sortie = args.base.parent / "robustesse_daily_score.json"
                    _bilan_safety(journal, executer_safety(entree, sortie, journal, stockage, vehicule_id=VEHICULE), sortie)
            journal.info("=> Scénario terminé normalement.")
        except ErreurDonnees as erreur:
            journal.error("[ERREUR DONNÉES] %s", erreur)
            journal.info("=> Erreur détectée et maîtrisée : le moteur refuse l'entrée sans planter.")
        except Exception:
            plantages += 1
            journal.exception("=> PLANTAGE inattendu")
    _titre(journal, f"Robustesse : {len(scenarios)} scénarios, {plantages} plantage(s).")
    return CODE_OK if plantages == 0 else CODE_INATTENDU


def commande_bench(args: argparse.Namespace, journal: logging.Logger) -> int:
    from .moteur_zfe import charger_polygone

    _titre(journal, "PERFORMANCE — faisabilité sur Zebra ET40 (Snapdragon 660, 150 Mo de RAM)")
    mesurer_zfe(charger_polygone(args.polygone).sommets, journal)
    mesurer_safety(journal)
    return CODE_OK


def commande_generer(args: argparse.Namespace, journal: logging.Logger) -> int:
    trajet = TrajetSimule(duree_s=args.duree)
    args.sortie.parent.mkdir(parents=True, exist_ok=True)
    with open(args.sortie, "w", encoding="utf-8", newline="") as fichier:
        fichier.write("timestamp,acc_x,acc_y,acc_z\n")
        for e in trajet.echantillons():
            fichier.write(f"{e.horodatage:.2f},{e.ax:.3f},{e.ay:.3f},{e.az:.3f}\n")
    journal.info("%s écrit : %d s à 100 Hz.", affichable(args.sortie), args.duree)
    journal.info("Freinages violents cachés (vérité terrain) :")
    for e in trajet.freinages_violents:
        journal.info("  t = %.2f s (début), durée %.2f s, amplitude -%.2f m/s²", e.debut - trajet.debut, e.duree_s, e.amplitude_ms2)
    journal.info("Analyse : python -m gml_edge safety --csv %s", affichable(args.sortie))
    return CODE_OK


def construire_analyseur() -> argparse.ArgumentParser:
    commun = argparse.ArgumentParser(add_help=False)
    commun.add_argument("--base", type=Path, default=SORTIES / "gml_edge.db", help="base SQLite locale (défaut : %(default)s)")
    commun.add_argument("--journal", type=Path, default=SORTIES / "gml_edge.log", help="fichier journal (défaut : %(default)s)")
    commun.add_argument("-v", "--verbeux", action="store_true", help="affiche aussi les messages de débogage")

    zfe = argparse.ArgumentParser(add_help=False)
    zfe.add_argument("--polygone", type=Path, default=DONNEES / "lyon_polygon.json")
    zfe.add_argument("--trace", type=Path, default=DONNEES / "truck_gps.json")
    zfe.add_argument("--temps-reel", action="store_true", help="rejoue la trace au rythme de ses horodatages")
    zfe.add_argument("--acceleration", type=float, default=5.0, help="facteur d'accélération du temps réel (défaut : x%(default)s)")
    zfe.add_argument("--pre-alerte", type=float, default=500.0, help="distance de pré-alerte en mètres (défaut : %(default)s)")

    safety = argparse.ArgumentParser(add_help=False)
    safety.add_argument("--csv", type=Path, default=DONNEES / "accelerometer_data.csv")
    safety.add_argument("--sortie", type=Path, default=SORTIES / "daily_score.json")

    analyseur = argparse.ArgumentParser(prog="python -m gml_edge", description="POC Edge télématique GreenMove Logistics.")
    analyseur.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sous = analyseur.add_subparsers(dest="commande", required=True)
    sous.add_parser("zfe", parents=[commun, zfe], help="Module 1 : détection d'entrée en ZFE").set_defaults(fonction=commande_zfe)
    sous.add_parser("safety", parents=[commun, safety], help="Module 2 : freinages violents et daily_score.json").set_defaults(fonction=commande_safety)
    p = sous.add_parser("synchro", parents=[commun], help="transmet les événements en attente dans SQLite")
    p.add_argument("--hors-ligne", action="store_true", help="simule l'absence de réseau")
    p.set_defaults(fonction=commande_synchro)
    sous.add_parser("demo", parents=[commun, zfe, safety], help="démonstration complète (ZFE en temps réel, Safety, synchro)").set_defaults(
        fonction=commande_demo
    )
    sous.add_parser("robustesse", parents=[commun], help="rejoue des entrées corrompues : aucun plantage").set_defaults(fonction=commande_robustesse)
    sous.add_parser("bench", parents=[commun, zfe], help="mesures de performance, mémoire et volume 4G").set_defaults(fonction=commande_bench)
    p = sous.add_parser("generer", parents=[commun], help="génère un CSV accéléromètre bruité à 100 Hz")
    p.add_argument("--duree", type=float, default=600.0, help="durée en secondes (défaut : %(default)s)")
    p.add_argument("--sortie", type=Path, default=SORTIES / "accelerometre_100hz.csv")
    p.set_defaults(fonction=commande_generer)
    return analyseur


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        # Sous Windows, une sortie redirigée vers un fichier serait sinon encodée en cp1252 (accents et µ illisibles).
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = construire_analyseur().parse_args(argv)
    journal = configurer_journal(args.journal, args.verbeux)
    try:
        return args.fonction(args, journal)
    except ErreurDonnees as erreur:
        journal.error("[ERREUR DONNÉES] %s", erreur)
        return CODE_DONNEES
    except KeyboardInterrupt:
        journal.warning("Interrompu par l'utilisateur.")
        return CODE_INTERROMPU
    except Exception as erreur:  # dernier filet : message clair en console, trace complète dans le fichier journal
        journal.debug("Trace complète :", exc_info=True)
        journal.error("[ERREUR INATTENDUE] %s: %s (détail dans %s)", type(erreur).__name__, erreur, affichable(args.journal))
        return CODE_INATTENDU


if __name__ == "__main__":
    sys.exit(main())
