# Recettes matérielles avec pytest

La [campagne de qualification v2](qualification-v2.md) fournit des recettes de
lecture seule avec refus précis, référence vérifiée et preuves automatiques pour
les deux clients, les interruptions et le nettoyage. Elle doit être rejouée sur
matériel ; les verdicts de la campagne historique restent attachés à ses fichiers.

Le moteur de recette est pytest. Le composant `bench/python` fournit un client API v1
sans dépendance d’exécution et un petit plugin pytest optionnel. Il ne possède pas le
port série : le navigateur reste l’agent matériel et le serveur conserve l’exclusion,
les scopes, les grants et la quarantaine. Aucun langage de recette supplémentaire.

## Sélection des tests et niveaux de validation

**Ne pas confondre le moteur pytest avec le périmètre à qualifier.** Les
répertoires ont des responsabilités distinctes :

| Emplacement | Responsabilité |
| --- | --- |
| `bench/tests/` | Tests internes Node.js et navigateur du banc |
| `bench/python/tests/` | Tests internes du client Python, du plugin et de l'infrastructure |
| `bench/recipes/` | Recettes génériques versionnées avec Device Lab |
| Dépôt consommateur | Recettes métier propres au firmware ou au système qualifié (par exemple SDIO P4/C6) |

Avant l'exécution, dresser le plan : composant modifié, nature, niveau,
tests directs, dépendants, commande prévue, tests collectés, reste confié à
la CI et justification. Vérifier la sélection pytest via `--collect-only`
quand c'est pertinent. Le nombre de tests du dépôt ne détermine jamais
à lui seul la taille de la validation.

**L1 — tests ciblés** (recette nouvelle ou changement localisé) : uniquement
les tests de la modification et de ses invariants. Exemple, depuis `bench/` :

```sh
python -m pytest python/tests/test_m40_identity.py --collect-only -q
python -m pytest python/tests/test_m40_identity.py -q
```

Ces chemins illustrent **la sélection**, pas une instruction de relancer la
qualification M40 pour une autre tâche. Une recette de projet consommateur est
testée dans **son propre dépôt**, pas en collectant tous les tests de Device Lab.

**L2 — non-régression ciblée** (module partagé/API) : ajouter aux tests L1
ceux des consommateurs directs. Par exemple, si le gestionnaire de contextes
Web Serial change, cibler ses tests Node et les tests navigateur concernés ;
si le client Python change, cibler ses tests contractuels et les recettes
directement dépendantes. Justifier toute extension de la sélection.

**L3 — validation globale** (CI, refonte transversale, mécanisme critique ou
requalification explicitement demandée) : la CI GitHub de PR exécute sa
suite logicielle avec le filtre `not slow_integration` ; le lancement manuel
du workflow couvre aussi les intégrations simulées longues. Ces commandes,
depuis `bench/`, décrivent les deux niveaux de CI, **pas** une obligation
de les relancer dans chaque session :

```sh
# CI PR : sélection globale rapide, automatique sur PR concernée
python -m pytest python/tests recipes -m 'not slow_integration' -vv --durations=20 --durations-min=1
# CI workflow_dispatch : intégration complète, demandée explicitement
python -m pytest python/tests recipes -vv --durations=30 --durations-min=1
```

Les tests Node et Playwright sont également exécutés par la CI ; voir
`.github/workflows/bench.yml`. Une CI non déclenchée, absente ou en échec
doit être signalée : L1/L2 ne valent pas CI globale verte.

**CI conteneur — environnement reproductible** : le job `container` du
workflow `.github/workflows/devcontainer.yml` construit et ouvre le
devcontainer, exécute `bench-container-smoke` avant et après recréation,
et vérifie la persistance des volumes privés. Le smoke couvre la disponibilité
des toolchains ESP/Rust, Python et Node, l'import effectif du client depuis
`/workspace/bench/python` et l'ouverture d'un journal SQLite isolé. Il
**n'exécute pas** la suite générale pytest, Node ou unittest : ces tests
relèvent de `software` et ne doivent pas être rejoués localement pour
préparer la CI. Les jobs se déclenchent suivant leurs propres filtres
de chemins ; une CI absente ne peut pas être considérée comme verte.

**H — qualification matérielle**, indépendante de L1/L2/L3 : sélectionner
une recette pytest précise et son matériel déclaré, obtenir avant toute
commande les consentements applicables, scopes, réservations et grants
locaux nécessaires. Seuls le navigateur et l'agent Web Serial possèdent
le port USB. L'accès matériel passe exclusivement par l'API Device Lab.
Un `SKIP` sans `--bench` ou un PASS simulé ne vaut pas qualification d'ESP32.
Les campagnes historiques ne sont jamais rejouées sans motif et accord
explicite.

Pour interrompre une suite longue, relever le PID et le groupe des
processus qu'elle a lancés ; arrêter uniquement ceux-ci, jamais un
serveur Device Lab actif. Éviter `pkill -f` générique et ne pas ouvrir
un serveur concurrent sur le port du banc.

## Installation et première campagne

Depuis la racine du dépôt, dans l’environnement Python du projet :

```sh
python -m pip install -e './bench/python[pytest]'
# BENCH_CLIENT_TOKEN contient un token client du serveur, fourni hors du dépôt.
export BENCH_URL=http://127.0.0.1:8080
export BENCH_DEVICE=esp32-local
python -m pytest bench/recipes/test_read_only.py --bench \
  --bench-results /qualification-runs/my-project \
  --junitxml=/qualification-runs/my-project/report.xml -v
```

Le navigateur doit avoir connecté la carte au même serveur. `--bench-url` et
`--bench-device` remplacent les variables correspondantes si nécessaire. Le token
reste uniquement dans `BENCH_CLIENT_TOKEN`, jamais dans les arguments ou le fichier
de recette. Installer le paquet active la découverte du plugin pytest.

Sans `--bench`, les tests marqués `bench` et ceux utilisant la fixture `board` sont
**SKIP**, sans accès matériel. Avec `--bench` mais sans token, la préparation échoue.
La recette fournie cible une S3 : identification, deux lectures de 4 Kio à `0x10000`,
vérification des tailles et SHA-256 des artefacts puis comparaison binaire. Elle ne
programme ni n’efface ; les opérations chargeur peuvent cependant redémarrer la carte.
Cette recette ne prouve pas la validité de la table de partitions ou du firmware.

## Recettes des projets

```python
import pytest

pytestmark = pytest.mark.bench


def test_flash_read_is_repeatable(board):
    assert board.identify()["chip"] == "esp32s3"
    first = board.read_flash(address=0x10000, size=4096)
    second = board.read_flash(address=0x10000, size=4096)
    assert first == second
```

Chaque test reçoit sa propre réservation exclusive et une cible liée à une génération
et un epoch serveur. Le client vérifie la cible avant les commandes et après leurs
résultats. Le serveur révoque les réservations lors d’une déconnexion ; une reconnexion
n’autorise donc jamais le client à continuer avec sa réservation précédente.

`board.command(name, args, timeout=120, start_timeout=30)` permet les autres commandes
publiées par l’API. Les helpers initiaux sont `identify()`, `partitions(address=...)`,
`read_flash(address=..., size=..., timeout=600)`, `cursor()`, `events(cursor)`,
`wait_uart(pattern, cursor=..., timeout=...)`, `renew(ttl=...)` et `close()`.
`bench_client` expose aussi `devices()`, `device(id)`, `health()` et `download(artifact)`.

Pour observer des octets émis par une opération, prendre le curseur **avant** :

```python
cursor = board.cursor()
# Effectuer ici l’action explicitement prévue par le projet.
board.wait_uart(b"READY", cursor=cursor, timeout=10)
```

Sans curseur fourni, `wait_uart` observe uniquement les événements futurs. Les motifs
sont des octets, recherchés sur plusieurs fragments USB. Le client poll les lots REST
bornés et utilise `next`, pas `latest`, pour avancer ; il refuse toute rupture d’epoch,
de génération, déconnexion ou perte serveur/agent. Il ne décode pas des logs métier.
Il ne conserve pas automatiquement de capture UART brute.

## Délais et nettoyage

La réservation dure 900 s par défaut, réglable avec `--bench-lease-ttl` (1 à 3 600 s).
Il n’y a pas de renouvellement automatique : une longue recette appelle `board.renew`
explicitement. Les commandes bornent le délai de départ et le délai d’exécution selon
le contrat serveur ; chaque requête réseau a aussi un timeout de 10 s par défaut.
Le client attend le résultat **et** `settled` avant de rendre la commande terminée.

Après succès ou échec d’une assertion, la fixture libère sa réservation si le matériel
est stabilisé. Elle ne révoque pas un grant local et ne lève jamais une quarantaine.
Si le POST a pu être accepté mais que sa réponse est perdue, il n’est pas renvoyé.
Si une opération est indéterminée ou non stabilisée, le nettoyage signale une erreur
et conserve la réservation jusqu’à expiration plutôt que d’interrompre le matériel.
Les IDs connus sont conservés dans le client pour diagnostic. Un processus tué ne
peut pas assurer son teardown : la réservation reste bornée par son TTL.

La recette décide comment gérer un échec ; aucune commande ni campagne n’est rejouée
automatiquement. Exécuter les tests d’une carte séquentiellement. Une exécution
concurrente est refusée par le serveur, sans prise de force.

## Autorisations

Le token a besoin de `read` et `control`. Les règles distantes restent celles du banc.
Une commande sensible demande toujours le grant local adapté. Programmer/effacer exige
également `program`, `--bench-allow-destructive` et
`board.command(..., allow_destructive=True)` pour chaque commande concernée.
Aucun de ces opt-ins ne crée une autorisation dans le navigateur. La recette doit
indiquer explicitement les régions et obtenir les autorisations matérielles appropriées.
La recette de démonstration ne demande aucun de ces droits supplémentaires.

## Preuves et rapports

`--bench-results` contient un sous-dossier par exécution et par test : recette Python
exacte avec son SHA-256, métadonnées de cible/epoch/génération, diagnostics d’opérations
sans arguments ni tokens, dumps complets et leurs adresses, tailles et SHA-256.
Les dossiers créés ont des droits privés ; dumps et assertions peuvent contenir des
données sensibles. Le répertoire par défaut `.bench-runs` est ignoré dans ce dépôt ;
les projets consommateurs doivent également garder ces preuves hors Git.

pytest produit ses verdicts et, avec `--junitxml`, un rapport JUnit. Les versions du
firmware, préconditions métier et critères d’acceptation restent à documenter dans les
recettes de chaque projet. Les assertions, paramètres de test et rapports pytest ne
sont pas un filtre de secrets : ne pas y injecter des tokens ni des logs sensibles.

## Périmètre initial et validation

La commande globale ci-dessous est une **illustration de validation L3**.
Elle n'est pas requise après une recette nouvelle ou un correctif localisé :
suivre la [sélection L1/L2/L3/H](#sélection-des-tests-et-niveaux-de-validation).


Ce premier composant fonctionne en ligne de commande ; la page ne charge ni n’exécute
de fichiers Python. Les interventions opérateur, la pause/reprise durable et les
campagnes d’écriture/coupure ne sont pas implémentées. Ces étapes ne doivent pas être
remplacées par un PASS ou par une opération implicite.

```sh
python -m pip install -r bench/requirements.txt
python -m pip install -e './bench/python[pytest]'
python -m pytest bench/python/tests bench/recipes -q
```

Les tests démarrent un vrai serveur API et un agent RAM isolés, et lancent les recettes
avec pytest dans des processus séparés. Ils couvrent exclusion, scopes, artefacts,
libération après assertion échouée, absence de rejeu, attente de stabilisation, pertes
UART et verrouillage du destructif. Les tests matériels restent SKIP sans `--bench`.
Cette validation simulée ne qualifie aucune carte physique.
