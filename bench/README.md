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
}
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as f:
    json.dump(config, f, indent=2)
print('Credentials written to', path)
PY
BENCH_TOKENS_FILE=/tmp/bench-tokens.json python3 bench/server/bench_server.py
```

Le serveur écoute `127.0.0.1:8080`. Ouvrir `http://localhost:8080` sur le PC qui possède le port USB. La page établit automatiquement une session locale, sans token à saisir. **Connecter un port série** ouvre le sélecteur Web Serial et rattache automatiquement la carte au serveur. Cliquer ensuite directement sur Identifier, Lire, Reset ou les autres commandes : le serveur prend et libère la réservation pour chaque opération, après stabilisation du retour console. **Tous ces boutons utilisent la même API et la même file que les clients externes.** Programmation et effacement conservent une confirmation explicite ; les actions locales ne créent aucun grant pour les clients distants. Le panneau avancé sert à autoriser temporairement les opérations sensibles de Claude. Les clés de la configuration ne sont plus recopiées dans la page. Cette session automatique exige un accès direct en boucle locale ; l’authentification d’une UI par le réseau n’est pas implémentée.

L’identifiant de carte est un nom logique configuré pour le banc (`esp32-local` par défaut) ; la famille et la capacité flash sont mesurées par esptool, puis revérifiées avant les opérations flash. Deux ports physiques ne doivent pas être déclarés sous deux identifiants pour une même carte.

Pour les clients externes, programmer ou effacer exige un token avec `program`, une réservation, une autorisation locale temporaire et `allow_destructive:true`. Leurs transmissions UART, BOOT, déclencheurs et requêtes HTTP exigent également un grant. Les actions de la page sur son propre agent physique utilisent une autorité limitée à chaque opération et une confirmation destructive explicite, sans ouvrir ce grant distant. Le navigateur ne conserve aucune clé API ; sa session HttpOnly est éphémère. Le blocage de sécurité par identifiant de carte reste mémorisé localement.

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
