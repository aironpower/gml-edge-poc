import json
import logging
import tempfile
import unittest
from pathlib import Path

from gml_edge.geo import ErreurDonnees
from gml_edge.safety import AnalyseurConduite, Echantillon, RapportLecture, executer_safety, lire_accelerometre
from gml_edge.simulation import TrajetSimule

DONNEES = Path(__file__).resolve().parent.parent / "donnees"


def journal_muet() -> logging.Logger:
    journal = logging.getLogger("gml_edge.tests")
    journal.propagate = False
    if not journal.handlers:
        journal.addHandler(logging.NullHandler())
    return journal


class TestJeuDeLEnonce(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        self.sortie = Path(self.dossier.name) / "daily_score.json"

    def tearDown(self):
        self.dossier.cleanup()

    def test_freinage_violent_isole(self):
        score = executer_safety(DONNEES / "accelerometer_data.csv", self.sortie, journal_muet())
        self.assertEqual(len(score["evenements"]), 1)
        evenement = score["evenements"][0]
        self.assertEqual(evenement["type"], "FREINAGE_VIOLENT")
        self.assertEqual(evenement["horodatage"], 1678880005)  # ligne 6 du fichier
        self.assertEqual(evenement["pic_brut_ms2"], -3.45)
        self.assertLess(evenement["pic_filtre_ms2"], -2.5)

    def test_daily_score_produit(self):
        executer_safety(DONNEES / "accelerometer_data.csv", self.sortie, journal_muet())
        score = json.loads(self.sortie.read_text(encoding="utf-8"))
        self.assertEqual(score["format"], "gml.daily_score/1.0")
        self.assertEqual(score["date"], "2023-03-15")
        self.assertEqual(score["synthese"]["score_securite"], 90)
        self.assertTrue(score["synthese"]["eligible_bonus_assurance"])
        self.assertFalse(self.sortie.with_suffix(".json.tmp").exists())

    def test_fichier_degrade_traite_sans_plantage(self):
        score = executer_safety(DONNEES / "robustesse" / "accelerometre_degrade.csv", self.sortie, journal_muet())
        qualite = score["qualite_donnees"]
        self.assertEqual(qualite["lignes_rejetees"], 6)
        self.assertEqual(qualite["motifs_rejet"]["horodatage non croissant"], 2)
        self.assertEqual(score["synthese"]["freinages_violents"], 1)

    def test_aucune_mesure_valide(self):
        csv = Path(self.dossier.name) / "vide.csv"
        csv.write_text("timestamp,acc_x,acc_y,acc_z\nabc,1,2,3\n", encoding="utf-8")
        score = executer_safety(csv, self.sortie, journal_muet())
        self.assertIsNone(score["synthese"]["score_securite"])
        self.assertFalse(score["synthese"]["eligible_bonus_assurance"])
        self.assertTrue(self.sortie.exists())

    def test_erreurs_fatales(self):
        for nom in ("colonne_manquante.csv", "absent.csv"):
            with self.assertRaises(ErreurDonnees):
                list(lire_accelerometre(DONNEES / "robustesse" / nom, journal_muet(), RapportLecture()))


class TestFiltrage(unittest.TestCase):
    def analyser(self, valeurs_y, frequence_hz=100.0):
        analyseur = AnalyseurConduite(journal_muet())
        for rang, ay in enumerate(valeurs_y):
            analyseur.ajouter(Echantillon(rang / frequence_hz, 0.0, ay, 9.81))
        analyseur.terminer()
        return analyseur

    def test_pic_bref_ignore(self):
        # Nid-de-poule : 30 ms à -6 m/s², bien au-delà du seuil mais trop court pour être un freinage.
        analyseur = self.analyser([0.0] * 50 + [-6.0] * 3 + [0.0] * 50)
        self.assertEqual(analyseur.evenements, [])
        self.assertEqual(analyseur.pics_brefs_ignores, 1)

    def test_freinage_soutenu_detecte_une_seule_fois(self):
        # 1 s à -3 m/s² avec un bruit alterné de ±0,6 : l'hystérésis évite de découper l'événement.
        bruit = [-3.0 + (0.6 if rang % 2 else -0.6) for rang in range(100)]
        analyseur = self.analyser([0.0] * 50 + bruit + [0.0] * 50)
        self.assertEqual(len(analyseur.evenements), 1)
        self.assertAlmostEqual(analyseur.evenements[0]["duree_s"], 1.0, delta=0.1)

    def test_freinage_normal_ignore(self):
        analyseur = self.analyser([0.0] * 50 + [-1.8] * 300 + [0.0] * 50)
        self.assertEqual(analyseur.evenements, [])

    def test_trajet_simule_a_100_hz(self):
        trajet = TrajetSimule(duree_s=600, freinages_violents=4, freinages_normaux=10, nids_de_poule=40, graine=3)
        analyseur = AnalyseurConduite(journal_muet())
        for e in trajet.echantillons():
            analyseur.ajouter(e)
        analyseur.terminer()
        detectes = [e for e in analyseur.evenements if e["type"] == "FREINAGE_VIOLENT"]
        self.assertEqual(len(detectes), 4)
        for evenement, injecte in zip(detectes, trajet.freinages_violents):
            self.assertTrue(injecte.debut <= evenement["horodatage"] <= injecte.debut + injecte.duree_s)


if __name__ == "__main__":
    unittest.main()
