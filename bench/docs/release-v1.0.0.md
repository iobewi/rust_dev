# IOBEWI Device Lab v1.0.0 — notes de version

Première publication officielle. Le tag `v1.0.0` désigne le **commit de fusion de la PR de release** dans `main` ; son SHA exact est reporté dans la release GitHub à la publication (il n'existe pas avant la fusion).

## Contenu

- Une fenêtre navigateur, plusieurs cartes Web Serial : agent, moteur matériel et contexte indépendants par carte ; liste permanente **Cartes actives** (ajout, sélection par le nom, déconnexion et reconnexion par le statut, retrait).
- Identification et Commandes liées à la carte sélectionnée ; consoles, opérations, réservations, grants et quarantaines isolés par carte.
- API REST/WebSocket pour clients externes selon leurs scopes ; journal durable ; aucune opération incertaine n'est rejouée.
- Client Python, plugin pytest et recettes de qualification génériques (M40 comprises).
- Documentation : [README](../README.md), [interface et parcours minimal](interface.md), [contextes multicarte](multi-board.md), [API](api.md), [fiabilité](reliability.md).

Cette version ne change ni le protocole (version 1) ni le contrat des agents API distants.

## Qualification : acquis et limites

- Logiciel : suites Node, Python et Playwright (deux ports Web Serial simulés dans une page, serveur HTTP/WebSocket réel) vertes en CI sur la PR de release.
- Matériel (ESP32-S3 DevKitC-1 N16 sur UART externe, ESP32-S3 Zero sur USB Serial/JTAG) : M40-0, M40-5 à 9, M40-11a, M40-11c/d et M40-12a PASS ; M40-3 et M40-8 partiels ; M40-1/2 provisoires (INDETERMINATE). Détail et limites : [qualification-multi-board](qualification-multi-board.md), [complément](qualification-multi-board-complement.md).
- Non exécuté matériel : M40-ID, M40-PHYS-B (simulation seulement), M40-12b/c, M40-4 simultané. Un PASS simulé n'est jamais un PASS matériel.
- Acceptation matérielle finale : voir le rapport de la PR de release ; elle n'est pas un résultat de ce document.

## Limites connues

- `esptool-js 0.7.0` est chargé depuis `unpkg.com` par le navigateur (accès Internet requis).
- Pas d'authentification générale de l'interface web : usage local sur `127.0.0.1` ou transfert sécurisé uniquement.
- Qualifié sur ESP32-S3 uniquement ; un seul serveur autoritaire.
- Anomalies suivies pour les versions suivantes : ré-enregistrements périodiques non expliqués des agents, capacité flash annoncée par la carte USB Serial/JTAG (4 Mio observés au lieu de 16 Mio attendus), flake Playwright connu (`#card-boot-actions`), durcissement des écritures de preuves (liens symboliques, permissions d'un fichier annexe de lecture flash).

## Dépendances et prérequis

| Composant | Version |
|---|---|
| Python | 3.12+ |
| aiohttp | 3.14.4 (`requirements.txt`) |
| pytest (client, optionnel) | 8 à 9 (`python/pyproject.toml`) |
| Node (tests) | 22+ |
| Playwright (tests navigateur) | 1.62.1 (`package-lock.json`) |
| Navigateur de l'opérateur | Prise en charge de l'API Web Serial : Chrome ou Edge récents, Firefox Desktop 151+. Qualifié : tests Playwright sous Chromium (moteur simulé) ; essais matériels de l'issue #2 sous Firefox 159. Pas de matrice par navigateur. |
| esptool-js (navigateur) | 0.7.0 via `unpkg.com` |

## Distribution

La release porte sur le **dépôt entier** au tag `v1.0.0` ; le paquet « IOBEWI Device Lab » est le dossier `bench/`. Il n'y a ni PyPI, ni npm, ni image Docker publiée : on installe depuis l'archive source GitHub du tag ou un clone. `.devcontainer/` et `docker/` ne servent qu'au développement.

Inventaire (`git ls-files`, 165 fichiers suivis avant la release) :

| Chemin | Rôle |
|---|---|
| `bench/server/`, `bench/protocol/` | serveur Python et protocole |
| `bench/web/` | interface et agent navigateur |
| `bench/python/` | client Python, plugin pytest |
| `bench/recipes/` | recettes de qualification génériques |
| `bench/tools/` | lanceur, simulateur d'agent, comparateur |
| `bench/tests/`, `bench/python/tests/` | tests Node, Python, Playwright |
| `bench/docs/`, `bench/examples/`, `bench/audit/` | documentation, exemple, audits historiques |
| `bench/README.md`, `requirements.txt`, `package*.json`, `pytest.ini` | métadonnées |
| `.github/workflows/`, `.devcontainer/`, `docker/`, `AGENTS.md`, `CLAUDE.md`, `LICENSE` | CI et développement |

Régénérer l'inventaire : `git ls-files` au commit du tag. **Jamais inclus** : tokens, `BENCH_STATE_DIR`, sessions privées, preuves de qualification, dumps flash, journaux UART (ils restent hors Git).

## Installation vérifiée

Procédure du [README](../README.md#installation) exécutée dans un environnement neuf et isolé (venv vierge, copie des seuls fichiers suivis, état et tokens temporaires, port 18080, aucun matériel) :

1. `pip install -r bench/requirements.txt` : aiohttp 3.14.4 installé ; `pip install -e './bench/python[pytest]'` : `iobewi-bench 1.0.0`.
2. Génération du fichier de tokens (`0600`) et du dossier d'état.
3. Sans `BENCH_TOKENS_FILE` : refus explicite de démarrer.
4. `python3 bench/tools/run_server.py` : `/health` renvoie `status: ok`, protocole 1, durable, version `1.0.0` ; `/` sert « IOBEWI Device Lab » et « Cartes actives » ; `/api/v1/devices` répond 401 sans token, 200 avec ; un Host étranger est refusé (401).
5. Arrêt par SIGTERM au lanceur identifié.

## Retour à la version précédente

Le tag précédent n'existe pas (première release) : le retour se fait au dernier commit connu de `main` avant la release (`d66eed8`).

1. Arrêter le serveur : SIGTERM au PID du **lanceur** affiché ; attendre sa fin.
2. Conserver `BENCH_STATE_DIR` (ne jamais le supprimer) ; le serveur de la version précédente le relit.
3. `git checkout d66eed8` (ou extraire l'archive de ce commit), réinstaller `bench/requirements.txt`.
4. Redémarrer avec `python3 bench/tools/run_server.py` et le même `BENCH_TOKENS_FILE`. Un redémarrage change l'`epoch`, invalide sessions, réservations et grants ; les opérations incertaines passent en récupération explicite, rien n'est rejoué.
5. Recharger la page du navigateur et reconnecter les ports.

Le passage de `1.0.0` à `d66eed8` ne change que la chaîne de version, la documentation et les métadonnées : aucune migration de données n'est nécessaire.
