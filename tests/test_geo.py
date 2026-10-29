import unittest
from pathlib import Path

from gml_edge.geo import (
    METRES_PAR_DEGRE_LAT,
    BoiteEnglobante,
    ErreurDonnees,
    EtatZone,
    ZoneZFE,
    distance_frontiere_m,
    point_dans_polygone,
)
from gml_edge.moteur_zfe import charger_json, charger_polygone

DONNEES = Path(__file__).resolve().parent.parent / "donnees"

CARRE = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0)]
# Polygone concave en U : l'encoche (lat 0.5 à 1, lon 1 à 2) est hors zone mais dans la Bounding Box.
EN_U = [(0.0, 0.0), (0.0, 3.0), (1.0, 3.0), (1.0, 2.0), (0.5, 2.0), (0.5, 1.0), (1.0, 1.0), (1.0, 0.0)]


class TestRayCasting(unittest.TestCase):
    def test_carre(self):
        self.assertTrue(point_dans_polygone(0.5, 0.5, CARRE))
        self.assertFalse(point_dans_polygone(1.5, 0.5, CARRE))
        self.assertFalse(point_dans_polygone(0.5, -0.1, CARRE))

    def test_polygone_concave(self):
        self.assertTrue(point_dans_polygone(0.25, 1.5, EN_U))  # base du U
        self.assertTrue(point_dans_polygone(0.75, 0.5, EN_U))  # branche gauche
        self.assertFalse(point_dans_polygone(0.75, 1.5, EN_U))  # encoche : dans la boîte, hors du polygone

    def test_rayon_passant_par_un_sommet(self):
        # Le rayon vers l'est passe exactement par le sommet (0.5, 2.0) : il ne doit pas être compté deux fois.
        self.assertTrue(point_dans_polygone(0.5, 0.5, EN_U))


class TestBoiteEnglobante(unittest.TestCase):
    def test_contient(self):
        boite = BoiteEnglobante.depuis_sommets(CARRE)
        self.assertTrue(boite.contient(0.5, 0.5))
        self.assertFalse(boite.contient(1.01, 0.5))

    def test_elargie_de_500_m(self):
        boite = BoiteEnglobante(45.0, 45.1, 4.0, 4.1).elargie(500)
        self.assertAlmostEqual((45.0 - boite.lat_min) * METRES_PAR_DEGRE_LAT, 500, delta=1)
        self.assertGreater(4.0 - boite.lon_min, 45.0 - boite.lat_min)  # un degré de longitude est plus court


class TestDistanceFrontiere(unittest.TestCase):
    def test_distance_a_une_arete_horizontale(self):
        # 0,001° de latitude au nord du bord supérieur du carré ≈ 111 m.
        self.assertAlmostEqual(distance_frontiere_m(1.001, 0.5, CARRE), 111.2, delta=0.5)


class TestZoneLyon(unittest.TestCase):
    def setUp(self):
        self.zone = charger_polygone(DONNEES / "lyon_polygon.json")
        self.trace = {p["id"]: p for p in charger_json(DONNEES / "truck_gps.json")}

    def classer(self, identifiant):
        point = self.trace[identifiant]
        return self.zone.classer(point["lat"], point["lon"])

    def test_sommet_de_fermeture_retire(self):
        self.assertEqual(len(self.zone.sommets), 5)

    def test_points_eloignes_ecartes_par_la_boite(self):
        for identifiant in (1, 2):
            classement = self.classer(identifiant)
            self.assertEqual(classement.etat, EtatZone.HORS_ZONE)
            self.assertTrue(classement.rejet_boite)

    def test_point_3_en_approche_a_451_m(self):
        classement = self.classer(3)
        self.assertEqual(classement.etat, EtatZone.APPROCHE)
        self.assertAlmostEqual(classement.distance_frontiere_m, 451, delta=5)
        self.assertFalse(point_dans_polygone(45.7850, 4.8050, self.zone.sommets))

    def test_points_4_et_5_dans_la_zfe(self):
        for identifiant in (4, 5):
            self.assertEqual(self.classer(identifiant).etat, EtatZone.DANS_ZFE)

    def test_tolerance_gps_a_la_frontiere(self):
        # 5 m au nord de l'arête (45.78, 4.80) → (45.79, 4.86) : principe de précaution.
        lat_arete = 45.78 + 0.01 * (0.03 / 0.06)
        classement = self.zone.classer(lat_arete + 5 / METRES_PAR_DEGRE_LAT, 4.83)
        self.assertEqual(classement.etat, EtatZone.DANS_ZFE)

    def test_polygone_degenere_refuse(self):
        with self.assertRaises(ErreurDonnees):
            ZoneZFE("plat", [(0.0, 0.0), (1.0, 1.0), (0.0, 0.0)])
        with self.assertRaises(ErreurDonnees):
            charger_polygone(DONNEES / "robustesse" / "polygone_invalide.json")


if __name__ == "__main__":
    unittest.main()
