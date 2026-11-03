# Banc matériel générique · API v1

Le navigateur détient le port USB avec Web Serial ; le serveur Python ne touche jamais au matériel. Les clients des projets soumettent des commandes par API. Le banc transporte des octets et des résultats matériels, sans interpréter les logs ni décider des verdicts de qualification.

Cette refonte part de **`bench/audit-final` (`9938b64`)**, qui contient entièrement `bench/rollback-preparation`, `bench/canonical-preparation` et `bench/p5c-preparation` (21 commits d’avance sur `main`). Aucune branche historique n’est supprimée. Les recettes et preuves OTA sont retirées de l’arbre livré ; elles restent consultables dans ces branches. Les dépôts consommateurs ne sont pas modifiés.

- [Architecture et audit](docs/architecture.md)
- [Contrat REST et WebSocket](docs/api.md)
- [Interface d’instrumentation et parcours de connexion](docs/interface.md)
- [Couverture de l’existant et validation matérielle restante](docs/coverage.md)
- [Client Python indépendant](examples/client.py)

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
    secrets.token_urlsafe(32): {'id': 'browser-local', 'role': 'agent', 'devices': ['esp32-local']},
}
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as f:
    json.dump(config, f, indent=2)
print('Credentials written to', path)
PY
BENCH_TOKENS_FILE=/tmp/bench-tokens.json python3 bench/server/bench_server.py
```

Le serveur écoute `127.0.0.1:8080`. Ouvrir `http://localhost:8080` sur le PC qui possède le port USB. Le bouton **Connecter un port série** ouvre Web Serial et permet d’observer la console locale sans token. Dans **Pilotage distant et accès…**, saisir le token agent, l’identifiant déclaré dans sa liste `devices` et le token client, puis ouvrir la session du banc. L’origine du serveur est automatiquement celle de la page. **Prendre le pilotage manuel** réserve la carte ; **Autoriser le pilotage distant** termine cette réservation en gardant le port ouvert. **Ces boutons passent par la même API et la même file que les clients externes.** Seuls le choix du port, l’identification initiale, la connexion, l’octroi local des droits et la récupération de sécurité sont locaux.

L’identifiant de carte est un nom logique choisi par l’opérateur ; la famille et la capacité flash sont mesurées par esptool, puis revérifiées avant les opérations flash. Deux ports physiques ne doivent pas être déclarés sous deux identifiants pour une même carte.

Pour programmer ou effacer : token client avec `program`, réservation, autorisation locale temporaire et `allow_destructive:true`. Les transmissions UART, BOOT, déclencheurs et requêtes HTTP locales exigent également une autorisation locale temporaire. Les tokens restent en mémoire de page ; ils ne sont pas conservés dans localStorage. Le seul état persistant du navigateur est le blocage de sécurité par identifiant de carte.

`BENCH_HOST`, `BENCH_PORT` et `BENCH_ORIGIN` configurent l’écoute et l’origine publique exacte derrière un proxy. Pour un serveur distant, utiliser HTTPS/WSS et un transfert local sécurisé ou un proxy TLS. Web Serial exige un contexte sécurisé reconnu par le navigateur. Le serveur ne fournit pas lui-même de certificat TLS. Le moteur charge **esptool-js 0.7.0** depuis unpkg ; ce chargement exige un accès Internet et garde la même dépendance navigateur que l’ancien banc.

## Utilisation sans matériel

Dans un deuxième terminal, définir `BENCH_AGENT_TOKEN` avec le token agent du fichier, puis :

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

## Tests logiciels

```sh
cd bench
npm install --ignore-scripts
npm test
npx playwright install --with-deps chromium
npm run test:browser
```

La CI exécute les tests Python, Node et Chromium. Les tests navigateur utilisent le véritable agent JavaScript et le WebSocket avec **un moteur matériel simulé**. Les tests du moteur esptool et du transport utilisent des doubles contrôlés. Les tests Python comprennent un cycle API complet : réservation, upload, programmation simulée, relecture, comparaison binaire et UART.

Le stockage serveur est **éphémère et borné** : 16 Mio par artefact, 64 Mio au total, 2 048 événements par carte, 4 096 opérations, rétention d’une heure pour les artefacts et résultats terminaux. Un redémarrage change `epoch` et supprime réservations, résultats et artefacts ; aucun travail n’est rejoué. Les clients doivent exporter les données nécessaires avant expiration. Le navigateur affiche uniquement un tampon UART limité et peut l’exporter manuellement.
