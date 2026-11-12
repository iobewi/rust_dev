# ESP32-P4 ECO7 : étude du chemin ROM (sans stub), esptool 5.6.0

**Statut : étude documentaire. Aucune commande matérielle lancée, esptool n'est pas installé dans le devcontainer, aucun port n'a été ouvert.** Sources : esptool `v5.6.0` (commit `5739c8454d25a3fdb7275b9734e55e56a3a73659`, 2026-10-15, dernière version officielle publiée), lues sur GitHub : `esptool/loader.py`, `cmds.py`, `__init__.py`, `targets/esp32p4.py` et la documentation `advanced-options.rst`. Les numéros de ligne renvoient à ce tag.

## 1. Conclusion principale

**L'objectif « deux lectures indépendantes de `0x2000`/4096 octets par le chargeur ROM, sans stub » n'est pas réalisable avec esptool 5.6.0 sur l'ESP32-P4.** `read-flash --no-stub` échoue : `ESPLoader.read_flash` appelle `read_flash_slow` quand le stub n'est pas chargé (`loader.py:1691-1693`), et cette méthode lève `NotImplementedInROMError` (`loader.py:1688-1689`). Seul `targets/esp32.py` (ESP32 classique) la surcharge ; `esp32p4.py` ne le fait pas. Le ROM du P4 ne fournit donc aucune lecture de flash par octets via esptool.

Ce qui existe dans le ROM : `SPI_ATTACH`, `SPI_SET_PARAMS`, **`SPI_FLASH_MD5`** (`flash_md5sum`, décorée « stub ou ROM ESP32 et suivants », `loader.py:1621-1638`, réponse de 32 caractères ASCII en ROM contre 16 octets bruts avec le stub), la lecture d'identifiant, du registre d'état et du SFDP, et la commande générique `run_spiflash_command`.

## 2. Réponses aux six points demandés

| # | Question | Constat (esptool 5.6.0) |
|---|---|---|
| 1 | `--no-stub` sur P4 ECO7 | Option globale : « only talk to ROM bootloader. Some features will not be available » (`__init__.py:436`). Le P4 se connecte ; `power_on_flash` (voir 5) est exécuté dans `_post_connect`. Les commandes réservées au stub échouent avec `NotImplementedInROMError` |
| 2 | `read-flash` en ROM | **Non pris en charge sur P4** (section 1). `verify-flash` fonctionne sans `--diff` : il compare le MD5 calculé par le ROM à celui du fichier local (`cmds.py:2677`) ; avec `--diff`, il tente `read_flash` et échoue en ROM |
| 3 | `--spi-connection` | **Non pris en charge sur P4** : `CUSTOM_SPI_FLASH_PINS_SUPPORTED = False` (`esp32p4.py:23`) ; une liste de broches lève `NotSupportedError`, et `HSPI` est réservé à l'ESP32. Sans l'option, esptool envoie `SPI_ATTACH 0` en ROM (« Enabling default SPI flash mode », `cmds.py:2089-2091`), c'est-à-dire les broches du boot normal ; les eFuses de broches ne sont consultées que par le ROM pour ce comportement par défaut. **Ne pas utiliser `--spi-connection` sur cette carte.** La flash du P4 utilise des broches dédiées |
| 4 | `--flash-size` | Option `-fs`, valeurs exactes : `detect`, `keep`, `256KB`, `512KB`, `1MB`, `2MB`, `4MB`, `8MB`, `16MB`, **`32MB`**, `64MB` (casse et unité comme écrites). Le défaut de `read-flash` est `keep` : taille détectée mais paramètres non modifiés dans l'image. En ROM, la taille explicite est envoyée par `SPI_SET_PARAMS` (`flash_set_parameters`) ; esptool note que au-delà de 16 Mo l'adressage 4 octets n'est « pas entièrement pris en charge » en ROM et conseille de fixer la taille en cas d'échec |
| 5 | Alimentation de la flash, ECO7 | `power_on_flash` (`esp32p4.py:289-360`) s'applique aux révisions 301 et 302 (le P4 observé est v3.2, soit 302). En ECO7, si l'eFuse `DOWNLOAD_MODE_XPD_ON` est programmé, le ROM alimente déjà la flash ; esptool commente un **bogue ROM ECO7** : une deuxième entrée en mode téléchargement par reset UART laisse la flash alimentée mais le ROM rejoue la séquence « XPD on », qui n'est pas sûre deux fois : « the second attach/download can fail ». Contournement : effacer les bits `PMU_DATE` de forçage avant le rattachement. Sinon, séquence complète d'alimentation. **L'état de l'eFuse `DOWNLOAD_MODE_XPD_ON` de cette carte n'est pas connu** |
| 6 | Reset et retour application | Par défaut `--before default-reset` (séquence DTR/RTS) et `--after hard-reset` (RTS ; sur P4 avec USB OTG, reset par watchdog). `--after no-reset` laisse la puce en mode téléchargement. Le câblage DTR/RTS de cette carte vers EN/BOOT est **inféré, jamais mesuré** : un retour à l'application n'est pas garanti |

Constat supplémentaire pertinent : esptool-js 0.7.0 (utilisé par Device Lab) exécute le même `powerOnFlash` ECO7 que le Python (`esploader.main` appelle `postConnect`). Ce correctif existe donc déjà côté banc ; son effet sur nos mesures n'est pas établi.

## 3. Version et paramètres retenus

- **esptool 5.6.0**, stub par défaut inclus : le stub P4 de cette version est **esp-flasher-stub v1.4.1** (condensat identique à celui de la mise à jour du 2026-10-14), plus récent que le v1.2.2 d'esptool-js 0.7.0. Un `read-flash` avec stub par esptool 5.6.0 teste donc aussi un autre stub, et **vérifie lui-même le MD5 final** de la trame du stub (`loader.py:1719-1727`, « Digest mismatch »), contrairement à esptool-js.
- Mode ROM : `--no-stub` ; tailles : `--flash-size 32MB` ; ne pas fournir `--spi-connection` ; `--after no-reset` pour ne pas redémarrer la carte pendant la mesure.
- Journalisation obligatoire : `esptool version`, révision de puce (`v3.2`), `Flash ID`, taille configurée, message « Enabling default SPI flash mode », ligne de commande sans port ni numéro de série, SHA-256 des fichiers lus, horodatage.

## 4. Mesures ROM réellement possibles

| Option | Contenu | Octets comparables | Remarques |
|---|---|---|---|
| **A. MD5 ROM** | `SPI_FLASH_MD5` de `0x2000`+4096, deux fois ou plus (script bibliothèque `esp.flash_md5sum`, ou `verify-flash 0x2000 <fichier>` sans `--diff`, contre un fichier déjà enregistré) | Non (MD5 seul) | Lecture ROM `esp_rom_spiflash_read` : c'est la **même fonction** que le stub utilise ; un MD5 ROM stable avec des MD5 stub variables désignerait le stub (initialisation, horloge) |
| **B. Lecture lente SPI1** | boucle `run_spiflash_command(0x03, read_bits=32, addr=0x2000+o, addr_len=24)`, 1024 commandes par 4 Kio (maximum 32 bits lus par commande, `loader.py:2032`) | Oui | Chemin indépendant du ROM et du stub, mais **même mécanisme de commande utilisateur SPI1 que la lecture d'identifiant JEDEC**, qui a échoué en deuxième commande (valeur brute `0xFF7F0000`). Script à écrire et à relire, lent |
| **C. `read-flash` avec stub 1.4.1** | `esptool read-flash 0x2000 0x1000 fichier.bin`, deux fois | Oui | Vérifie le MD5 final du stub. Ce n'est pas le chemin ROM, mais isole l'effet du stub (v1.4.1 contre v1.2.2) |

Les cinq issues à distinguer, pour chaque exécution : **deux lectures identiques et réussies** ; **deux lectures différentes** (comparer octet par octet, avec `bench/tools/bitdiff.py`) ; **échec d'identification** (`flash-id`/valeur brute) ; **commande ROM non prise en charge** (`NotImplementedInROMError`, attendu pour `read-flash --no-stub`, à classer comme « non pris en charge », pas comme FAIL du matériel) ; **échec d'initialisation de la flash** (échec de `SPI_ATTACH`/`SPI_SET_PARAMS`). Aucune cause n'est déduite d'une seule exécution.

## 5. Respect de l'architecture du banc

Le navigateur est le propriétaire exclusif du port USB (Web Serial) ; `AGENTS.md` interdit esptool et l'accès direct aux ports pour contourner le banc, et le devcontainer n'a pas d'accès USB. Trois voies, de la plus à la moins conforme :

1. **Dans le banc, par le navigateur** : ajouter à l'agent une option `no_stub` à la connexion (esptool-js peut ne pas appeler `runStub`) et exposer `flash.md5` en mode ROM. La commande `flash.md5` accepte déjà le format ROM (`rom-ascii32`, vérifié par les tests simulés), et ses garanties (réservation, journal, preuves) restent intactes. C'est une modification du code servi : nouveau SHA relu, déploiement contrôlé et consentement. Elle ne donne que l'option A.
2. **esptool Python sur le PC de l'opérateur, port libéré** : l'opérateur déconnecte le port dans la page (le banc voit la carte déconnectée), lance lui-même la procédure, puis reconnecte. Aucun mécanisme concurrent n'est introduit dans Device Lab, mais les garanties (réservation, journal, quarantaine) ne s'appliquent pas à cette phase, et cela déroge à `AGENTS.md`. Il faut donc une décision explicite de l'opérateur, et les preuves sont saisies à la main (modèle de fiche à préparer). Donne A, B et C.
3. **Ne rien changer et comparer avec ce que le banc sait déjà faire** : écartée, elle ne répond pas à la question ROM.

Recommandation : **voie 1** pour l'option A en premier (la moins coûteuse et conforme à l'architecture), puis décision de l'opérateur sur la voie 2 si les octets (B) ou le stub 1.4.1 (C) sont nécessaires.

## 6. Procédure minimale proposée (à autoriser plus tard)

1. Préconditions : P4 connecté, aucune réservation, assets vérifiés, consentement nommant carte, scénario, commit et effets.
2. Mesure ROM (option A) : deux `flash.md5` ROM de `0x2000`/4096 dans une session sans stub, retour console unique à la fin ; arrêt au premier échec, aucun rejeu.
3. Comparer avec les MD5 de PC-7 (trois valeurs distinctes) et de PC-8 (`1229733f…`).
4. Si les MD5 ROM sont stables et diffèrent des MD5 du stub : instruire le stub et son initialisation. S'ils varient aussi : instruire la flash, le contrôleur ou l'alimentation.

## 7. Risques et limites

- La lecture par octets en ROM est impossible avec esptool 5.6.0 : toute « lecture ROM » comparable passe par B (script maison, mécanisme proche de celui qui échoue).
- Le MD5 ROM ne prouve pas l'intégrité : il peut varier sans que l'on sache quels octets.
- Le comportement de `--no-stub` sur ECO7 est documenté par les commentaires du code ; **rien n'a été observé sur la carte**. L'état de l'eFuse `DOWNLOAD_MODE_XPD_ON` est inconnu ; le lire est une opération matérielle (lecture seule), à autoriser séparément.
- Entrer plusieurs fois en mode téléchargement est précisément la situation du bogue ECO7 décrit par esptool : prévoir qu'une deuxième connexion puisse échouer, et ne pas la rejouer automatiquement.
- Reset et retour à l'application : câblage DTR/RTS non mesuré.
- Aucune modification d'eFuse, aucune écriture, aucun effacement ne fait partie de cette étude ; ne pas utiliser `espefuse` en écriture.
