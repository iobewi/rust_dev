# Banc de qualification ESP32 (Web Serial)

C'est le banc qui a servi aux campagnes matérielles de `rust-mcu-libs` (#61, #68, #71) sur ESP32-S3.

Le conteneur n'accède jamais à la carte. **Le navigateur possède le port série** (Web Serial, esptool-js, ESP Web Tools) et relaie tout au conteneur par HTTP :
- les lignes de console ;
- les relectures flash.

Le conteneur, lui, sert les pages et les images, horodate la capture et pilote les resets.

```
navigateur (port UART) ── /api/log, /api/dump ──▶ bench_server.py ──▶ logs/serial.log, logs/dumps/
                       ◀── /api/cmd (reset, mark, arm) ── bench.sh / ready.sh
```

## Démarrage

```sh
python3 bench/server/bench_server.py        # :8080 ; remplace http-server sur ce port
```

Variables d'environnement :

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `BENCH_PORT` | 8080 | port HTTP (déjà redirigé par le devcontainer) |
| `BENCH_WEB` | `/workspace/web` | données servies : `manifest.json`, manifestes, images |
| `BENCH_LOGS` | `bench/logs` (côté serveur) | capture `serial.log` et `dumps/` (non versionnés), relectures servies sous `/dumps/` |

La page de `bench/web/` a priorité. Tout le reste vient de `BENCH_WEB`. Les outils ne devinent pas le chemin de la capture : ils le demandent au serveur (`/api/info`) et s'arrêtent si le fichier ou le repère est absent.

Les images, les manifestes de campagne et les secrets ne sont **jamais versionnés ici**. Cela vaut pour les identifiants Wi-Fi, le Bearer et la clé TLS : tout reste hors dépôt.

## Page du banc (`index.html`)

Une seule page et **une seule autorisation de port** pour toute la session. Choisissez le pont UART (CH343 sur la DevKitC-1) : le port USB-JTAG natif (VID 0x303a) ne pilote ni EN ni IO0.

Après un rechargement de la page, le port déjà autorisé est retrouvé sans sélecteur. Après un débranchement USB, la console se rouvre d'elle-même.

- **Console** : chaque ligne part dans `logs/serial.log`, préfixée `[uart]`. La console offre :
  - le reset EN (impulsion RTS, DTR relâché) ;
  - les repères `MARK` ;
  - l'exécution des commandes de `bench.sh` et `ready.sh` (reset, repère, reset déclenché sur une expression régulière).
- **Bascule console ↔ chargeur, sans resélectionner le port.** Toute opération flash (identifier, lire la table, relire, écrire, installer) ferme la console, connecte esptool-js en mode téléchargement, exécute l'opération, puis rouvre la console. Ce retour automatique peut être désactivé pour enchaîner plusieurs opérations ; on revient ensuite par « Retour console ».
  - Il n'y a pas de reset final : **le reset provoqué par la réouverture du port est le seul boot, capturé depuis la ROM**.
  - Les lignes d'esptool-js vont aussi dans la capture, préfixées `[esptool]`.
- **Identifier** : puce, flash ID, fabricant, capacité et MAC.
- **Table de partitions** : lecture et décodage, à 0x8000 par défaut, avec un bouton « Relire » par partition. Une région libre (nom, adresse, taille) peut aussi être relue.
  - Les relectures se font en lecture seule ; le résultat va dans `logs/dumps/<nom>.bin`, plus une copie horodatée dans `logs/dumps/archive/`.
  - Le SHA256 est affiché et consigné dans la capture.
- **Binaires disponibles** : liste actualisée toutes les 3 s des `*.bin` de `BENCH_WEB` et des relectures, avec taille, SHA256 et date, les plus récents en tête (surlignés pendant 10 min). Une nouvelle livraison apparaît donc sans recharger la page ni reconnecter le port.
  - **Adresse proposée** : celle du manifeste qui référence l'image, sinon le suffixe `-0x<adresse>` du nom (cas des relectures). Restaurer une sauvegarde revient donc à cocher la relecture puis « Écrire ».
  - **Écriture** : sans effacement global, sauf si la case est cochée, avec confirmation. Le navigateur revérifie le SHA256 de chaque image avant d'écrire.
- **Manifestes** : tout manifeste ESP Web Tools trouvé sous `BENCH_WEB` s'installe en un clic. Les parties absentes sont signalées. L'effacement complet n'a lieu que si la case est cochée, avec confirmation : `new_install_prompt_erase` ne fait que proposer ce choix et n'est jamais pris pour un ordre d'effacement.

## Pilotage à distance et droits d'accès

La page exécute les commandes que le serveur met en file d'attente. Il y en a deux sources :
- `tools/bench.sh` : reset, repère, reset déclenché ;
- `tools/benchctl.py` : opérations flash, qui attendent le résultat renvoyé par la page.

Ces commandes ne sont exécutées que si la page tient le port (mode console ou chargeur). Chacune porte une échéance : la page abandonne une commande expirée. Ainsi, une écriture laissée en attente pendant que l'onglet était fermé ne s'exécutera jamais tard.

| Niveau | Commandes à distance |
| --- | --- |
| **limité** (défaut) | `reset`, `mark`, `arm`/`disarm`, `console`, `identify`, `readpt`, `dump` : rien n'écrit la flash |
| **complet** (bouton dans la page) | en plus : `write`, `install` et l'option `--erase` |

L'accès complet ne s'accorde que **depuis la page**, après confirmation. Il expire au bout de 30 min et se révoque d'un clic. Chaque attribution, révocation, expiration ou refus est inscrit dans la capture.

À distance, une écriture n'accepte que les binaires listés par le serveur, avec le SHA256 annoncé ; le navigateur le revérifie avant d'écrire.

```sh
bench/tools/benchctl.py images c71                    # binaires livrés (sans la page)
bench/tools/benchctl.py identify
bench/tools/benchctl.py readpt
bench/tools/benchctl.py dump config 0x9000 0x40000    # → dumps/config-0x9000.bin (+ archive)
bench/tools/benchctl.py write c71/ota-mem/app.bin@0x20000          # accès complet
bench/tools/benchctl.py install manifest-ota68-factory.json --erase # accès complet
```

## Outils

- **`tools/benchctl.py`** : opérations flash à distance (voir ci-dessus).
- **`tools/bench.sh mark|since|reset|arm`** : repères et resets à travers la console. Sans onglet console connecté, les commandes sont perdues **sans erreur**.
- **`tools/ready.sh <repère>`** : attend que la carte serve de nouveau après le **dernier** boot qui suit le repère.
  - Prête signifie : `READY_RE` vu (défaut `OTA ready`) **et** `/health` qui répond 200, en TLS vérifié (`BENCH_CA`, `BENCH_OTA_HOST`).
  - Si aucun boot n'a eu lieu depuis le repère, le script attend sans jamais resetter.
  - Si la connexion Wi-Fi échoue, il fait un reset EN, au plus 5 fois.
- **`tools/ota_inject.py`** : requêtes d'injection pour l'exemple OTA A (mauvais digest, transfert tronqué, image non amorçable, jeton absent ou faux, activation). Le jeton est lu dans `IOBEWI_OTA_TOKEN`, la CA est passée par `--ca` ou `BENCH_CA`.

## Sonde mémoire (`probe/qual_mem.rs`)

Une sonde de **qualification seulement**, à ne jamais fusionner :
- **tas** : statistiques esp-alloc, avec la feature `internal-heap-stats` ;
- **pile** : haute eau de la pile principale cpu0, obtenue en peignant la pile au démarrage.

Pour l'utiliser :
1. Incluez-la par `#[path = "…/qual_mem.rs"] mod qual_mem;`.
2. Appelez `qual_mem::paint()` en tête de `main`, avant `esp_hal::init`.
3. Appelez `qual_mem::probe(target, "point")` aux points voulus.
4. Ajoutez `internal-heap-stats` aux features d'esp-alloc.

Limites :
- Les piles des tâches radio, allouées dans le tas, ne sont pas couvertes.
- Les valeurs sont des maxima cumulés depuis le boot.
- Les résultats sont à publier comme « instrumentés », séparés de ceux du code nominal.

## Leçons du banc

- Ouvrir le port fait un reset (circuit auto-programme). Pour capturer un premier boot, n'ajoutez pas d'impulsion EN : elle couperait ce boot.
- Ne vous fiez jamais à une ligne « ready » d'un boot précédent : vérifiez ce qui suit le dernier `rst:`, plus `/health`.
- Un outil qui resette pendant une écriture invalide l'essai : `ready.sh` ne resette jamais sans boot observé.
- Masquez les identifiants (SSID, etc.) avant de joindre une capture.
- Chrome bride les minuteurs des onglets en arrière-plan, jusqu'à un réveil par minute. La page n'en dépend donc pas : elle envoie chaque ligne dès qu'elle arrive, et attend les commandes par une requête longue (`/api/cmd?wait=25`). La capture et les commandes restent ainsi immédiates, même quand l'onglet est caché.
