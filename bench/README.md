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
  - l'exécution des commandes de `bench.sh` et `ready.sh` (reset, repère, reset déclenché sur une expression régulière) ;
  - un **reset automatique** sur une ligne donnée, actif par défaut sur `Wi-Fi: connection to .* failed` : un premier join Wi-Fi raté laisse la carte injoignable (#72). Il est tracé, et suspendu après 5 resets consécutifs sans `OTA ready`. On le règle dans la page ou avec `benchctl.py autoreset on|off [REGEX]`. Le réglage survit à un rechargement de la page, et chaque chargement l'inscrit dans la capture (`page loaded: auto reset on|off`). Il faut le couper pour qualifier un firmware qui retente lui-même (#72).
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

## Fiabilité des opérations flash et des commandes

Rien de ce qui suit ne dépend d'une carte ; les tests sont dans `bench/tests` (`npm test`, ou `node --test tests/` puis
`python3 -m unittest discover -s tests -p "test_*.py"` depuis `bench/`).

**Transport** (`web/bench-transport.js`). esptool-js 0.7.0 abandonne `Transport.read()` quand son réveil `sleep(1)` tombe après
l'échéance, sans relire le tampon : dans un onglet bridé, une réponse arrivée à temps est déclarée absente, avec le message
« Serial data stream stopped: Possible serial noise or corruption » (qui signifie « aucun octet reçu », pas « bruit »). Le transport
du banc examine le tampon d'abord, se réveille à l'arrivée des octets, utilise une échéance monotone et distingue `no-response`,
`partial-packet`, `invalid-packet` et `closed`. Il ne rejoue rien.

**Résultat d'une écriture** (`web/flash-op.js`). Le chargeur efface avant d'acquitter `FLASH_DEFL_BEGIN` : dès que ce début (ou un
effacement complet) est envoyé, une réponse manquante ne prouve rien sur la flash.

| état | sens | `ok` |
|---|---|---|
| `completed` | transfert terminé, MD5 du chargeur égal (sauf `md5:false`) | oui |
| `failed` | rien d'envoyé qui modifie la flash (`flash:"unchanged"`), ou contenu connu différent (`content-mismatch`) | non |
| `indeterminate` | la flash a pu être effacée ou à moitié écrite, ou la vérification n'a pas répondu | non |

Un résultat porte `verification: {transfer, md5, sha256_readback}` ; `sha256_readback` vaut toujours `not-run` : la relecture
indépendante reste celle de la campagne. Après `indeterminate` : pas de retour console automatique (la réouverture du port réinitialise
la carte), `reset`/`console`/`tx`/`io0` refusés, état affiché dans l'en-tête de la page et levé **par l'opérateur seulement**, après
relecture. Les relectures et les écritures de restauration restent possibles. À chaque échec, les 60 derniers événements de la trace
(horloge monotone, visibilité de l'onglet, commandes du chargeur, délais, fermeture du port ; jamais de données) vont dans la capture
sous `[trace]`.

**Commandes** (`server/commands.py`). `Queued → Claimed → Running → Completed / Failed / Indeterminate`, ou `Expired`.
La page n'exécute qu'après l'accusé de réception accepté par le serveur ; une commande non commencée expire (ttl 120 s par défaut, `--ttl`)
et n'est jamais exécutée tard ; une commande prise sans accusé n'a certainement pas été exécutée ; un identifiant ne sert qu'une fois ;
une seule commande à la fois. Une commande `Running` n'est jamais interrompue ni expirée parce qu'un client HTTP est parti ; si une autre
instance de la page prend le relais elle devient `Indeterminate` (« démarrée, issue inconnue ») et n'est pas rejouée. Les événements
(op, état, raison) sont dans `logs/commands.jsonl`. Code de sortie de `benchctl.py` : 0 completed, 1 failed, 2 sans réponse (jamais exécutée),
3 indeterminate, 4 expired.

**Serveur.** L'inventaire ne tient aucun verrou partagé avec les commandes ni la capture. Un seul serveur par répertoire de capture
(`logs/.bench_server.lock`) : les serveurs `:8080` et `:8081` (`BENCH_LOGS=logs/usb-native`) sont deux bancs, pas des doublons.

**Inventaire.** Par défaut : les dossiers de `BENCH_INVENTORY_DIRS` (défaut `p91`) et les relectures courantes. `?scope=all` ajoute les
autres dossiers, `?archives=1` les copies archivées. Catégories : `artifact`, `backup`, `capture`, `archive`. Fichiers et empreintes
uniques sont comptés séparément ; deux fichiers de même SHA-256 restent deux entrées. Les JSON qui ne sont pas des manifestes ESP Web
Tools sont listés avec leur raison. Rien n'est déplacé ni supprimé.

## Pilotage à distance et droits d'accès

La page exécute les commandes que le serveur met en file d'attente. Il y en a deux sources :
- `tools/bench.sh` : reset, repère, reset déclenché ;
- `tools/benchctl.py` : opérations flash, qui attendent le résultat renvoyé par la page.

Ces commandes ne sont exécutées que si la page tient le port (mode console ou chargeur). Chacune porte une échéance : la page abandonne une commande expirée. Ainsi, une écriture laissée en attente pendant que l'onglet était fermé ne s'exécutera jamais tard.

| Niveau | Commandes à distance |
| --- | --- |
| **limité** (défaut) | `reset`, `mark`, `arm`/`disarm`, `autoreset`, `console`, `identify`, `readpt`, `dump` : rien n'écrit la flash |
| **complet** (bouton dans la page, durée limitée) | en plus : `write`, `install`, l'option `--erase`, et les commandes qui parlent à la carte : `tx`, `rxhex`, `io0` |

L'accès complet ne s'accorde que **depuis la page**, après confirmation, pour une durée choisie (30 min, 2 h ou 4 h). Il expire de lui-même et se révoque d'un clic. Chaque attribution, révocation, expiration ou refus est inscrit dans la capture.

À distance, une écriture n'accepte que les binaires listés par le serveur, avec le SHA256 annoncé ; le navigateur le revérifie avant d'écrire.

```sh
bench/tools/benchctl.py images c71                    # binaires livrés (sans la page)
bench/tools/benchctl.py identify
bench/tools/benchctl.py readpt
bench/tools/benchctl.py dump config 0x9000 0x40000    # → dumps/config-0x9000.bin (+ archive)
bench/tools/benchctl.py write c71/ota-mem/app.bin@0x20000          # accès complet
bench/tools/benchctl.py install manifest-ota68-factory.json --erase # accès complet
bench/tools/benchctl.py tx 494d50524f56...                          # accès complet : octets (hex) vers l'UART
bench/tools/benchctl.py rxhex on|off                                # accès complet : octets reçus journalisés en hex
bench/tools/benchctl.py io0 3600                                    # accès complet : GPIO0 (BOOT) tenu bas 3,6 s, carte en marche
```

## Outils

- **`tools/benchctl.py`** : opérations flash à distance (voir ci-dessus).
- **`tools/bench.sh mark|since|reset|arm`** : repères et resets à travers la console. Sans onglet console connecté, les commandes sont perdues **sans erreur**.
- **`tools/ready.sh <repère>`** : attend que la carte serve de nouveau après le **dernier** boot qui suit le repère.
  - Prête signifie : `READY_RE` vu (défaut `OTA ready`) **et** `/health` qui répond 200, en TLS vérifié (`BENCH_CA`, `BENCH_OTA_HOST`).
  - Si aucun boot n'a eu lieu depuis le repère, le script attend sans jamais resetter.
  - Si la connexion Wi-Fi échoue, il fait un reset EN, au plus `READY_RESETS` fois (5 par défaut). Avec `READY_RESETS=0`, il attend sans jamais resetter.
- **`tools/improv.py`** : client Improv Serial (`state`, `info`, `scan`, `wifi SSID [MOT_DE_PASSE]`), accès complet requis. Il émet par `benchctl tx` et relit les trames dans la capture (`rxhex`). Les identifiants peuvent venir de `IMPROV_SSID` / `IMPROV_PASSWORD` ; la page ne journalise que le **nombre** d'octets émis, jamais leur contenu. Un fichier d'environnement local (`bench/.improv.env`) est ignoré par git ; **ne jamais versionner d'identifiants**. Les lignes de console d'un firmware antérieur peuvent contenir le SSID : masquez-le avant de joindre une capture.
- **`tools/improv_serial.py`** : même client Improv pour un PC Windows branché directement sur la carte (pyserial : `py improv_serial.py COM7 wifi --env ...\.improv.env`) ; DTR et RTS sont tenus bas avant l'ouverture du port (sur l'USB-Serial-JTAG natif ils pilotent reset et mode de démarrage).
- **Port USB natif (0x303a, ex. Waveshare ESP32-S3 Zero)** : depuis le 15 octobre 2026 la page sert aussi les commandes distantes sur ce port. `esptool-js` le réinitialise lui-même (`UsbJtagSerialReset`) : `identify`, `readpt`, `dump`, `write`, `install`, `tx` et `rxhex` fonctionnent, et la carte redémarre sur la nouvelle image après l'écriture. `reset`, `arm` et `io0` restent refusés (pas de lignes EN ni IO0). Quand la carte devient un disque USB (OTG), le port disparaît de la page : un redémarrage de la carte est alors un geste manuel (débrancher/rebrancher ou RESET).
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
- Après un rechargement de la page, l'ancienne requête longue reste en attente sur le serveur. Pour qu'elle ne prenne pas, puis ne perde pas, une commande, le serveur vérifie que le client est toujours connecté avant de la lui remettre.
- Un repère n'est utile que s'il est vraiment écrit avant la suite : `bench.sh mark` attend donc l'accusé de la page.
