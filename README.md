# GML Edge — POC du moteur télématique

[![Tests](https://github.com/aironpower/gml-edge-poc/actions/workflows/tests.yml/badge.svg)](https://github.com/aironpower/gml-edge-poc/actions/workflows/tests.yml)

Preuve de concept du moteur embarqué dans la tablette des camions de GreenMove Logistics. Il fonctionne sans réseau et contient deux modules :

- Module 1 — ZFE (Geo) : détecte l'entrée dans la Zone à Faibles Émissions de Lyon à partir de `lyon_polygon.json` et `truck_gps.json`, et produit le log `[ALERT ZFE]`.
- Module 2 — Safety (Physics) : isole les freinages violents dans `accelerometer_data.csv` et produit `daily_score.json`.

Le code utilise uniquement la bibliothèque standard de Python : il n'y a rien à installer.

## Procédure de test en local

Prérequis : Python 3.10 ou plus récent et Git.

```bash
git clone https://github.com/aironpower/gml-edge-poc.git
cd gml-edge-poc

python -m gml_edge zfe      # Module 1
python -m gml_edge safety   # Module 2, écrit sorties/daily_score.json
python -m unittest -v       # tests automatisés
```

Sous Windows, utilisez `py` à la place de `python` si nécessaire.

Autres commandes :

| Commande | Rôle |
| --- | --- |
| `python -m gml_edge zfe --temps-reel` | Rejoue la trace GPS au rythme de ses horodatages |
| `python -m gml_edge demo` | Démonstration complète : ZFE en temps réel, Safety, synchronisation |
| `python -m gml_edge robustesse` | Rejoue six fichiers corrompus pour vérifier qu'aucun ne fait planter le moteur |
| `python -m gml_edge bench` | Mesures de performance, de mémoire et de volume 4G |
| `python -m gml_edge generer` | Génère un CSV bruité à 100 Hz contenant des freinages violents cachés |

Les fichiers produits (score, journal, base SQLite) sont écrits dans `sorties/`.

## Module 1 — ZFE

Chaque position GPS passe par trois étapes, de la moins coûteuse à la plus coûteuse :

1. Bounding Box : le rectangle qui englobe la ZFE, élargi de 500 m, est calculé une seule fois. Une position située hors de ce rectangle est écartée en quatre comparaisons.
2. Ray Casting : pour une position dans le rectangle, on trace un rayon vers l'est et on compte les arêtes du polygone traversées. Un nombre impair signifie que le camion est dans la zone. L'algorithme ne fait qu'une boucle sur les arêtes, il n'est donc pas en O(N²).
3. Distance à la frontière : à moins de 500 m de la zone, le moteur émet une pré-alerte. À moins de 10 m (précision du GPS), le camion est considéré comme dans la zone.

L'alerte n'est émise qu'au changement d'état, pour ne pas répéter le message au chauffeur à chaque seconde. Une position GPS manquante ou invalide (perte de signal dans un tunnel, par exemple) est ignorée et le dernier état connu est conservé.

Remarque sur le point n°3 de `truck_gps.json` : il est annoncé « IN (Bordure) », mais il se trouve en dehors du polygone, à 451 m au nord de l'arête (45.78, 4.80) → (45.79, 4.86). Le moteur le classe donc en approche et déclenche la pré-alerte ; l'alerte d'entrée se déclenche au point n°4.

## Module 2 — Safety

Le fichier CSV est lu ligne par ligne, en un seul passage et avec une mémoire constante :

1. Les lignes invalides (valeur non numérique, colonne manquante, horodatage qui recule, valeur supérieure à 4 g) sont écartées et comptées, sans arrêter le traitement.
2. Un filtre passe-bas à 5 Hz atténue les vibrations de la route et du moteur. Il est défini en hertz et non en nombre de points, ce qui le rend valable à 1 Hz (jeu de l'énoncé) comme à 100 Hz (tablette).
3. Un freinage violent commence quand la décélération filtrée dépasse 2,5 m/s² et se termine quand elle repasse sous 1,5 m/s².
4. Un pic de moins de 0,3 s (nid-de-poule) est ignoré.

Le seuil retenu est 2,5 m/s², comme dans l'Annexe 3. Le « 2.5G » du Dossier 4 correspondrait à 24,5 m/s², une valeur qu'un camion n'atteint jamais.

Une moyenne glissante a été écartée : sur le jeu de l'énoncé, elle ramène le pic de -3,45 à -1,82 m/s², sous le seuil, et le freinage n'est plus détecté.

Le fichier `daily_score.json` contient le véhicule, la date, les paramètres utilisés, la qualité des données, un score sur 100 avec l'éligibilité au bonus d'assurance, et la liste des événements détectés. Il ne contient jamais les données brutes. Voir l'[exemple](docs/exemple_daily_score.json).

## Stockage et transmission

Les alertes et les scores sont enregistrés dans une base SQLite locale. Ils sont envoyés par lot compressé quand le réseau est disponible et restent en attente sinon. Dans ce POC, l'envoi est simulé par l'écriture d'un fichier.

## Organisation du code

```
gml_edge/
├── __main__.py      ligne de commande
├── geo.py           Bounding Box, Ray Casting, distance à la frontière
├── moteur_zfe.py    Module 1
├── safety.py        Module 2
├── stockage.py      base SQLite et lots à transmettre
├── simulation.py    signal accélérométrique simulé à 100 Hz
├── performance.py   commande bench
└── journal.py       affichage des logs
donnees/             jeu de données de l'énoncé et fichiers corrompus
tests/               tests unitaires
```
