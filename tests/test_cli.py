import contextlib
import io
import json
import logging
import tempfile
import unittest
from pathlib import Path

from gml_edge.__main__ import CODE_DONNEES, CODE_OK, ROBUSTESSE, main
from gml_edge.stockage import JournalLocal


class TestLigneDeCommande(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        racine = Path(self.dossier.name)
        self.base = racine / "gml_edge.db"
        self.fichier_journal = racine / "gml_edge.log"
        self.commun = ["--base", str(self.base), "--journal", str(self.fichier_journal)]

    def tearDown(self):
        for gestionnaire in logging.getLogger("gml_edge").handlers:
            gestionnaire.close()  # sous Windows, un journal ouvert empêcherait la suppression du dossier
        self.dossier.cleanup()

    def lancer(self, *arguments):
        with contextlib.redirect_stdout(io.StringIO()) as sortie:
            code = main([arguments[0], *self.commun, *arguments[1:]])
        return code, sortie.getvalue()

    def test_zfe_emet_l_alerte(self):
        code, sortie = self.lancer("zfe")
        self.assertEqual(code, CODE_OK)
        self.assertIn("[PRE-ALERT ZFE]", sortie)
        self.assertEqual(sortie.count("[ALERT ZFE]"), 1)  # pas de répétition tant que le camion reste dans la zone
        self.assertIn("[ALERT ZFE]", self.fichier_journal.read_text(encoding="utf-8"))

    def test_safety_produit_le_score(self):
        daily = Path(self.dossier.name) / "daily_score.json"
        code, _ = self.lancer("safety", "--sortie", str(daily))
        self.assertEqual(code, CODE_OK)
        self.assertEqual(json.loads(daily.read_text(encoding="utf-8"))["synthese"]["freinages_violents"], 1)

    def test_synchronisation_differee(self):
        self.lancer("zfe")
        self.lancer("synchro", "--hors-ligne")
        with JournalLocal(self.base) as stockage:
            self.assertEqual(stockage.compter_en_attente(), 2)  # pré-alerte + alerte conservées hors réseau
        self.lancer("synchro")
        with JournalLocal(self.base) as stockage:
            self.assertEqual(stockage.compter_en_attente(), 0)
        self.assertTrue((self.base.parent / "dernier_lot.json.gz").exists())

    def test_entree_invalide_sans_trace_python(self):
        code, sortie = self.lancer("zfe", "--polygone", str(ROBUSTESSE / "polygone_invalide.json"))
        self.assertEqual(code, CODE_DONNEES)
        self.assertIn("[ERREUR DONNÉES]", sortie)
        self.assertNotIn("Traceback", sortie)

    def test_robustesse_aucun_plantage(self):
        code, sortie = self.lancer("robustesse")
        self.assertEqual(code, CODE_OK)
        self.assertIn("6 scénarios, 0 plantage(s)", sortie)


if __name__ == "__main__":
    unittest.main()
