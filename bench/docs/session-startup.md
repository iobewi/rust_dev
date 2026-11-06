# Nouvelle session Claude — démarrage et utilisation du banc

Cette procédure s'applique au serveur réel du banc. Les tests logiciels utilisent
leurs propres instances et simulateurs isolés. Ne redémarre pas le serveur réel
et ne connecte pas une carte pour travailler uniquement sur le code.

## 1. Inventaire sans effet sur les cartes

Depuis la racine du dépôt (`/workspace` dans le devcontainer) :

```sh
git status --short
git branch --show-current
git rev-parse HEAD
python --version
node --version
python -c 'import iobewi_bench; print(iobewi_bench.__file__)'
```

Vérifie que le client installé correspond au checkout utilisé. Le devcontainer
l'installe déjà en mode éditable à sa création. Hors devcontainer, prépare un
environnement Python 3.12+ isolé puis installe les dépendances selon `AGENTS.md`.
Pour une tâche de compilation Rust, source aussi `/opt/esp/export-esp.sh` si ce
fichier est présent ; le banc Python lui-même n'en dépend pas.

Réutilise les variables déjà configurées. Les valeurs par défaut suivantes sont
des conventions pour une instance locale ; elles ne remplacent jamais la
configuration privée d'une instance existante :

```sh
export BENCH_URL="${BENCH_URL:-http://127.0.0.1:8080}"
export BENCH_STATE_DIR="${BENCH_STATE_DIR:-$HOME/.local/state/iobewi-bench}"
export BENCH_TOKENS_FILE="${BENCH_TOKENS_FILE:-$BENCH_STATE_DIR/tokens.json}"
```

Dans le devcontainer, `BENCH_STATE_DIR` pointe vers le volume persistant.
Les variables d'environnement d'un ancien terminal ne sont pas nécessairement
présentes dans une nouvelle session : retrouve la configuration privée connue,
sans afficher son contenu. N'utilise jamais un nouveau dossier d'état pour
contourner un verrou ou une opération incertaine.

Lis `/health` sans authentification et sans effet matériel :

```sh
python - <<'PY'
import json, os
from urllib.request import urlopen
url = os.environ['BENCH_URL'].rstrip('/') + '/health'
with urlopen(url, timeout=3) as response:
    health = json.load(response)
print(json.dumps({k: health.get(k) for k in ('status','protocol','epoch','durability')}))
print('Server Git SHA:', (health.get('build') or {}).get('git_sha'))
PY
```

Un serveur prêt doit annoncer `status=ok`, `protocol=1` et, pour le serveur
réel lancé par le CLI, `durability=sqlite-full`. Le SHA du processus peut être
différent du checkout courant : le signaler, puis décider si un redémarrage est
nécessaire pour la tâche avant de toucher au matériel.

Réutilise une instance saine. Si `/health` répond mal, inspecte d'abord le
service ou processus existant. Une erreur HTTP, un échec d'authentification ou une
instance dégradée ne signifient pas que le port est libre. Si `BENCH_URL` vise un
serveur distant, ne démarre pas un serveur local pour masquer son indisponibilité.

## 2. Premier provisionnement uniquement

Si aucun serveur n'existe, aucun fichier de tokens n'est déjà configuré et aucune
configuration privée antérieure n'est à reprendre, crée une fois le fichier
ci-dessous. Si une instance ou un fichier existe déjà, ne régénère rien.

```sh
python - <<'PY'
import json, os, secrets
from pathlib import Path
path = Path(os.environ['BENCH_TOKENS_FILE']).expanduser()
path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
token = secrets.token_urlsafe(32)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    json.dump({token: {'id':'claude','role':'client',
                      'scopes':['read','control','program']}}, stream)
print('Private credentials file created; token not displayed.')
PY
```

Ce token ne donne aucun grant local et n'autorise aucune écriture implicite.
Avec cette configuration initiale sans token de rôle `agent`, la page propose
la carte logique `esp32-local`. Pour un banc déjà configuré avec plusieurs
identifiants, conserve sa configuration et ses noms existants ; ne change pas
les identifiants physiques/logiques pour faire passer un test. Le navigateur
local n'a pas besoin de token agent.

## 3. Lancer uniquement l'instance locale arrêtée

Vérifie qu'aucun ancien lanceur/serveur n'est encore actif, notamment si un
`launcher.pid` existe ou si le port répond sans fournir le bon `/health`.
Un PID conservé est un indice, pas une preuve d'identité : ne lui envoie pas de
signal sans vérifier à quel processus il correspond.

Depuis la racine, utilise un terminal persistant ou, si nécessaire, ce lancement
détaché. Ne répète pas cette commande à chaque session si le serveur tourne :

```sh
umask 077
mkdir -p "$BENCH_STATE_DIR"
nohup python bench/tools/run_server.py --shutdown-timeout 30 --lock-timeout 10 \
  >> "$BENCH_STATE_DIR/server.log" 2>&1 &
bench_launcher_pid=$!
printf '%s\n' "$bench_launcher_pid" > "$BENCH_STATE_DIR/launcher.pid"
```

Attends `/health` pendant au plus 30 secondes avec des lectures GET uniquement.
En cas d'échec, vérifie le processus et les diagnostics du lanceur ; ne lance
pas de boucle de redémarrage. Le verrou et la base SQLite doivent être conservés.
Les logs sont privés : extrais seulement le diagnostic utile, sans copier de
secrets ou de données matérielles dans un rapport public.

Les valeurs par défaut écoutent dans le conteneur sur `127.0.0.1:8080`.
Utilise le port transféré par VS Code vers `localhost` sur le PC. Si un relais
Docker nécessite `BENCH_HOST`, `BENCH_ORIGIN` ou `BENCH_UI_FORWARD_PEERS`, reprends
les valeurs explicitement configurées selon `bench/README.md`. Ne devine pas
l'adresse du relais et ne désactive pas les contrôles d'origine pour débloquer
une connexion. Ne publie pas automatiquement le service sur le LAN.

## 4. État API, puis connexion opérateur si nécessaire

Inspection avec le client officiel, sans réservation ni commande matérielle :

```sh
python - <<'PY'
import json, os
from pathlib import Path
from iobewi_bench import BenchClient
token = os.environ.get('BENCH_CLIENT_TOKEN')
if not token:
    config = json.loads(Path(os.environ['BENCH_TOKENS_FILE']).expanduser().read_text())
    candidates = [(key, who) for key, who in config.items() if who.get('role') == 'client']
    named = [(key, who) for key, who in candidates if who.get('id') == 'claude']
    candidates = named or candidates
    if len(candidates) != 1:
        raise SystemExit('Select the intended client via BENCH_CLIENT_TOKEN; do not display credentials.')
    token = candidates[0][0]
client = BenchClient(os.environ['BENCH_URL'], token)
print('API identity:', json.dumps(client.identity()))
for device in client.devices():
    print(json.dumps({k: device.get(k) for k in (
        'id','chip','flash_size','connected','generation','quarantined',
        'reservation','privileged_seconds')}))
PY
```

Si une tâche matérielle est demandée, l'opérateur ouvre l'interface sur son PC
avec son navigateur Web Serial et sélectionne le port correspondant. Vérifie
ensuite l'agent observé, la carte et son état ; ne réserve pas simplement pour
tester la disponibilité. Aucun navigateur ou port série n'est piloté dans Docker.

Absence de carte : annoncer « serveur prêt, agent/carte non connecté » et
continuer le travail logiciel indépendant. Carte en quarantaine, réservée par
un autre client ou opération incertaine : conserver cet état, inspecter les
preuves et demander le geste local nécessaire, sans prendre le contrôle de force.

## 5. Exécuter seulement la recette demandée

Applique d'abord [la méthode de qualification](qualification-workflow.md) :
recette versionnée dans une PR, revue croisée sur un SHA identifié, puis accord
pour la campagne avant tout nouvel essai matériel. La rédaction et les contrôles
logiciels n'exigent pas de carte. Les preuves privées ne remplacent pas le code
versionné des tests.

Les recettes pytest nécessitent `--bench`, `BENCH_URL`, l'identifiant observé
`BENCH_DEVICE` et `BENCH_CLIENT_TOKEN` dans l'environnement privé du processus.
Le token lu par le script d'inspection ci-dessus reste dans ce seul processus :
pour lancer pytest, injecte-le dans l'environnement du processus enfant sans
l'afficher, par exemple via `subprocess.run(..., env=...)`. Ne le passe pas sur
la ligne de commande et n'utilise pas `set -x`, `env` ou un dump d'environnement.

Lis la recette avant de l'exécuter : une recette dite « lecture seule » peut
entrer dans le loader ou redémarrer la carte. L'opt-in pytest n'est pas une
autorisation générale de reset, d'effacement ou de programmation.

Pour une campagne avec programmation : manifeste validé, secteurs autorisés,
sauvegarde privée vérifiée, consentement campagne et par commande, scopes et
grant local. Utilise `Campaign` et `Orchestrator`, pas une réimplémentation HTTP.
Prévois les durées, la relecture et la restauration ; après interruption, pas de
rejeu automatique. La clôture exige la révocation locale du grant et les contrôles
de restauration/réservations prévus. Les preuves vont dans un nouveau dossier
privé à chaque run, jamais dans une ancienne exécution réécrite.

Le bilan de session distingue serveur, agent navigateur, carte et campagne ; il
indique aussi les opérations encore pendantes. Laisse le serveur sain disponible
entre sessions, sauf arrêt ou redémarrage explicitement demandé.
