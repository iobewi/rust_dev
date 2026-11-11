# IOBEWI Device Lab · v1.0.0

*Contrôle et qualification de systèmes embarqués.* Une seule fenêtre de navigateur pilote **plusieurs cartes** par Web Serial ; un serveur Python expose ces cartes par une API REST/WebSocket à des clients externes (scripts, pytest, assistants). Le navigateur détient les ports USB ; le serveur ne touche jamais au matériel. Le banc transporte des octets et des résultats matériels, sans interpréter les logs ni décider des verdicts de qualification : ceux-ci appartiennent aux projets clients.

## Fonctionnalités de la v1

- Une fenêtre, plusieurs cartes Web Serial : un agent, un moteur matériel et un contexte indépendants par carte.
- Liste permanente **Cartes actives** : cartes connectées ou précédemment connectées dans la fenêtre, avec leur état USB ; ajout, sélection, déconnexion, reconnexion et retrait carte par carte.
- Zones **Identification** et **Commandes de la carte** liées à la carte sélectionnée ; consoles, opérations, journaux et historiques distincts par carte.
- Isolation par carte des consoles, opérations, réservations, grants et quarantaines. Changer de carte sélectionnée ne retarget jamais une opération en cours et ne transfère aucun droit.
- API REST/WS pour des clients externes selon leurs scopes (`read`, `control`, `program`) : réservation, file d'opérations, événements SSE, artefacts.
- Journal durable de récupération : une opération incertaine n'est jamais rejouée ni déclarée terminée sans preuve.
- Client Python installable, plugin pytest et recettes de qualification génériques (voir [pytest](docs/pytest.md)).

## Architecture

```text
Navigateur (PC de l'opérateur)           Serveur Python (aiohttp)        Clients externes
┌─────────────────────────────┐ WS/HTTP ┌─────────────────────────┐ REST/SSE ┌────────────────┐
│ Carte A : SerialPort+moteur ├─agent A─▶                         ◀──────────┤ scripts, pytest │
│ Carte B : SerialPort+moteur ├─agent B─▶ réservations, file,      │  token   │ Claude, CI      │
│ (Web Serial, esptool-js)    │ session │ grants, journal durable │          └────────────────┘
└─────────────────────────────┘ locale  └─────────────────────────┘
```

Le navigateur est l'**unique propriétaire USB** (Web Serial). Chaque carte y possède son agent WebSocket, authentifié par la session locale et limité à l'identifiant logique autorisé. Le serveur est le courtier des opérations : une seule instance est autoritaire pour un ensemble de cartes. Détails : [architecture](docs/architecture.md), [contextes multicarte](docs/multi-board.md), [contrat API](docs/api.md), [interface](docs/interface.md).

## Prérequis

- **Poste de l'opérateur** : Chrome ou Edge récent (Web Serial). La page et les ports USB sont sur ce poste ; Firefox n'implémente pas Web Serial.
- **Serveur** : Python 3.12+ et `aiohttp` (`requirements.txt`). Node 22+ seulement pour les tests.
- **Accès Internet du navigateur** : le moteur charge `esptool-js 0.7.0` depuis `unpkg.com` (dépendance externe non embarquée).
- **Cartes qualifiées** : ESP32-S3 (pont UART externe et USB Serial/JTAG). Voir [Restrictions et limites](#restrictions-et-limites).

## Installation

Depuis la racine d'un clone du dépôt ou de l'archive de la release :

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r bench/requirements.txt
# Optionnel : client Python et recettes pytest
pip install -e './bench/python[pytest]'
```

Créer le dossier d'état persistant privé et un fichier de tokens **hors dépôt** (valeurs aléatoires, droits `0600`) :

```sh
export BENCH_STATE_DIR="$HOME/.local/state/iobewi-bench"
mkdir -p -m 700 "$BENCH_STATE_DIR"
python3 - <<'PY'
import json, os, secrets
from pathlib import Path
path = Path(os.environ['BENCH_STATE_DIR']) / 'tokens.json'
config = {secrets.token_urlsafe(32): {'id': 'operator', 'role': 'client', 'scopes': ['read', 'control', 'program']}}
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as f:
    json.dump(config, f, indent=2)
print('Credentials written to', path)
PY
```

Ce fichier n'est créé qu'une fois (`O_EXCL`) : ne régénérez pas les tokens d'une instance existante. L'usage de la page web seule ne demande aucun token.

## Démarrage

```sh
BENCH_TOKENS_FILE="$BENCH_STATE_DIR/tokens.json" python3 bench/tools/run_server.py
```

Le serveur refuse de démarrer sans `BENCH_TOKENS_FILE` et écoute par défaut sur `127.0.0.1:8080`. `GET /health` indique l'état, l'`epoch`, le mode durable et la version. Ouvrir `http://127.0.0.1:8080` **sur le PC qui possède les ports USB** ; le parcours pas à pas est dans l'[interface](docs/interface.md#parcours-minimal). La session locale et l'autorisation Web Serial sont propres à l'origine : après un changement d'adresse, recharger la page et reconnecter les ports.

Avec le transfert de port de VS Code sous Windows et Firefox, `http://localhost:8080` a servi la page mais son WebSocket `/agent/v1/ws` est resté en `CONNECTING` (agent jamais enregistré), alors que `http://127.0.0.1:8080` fonctionne ; la cause n'est pas établie et aucun contrôle d'origine, de session ou de CSP n'est assoupli.

## Ajout et sélection de plusieurs cartes

Dans **Cartes actives**, choisir un identifiant logique libre, cliquer **Ajouter une carte**, puis choisir le port dans le sélecteur Web Serial du navigateur : aucune association automatique par VID/PID. Répéter pour chaque carte. **Cliquer sur le nom** d'une carte la sélectionne (surbrillance) sans rien envoyer ni reconnecter ; Identification, Commandes, consoles et opérations suivent la carte sélectionnée. Une même carte physique ne doit pas être déclarée sous deux identifiants ; un identifiant ou un port déjà utilisé est refusé.

## Connexion et reconnexion USB

Le **statut** de chaque carte est un bouton. **Connectée** déconnecte uniquement ce port (confirmation si une réservation, un grant ou un blocage existe ; refus pendant une opération). Après une déconnexion volontaire, l'entrée reste visible en **Déconnectée** ; **Retirer** l'oublie dans cette fenêtre sans supprimer les preuves serveur. Après un câble débranché : **USB perdu** puis **Reconnexion en attente** ; seul le port précédemment choisi est repris, après revérification de la puce, de la capacité flash, du RDID et du MAC. Annuler la reconnexion ne concerne que cette carte. Aucune opération interrompue n'est rejouée.

## Restrictions et limites

- La v1 est qualifiée sur deux ESP32-S3 ; les résultats matériels (M40) et leurs limites sont dans [multi-board](docs/qualification-multi-board.md) et la [couverture](docs/coverage.md). Un PASS simulé n'est jamais un PASS matériel ; certaines recettes restent partielles ou non exécutées.
- Un seul serveur autoritaire ; pas de répartition de charge ni de haute disponibilité.
- Le navigateur doit rester ouvert et actif : un onglet masqué ou un poste en veille peut interrompre un agent (Wake Lock demandé, sans garantie).
- Programmation et effacement exigent confirmation locale, scope `program`, réservation et grant.
- Pas d'authentification utilisateur pour l'interface web (voir ci-dessous).

## Déploiement et sécurité

- **Usage local** : serveur sur `127.0.0.1`, page sur `http://127.0.0.1:8080` ou `http://localhost:8080`. La page ouvre une session éphémère (cookie HttpOnly, SameSite Strict, 8 h) uniquement depuis la boucle locale, avec contrôle du pair TCP, du Host et de l'origine exacte.
- **Contexte sécurisé Web Serial** : le navigateur exige HTTPS ou `localhost`/`127.0.0.1`. Le serveur ne fournit pas de TLS.
- **Accès distant** : par un transfert de port local sécurisé (SSH, VS Code, devcontainer) déclaré avec `BENCH_ORIGIN` et `BENCH_UI_FORWARD_PEERS`, ou un proxy TLS sous votre responsabilité. Les en-têtes `Forwarded` et `X-Forwarded-*` ne déterminent jamais l'identité de confiance.
- **L'interface n'a pas d'authentification générale** : **ne pas exposer le serveur directement sur Internet ni sur un réseau non maîtrisé**. Un accès LAN ou DNS arbitraire n'ouvre aucune session privilégiée.
- **Clients API** : un token distinct par client dans le fichier de tokens, avec le minimum de scopes. Le navigateur ne reçoit jamais ces tokens. Les opérations sensibles distantes exigent en plus réservation, consentement et grant local temporaire, jamais créé par un client distant.
- **Hors Git** : fichier de tokens, `BENCH_STATE_DIR` (journal de récupération : ne jamais le supprimer ni le publier), sessions de qualification, preuves, dumps flash et journaux UART (qui peuvent contenir des secrets du firmware).
- **Dépendance navigateur** : `esptool-js 0.7.0` chargé depuis `unpkg.com`.

Règles détaillées : [reliability](docs/reliability.md).

## Version, distribution et retour arrière

[Notes de version v1.0.0](docs/release-v1.0.0.md) : contenu, inventaire de distribution, installation vérifiée, retour à la version précédente.

## Arrêt et redémarrage coordonnés

Le lanceur reste au premier plan et affiche son PID ainsi que celui de son serveur enfant. Envoyer `SIGHUP` **au PID du lanceur affiché** demande un redémarrage ; `SIGTERM`, `SIGINT` ou Ctrl+C demandent l’arrêt. Le lanceur attend et récolte la fin effective de son propre enfant avant tout remplacement. Il ne recherche ni ne tue d’autres serveurs et ne redémarre pas automatiquement après un crash.

`--shutdown-timeout` borne l’attente d’arrêt (30 s par défaut). Côté serveur, les WebSockets d’agent sont fermés (code 1001) et les flux d’événements terminés dès le signal d’arrêt, avant l’attente aiohttp ; `BENCH_SHUTDOWN_TIMEOUT` (5 s par défaut, 0 à 25 s) borne l’attente des autres connexions. aiohttp applique ce délai deux fois (attente puis annulation) : la somme doit rester inférieure à `--shutdown-timeout` du lanceur. Si elle expire, le lanceur sort avec le code 124, indique le PID encore vivant et refuse de démarrer un remplaçant ; aucun SIGKILL n’est envoyé. Inspecter ce processus localement avant de reprendre. `--lock-timeout` borne l’attente du verrou au démarrage (10 s par défaut). L’attente, l’acquisition ou le refus sont affichés explicitement.

Le démarrage direct par `bench/server/bench_server.py` reste disponible ; `BENCH_LOCK_TIMEOUT` y configure l’attente du verrou (0 s par défaut). **Le verrou et le journal ne sont jamais supprimés pour débloquer un redémarrage.** Une seconde instance ne peut pas devenir écrivain tant que la première détient le verrou. Un redémarrage change l’epoch et conserve les règles de récupération ; le lanceur ne rejoue aucune commande.

`BENCH_HOST`, `BENCH_PORT` et `BENCH_ORIGIN` configurent l’écoute et l’origine publique exacte derrière un proxy. Pour un serveur distant, utiliser HTTPS/WSS et un transfert local sécurisé ou un proxy TLS (voir « Déploiement et sécurité »).

## Utilisation sans matériel

Pour cet agent simulé externe seulement, ajouter au fichier un token aléatoire de rôle `agent`, avec `devices:["esp32-local"]`, puis redémarrer le serveur. Ce token n’est pas nécessaire à la page Web. Dans un deuxième terminal, définir `BENCH_AGENT_TOKEN` avec cette clé, puis :

```sh
python3 bench/tools/sim_agent.py --device esp32-local --grant 1800
```

Ce simulateur a une flash RAM de 4 Mio, une réponse UART déterministe, et **ne représente aucune qualification ESP32**. L’option `--grant` autorise explicitement les opérations privilégiées simulées ; elle vaut zéro par défaut.

Dans un troisième terminal, définir `BENCH_CLIENT_TOKEN`, puis :

```sh
python3 bench/examples/client.py --device esp32-local
# Écriture/relecture explicite, à utiliser sur une région appropriée à la cible :
python3 bench/examples/client.py --device esp32-local --image /path/to/image.bin \
  --address 0x10000 --chip esp32s3 --allow-destructive
```

Les captures durables, assertions et rapports appartiennent au client. `BenchClient.events()` offre un abonnement SSE ; chaque lot donne `epoch`, `next`, `lost` et les événements. Les réceptions UART restent des octets base64, pas des lignes décodées par le serveur. Vérifier aussi `source_lost` et les événements de déconnexion. Le journal UART peut contenir des secrets émis par le firmware ; il n’est accessible qu’aux tokens `read`.

## Devcontainer et redirection de port locale

Le navigateur reste sur le PC de l’opérateur : il ouvre l’interface servie par le
conteneur et possède seul le port USB via Web Serial. Le devcontainer ne lance
aucun navigateur, n’installe pas Chromium/Playwright et ne demande aucun accès
aux périphériques USB du PC. Le client Python installé dans le conteneur sert aux
recettes pytest et communique uniquement avec l’API du serveur. Les tests
navigateur simulés restent dans la CI logicielle existante, hors devcontainer.

Le projet est monté explicitement dans `/workspace` avec `workspaceMount`.
Deux volumes Docker nommés sont créés automatiquement : `iobewi-bench-state`
conserve le journal du banc, `iobewi-claude-state` conserve la configuration
Claude dans `/root/.claude`. Aucun dossier `.claude` préexistant sur le PC n’est
requis. `CLAUDE_CONFIG_DIR` dirige aussi sa configuration globale vers ce volume.
Ce nouveau volume ne reprend pas automatiquement la configuration d’un
ancien bind mount ; configurer Claude une première fois dans le conteneur si
nécessaire. Les reconstructions suivantes conservent les deux volumes.

Les versions sont fixées : Python 3.12.16, Node 22.23.4, Rust 1.99.1, Rust Xtensa
1.97.0.0, espup 0.19.0, espflash 4.7.0, esp-generate 2.0.0 et Claude Code 2.1.318.
Les exécutables ESP officiels sont contrôlés par SHA-256 avant installation ;
leurs versions et empreintes sont dans `docker/esp-tools.json`. Les paquets
système Debian et dépendances transitives non verrouillées continuent d’évoluer :
ceci ne garantit pas une image identique octet pour octet à chaque reconstruction.

Depuis VS Code, utiliser **Dev Containers: Rebuild and Reopen in Container**.
Le hook de création vérifie les outils et installe seulement le client Python
depuis le workspace, sans télécharger de navigateur. Le serveur du banc reste à
démarrer explicitement avec son fichier de configuration privé.

La CI `Devcontainer validation` construit et ouvre la configuration réelle avec
la CLI Dev Containers 0.90.0, vérifie les outils et les tests logiciels sans
navigateur, puis recrée le conteneur pour vérifier la persistance des deux
volumes. Ces contrôles n’accèdent à aucune carte physique.

L’écoute `0.0.0.0` dans le conteneur est compatible avec la page ouverte sur `localhost` du PC. Déclarer l’origine publique exacte et l’adresse du relais telle que le serveur la voit :

```sh
BENCH_HOST=0.0.0.0 \
BENCH_ORIGIN=http://localhost:8080 \
BENCH_UI_FORWARD_PEERS=172.18.0.1 \
BENCH_TOKENS_FILE=/path/to/bench-tokens.json \
python3 bench/tools/run_server.py
```

`172.18.0.1` est un exemple à remplacer par le pair réel du relais ; ce n’est pas une adresse présumée de votre devcontainer. `BENCH_UI_FORWARD_PEERS` accepte des adresses ou CIDR séparés par des virgules ; préférer l’adresse précise du relais. `BENCH_ORIGIN` est l’URL `localhost` visible dans le navigateur, qui peut avoir un port différent du port interne. Aucun nouveau token n’est demandé dans la page.

Le serveur vérifie le pair TCP réel et l’origine publique configurée. Les en-têtes `Forwarded` et `X-Forwarded-*` peuvent traverser le relais, mais ne déterminent jamais l’identité de confiance, l’origine attendue ou le caractère Secure du cookie. Sans cette configuration, le mode direct conserve ses contrôles. La redirection déclarée doit être réservée à la boucle locale du PC ; ce mode ne fournit pas d’authentification pour une publication LAN ou publique.

## Tests logiciels

```sh
cd bench
npm ci --ignore-scripts
npm test
npx playwright install --with-deps chromium
npm run test:browser
```

La CI exécute les tests Python, Node, le client/les fixtures pytest et Chromium. Les recettes pytest sont un composant client séparé : le serveur ne décide toujours aucun verdict de projet. Les tests navigateur utilisent le véritable agent JavaScript et le WebSocket avec **un moteur matériel simulé**. Les tests du moteur esptool et du transport utilisent des doubles contrôlés. Les tests Python comprennent un cycle API complet : réservation, upload, programmation simulée, relecture, comparaison binaire et UART.

Le stockage serveur est **borné** : 16 Mio par artefact, 64 Mio au total, 2 048 événements par carte, 4 096 opérations et rétention habituelle d’une heure pour les artefacts/résultats terminaux qui ne sont pas protégés. Le serveur lancé par le CLI conserve son état dans le journal durable : un redémarrage change `epoch`, invalide les anciennes sessions, réservations et grants, conserve les résultats connus et les artefacts retenus, et place les opérations incertaines en récupération explicite. Aucun travail n’est rejoué. Les instances volatiles sont réservées notamment aux simulations ; `/health` distingue les deux modes. Les clients doivent conserver leurs propres preuves privées. Le navigateur affiche uniquement un tampon UART limité et peut l’exporter manuellement.

## Fiabilisation et requalification

- [Nouveaux contrats : consentement, stockage, journal durable, récupération, provenance et manifeste](docs/reliability.md)
- [Protocole matériel S3/C3 après correction — non exécuté](docs/requalification-reliability.md)

Le serveur exécutable conserve son état de récupération dans `BENCH_STATE_DIR`
(volume privé persistant). `/health` indique si le serveur est durable ou volatile.
Les interruptions n’autorisent aucun rejeu automatique. La qualification matérielle
reste à exécuter explicitement sur chaque transport disponible.

## Documentation

- [Architecture](docs/architecture.md) · [Contextes multicarte](docs/multi-board.md) · [Contrat REST et WebSocket](docs/api.md)
- [Interface et parcours utilisateur](docs/interface.md)
- [Couverture et validation matérielle restante](docs/coverage.md)
- [Recettes pytest et client Python](docs/pytest.md) · [Méthode de qualification](docs/qualification-workflow.md) · [Démarrage d'une session Claude](docs/session-startup.md)
- [Exemple de client Python asynchrone](examples/client.py)
