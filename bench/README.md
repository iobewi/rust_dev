# Banc matériel générique · API v1

Le navigateur détient le port USB avec Web Serial ; le serveur Python ne touche jamais au matériel. Les clients des projets soumettent des commandes par API. Le banc transporte des octets et des résultats matériels, sans interpréter les logs ni décider des verdicts de qualification.

Cette refonte part de **`bench/audit-final` (`9938b64`)**, qui contient entièrement `bench/rollback-preparation`, `bench/canonical-preparation` et `bench/p5c-preparation` (21 commits d’avance sur `main`). Aucune branche historique n’est supprimée. Les recettes et preuves OTA sont retirées de l’arbre livré ; elles restent consultables dans ces branches. Les dépôts consommateurs ne sont pas modifiés.

- [Architecture et audit](docs/architecture.md)
- [Contrat REST et WebSocket](docs/api.md)
- [Interface d’instrumentation et parcours de connexion](docs/interface.md)
- [Couverture de l’existant et validation matérielle restante](docs/coverage.md)
- [Recettes pytest et client Python installable](docs/pytest.md)
- [Méthode : rédaction, revue croisée et exécution des qualifications](docs/qualification-workflow.md)
- [Début d'une nouvelle session Claude et utilisation du banc](docs/session-startup.md)
- [Exemple historique de client Python asynchrone](examples/client.py)

## Démarrer

Python 3.12+, Node 22+ pour les tests. Une seule instance de serveur est autoritaire pour un ensemble de cartes ; pas de répartition de charge entre processus.

```sh
python3 -m venv /tmp/bench-venv
. /tmp/bench-venv/bin/activate
pip install -r bench/requirements.txt
```

Créer un fichier de tokens **hors dépôt**, avec des valeurs aléatoires distinctes :

```sh
python3 - <<'PY'
import json, os, secrets
from pathlib import Path
path = Path('/tmp/bench-tokens.json')
config = {
    secrets.token_urlsafe(32): {'id': 'operator', 'role': 'client', 'scopes': ['read', 'control', 'program']},
}
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as f:
    json.dump(config, f, indent=2)
print('Credentials written to', path)
PY
BENCH_TOKENS_FILE=/tmp/bench-tokens.json python3 bench/tools/run_server.py
```

Le serveur écoute `127.0.0.1:8080`. Ouvrir `http://localhost:8080` sur le PC qui possède le port USB. La page établit automatiquement une session locale, sans token à saisir. **Connecter un port série** ouvre le sélecteur Web Serial et rattache automatiquement la carte au serveur. Cliquer ensuite directement sur Identifier, Lire, Reset ou les autres commandes : le serveur prend et libère la réservation pour chaque opération, après stabilisation du retour console. **Tous ces boutons utilisent la même API et la même file que les clients externes.** Programmation et effacement conservent une confirmation explicite ; les actions locales ne créent aucun grant pour les clients distants. Le panneau avancé sert à autoriser temporairement les opérations sensibles de Claude. Les clés de la configuration ne sont plus recopiées dans la page. La session automatique accepte l’accès direct en boucle locale et les redirections locales explicitement configurées ci-dessous ; l’authentification d’une UI par le réseau n’est pas implémentée.

L’identifiant de carte est un nom logique configuré pour le banc (`esp32-local` par défaut) ; la famille et la capacité flash sont mesurées par esptool, puis revérifiées avant les opérations flash. Deux ports physiques ne doivent pas être déclarés sous deux identifiants pour une même carte.

Pour les clients externes, programmer ou effacer exige un token avec `program`, une réservation, une autorisation locale temporaire et `allow_destructive:true`. Leurs transmissions UART, BOOT, armements de déclencheurs et requêtes HTTP exigent également un grant. Le désarmement explicite (`trigger`, `action:"disarm"`) reste possible après expiration ou révocation du grant, uniquement avec `control` et la réservation active qui a armé ce déclencheur dans la même génération. Il ne crée aucun nouveau droit. Les actions de la page sur son propre agent physique utilisent une autorité limitée à chaque opération et une confirmation destructive explicite, sans ouvrir ce grant distant. Le navigateur ne conserve aucune clé API ; sa session HttpOnly est éphémère. Le blocage de sécurité par identifiant de carte reste mémorisé localement.

### Arrêt et redémarrage coordonnés

Le lanceur reste au premier plan et affiche son PID ainsi que celui de son serveur enfant. Envoyer `SIGHUP` **au PID du lanceur affiché** demande un redémarrage ; `SIGTERM`, `SIGINT` ou Ctrl+C demandent l’arrêt. Le lanceur attend et récolte la fin effective de son propre enfant avant tout remplacement. Il ne recherche ni ne tue d’autres serveurs et ne redémarre pas automatiquement après un crash.

`--shutdown-timeout` borne l’attente d’arrêt (30 s par défaut). Côté serveur, les WebSockets d’agent sont fermés (code 1001) et les flux d’événements terminés dès le signal d’arrêt, avant l’attente aiohttp ; `BENCH_SHUTDOWN_TIMEOUT` (5 s par défaut, 0 à 25 s) borne l’attente des autres connexions. aiohttp applique ce délai deux fois (attente puis annulation) : la somme doit rester inférieure à `--shutdown-timeout` du lanceur. Si elle expire, le lanceur sort avec le code 124, indique le PID encore vivant et refuse de démarrer un remplaçant ; aucun SIGKILL n’est envoyé. Inspecter ce processus localement avant de reprendre. `--lock-timeout` borne l’attente du verrou au démarrage (10 s par défaut). L’attente, l’acquisition ou le refus sont affichés explicitement.

Le démarrage direct par `bench/server/bench_server.py` reste disponible ; `BENCH_LOCK_TIMEOUT` y configure l’attente du verrou (0 s par défaut). **Le verrou et le journal ne sont jamais supprimés pour débloquer un redémarrage.** Une seconde instance ne peut pas devenir écrivain tant que la première détient le verrou. Un redémarrage change l’epoch et conserve les règles de récupération ; le lanceur ne rejoue aucune commande.

`BENCH_HOST`, `BENCH_PORT` et `BENCH_ORIGIN` configurent l’écoute et l’origine publique exacte derrière un proxy. Pour un serveur distant, utiliser HTTPS/WSS et un transfert local sécurisé ou un proxy TLS. Web Serial exige un contexte sécurisé reconnu par le navigateur. Le serveur ne fournit pas lui-même de certificat TLS. Le moteur charge **esptool-js 0.7.0** depuis unpkg ; ce chargement exige un accès Internet et garde la même dépendance navigateur que l’ancien banc.

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
npm install --ignore-scripts
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
