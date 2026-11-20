# Audit en lecture seule — `powerOnFlash()` et l'accès flash de l'ESP32-P4 ECO7 (v3.2)

Date : 2026-11-20. Aucune exécution matérielle, aucun code modifié, aucune migration d'esptool-js. Cadre : run-003 (JEDEC brut `0x00ffffff` en session « ROM seule », puis timeout du chargement RAM). Les **faits** sont lus dans le code cité ; les **hypothèses** sont marquées comme telles.

## 1. Sources et versions

| Source | Fichiers lus |
| --- | --- |
| esptool-js **0.7.0** (version servie par IDLab, `https://unpkg.com/esptool-js@0.7.0/bundle.js`) | `lib/esploader.js` (`main`, `runStub`, `readFlashId`, `flashSpiAttach`, `usesUsbOtg`, `applyUsbFlashWriteSize`), `lib/targets/esp32p4.js` (`postConnect`, `powerOnFlash`, `disableWatchdogs`, `usesUsbOtg`, constantes) |
| esptool-js **0.7.1** | mêmes fichiers |
| esptool (Python) **5.6.0** | `esptool/targets/esp32p4.py` (`_post_connect`, `power_on_flash`, `disable_watchdogs`), `esptool/loader.py` (`_post_connect`, `flash_id`, `flash_spi_attach`, `mem_*`, `run_stub`), `esptool/cmds.py` (`attach_flash`, `flash_id`) |

**Fait** : entre 0.7.0 et 0.7.1, `lib/esploader.js` et `lib/targets/*.js` sont identiques (`diff -rq` : seuls `index.js`, `index.d.ts` et un nouveau `image/intelHex.*` diffèrent). Passer à 0.7.1 ne change ni `postConnect`, ni `powerOnFlash`, ni `usesUsbOtg`.

## 2. Conditions d'exécution

`main()` (JS) enchaîne : `detectChip` → description/révision/MAC → **`chip.postConnect(loader)`** → `runStub()` → `readFlashId()`. En Python : `_post_connect()` après la connexion. Pour le P4, `postConnect` fait, dans cet ordre :

1. si USB-OTG détecté : `ESP_RAM_BLOCK = USB_RAM_BLOCK` (0x800) ;
2. si pas de stub déjà en marche : `disableWatchdogs()` — n'agit que si `usesUsbJtagSerial()` (UARTDEV_BUF_NO = USB-JTAG/Serial) ; **sans effet sur notre transport USB-OTG** (PID 18) ;
3. si pas de mode « secure download » : **`powerOnFlash()`** (« needs to be powered on before attach_flash() », commentaire Python).

`powerOnFlash()` ne fait rien sauf pour les révisions **301 (ECO6)** et **302 (ECO7, v3.2)** : « flash defaults off on ECO6/ECO7 after the 1.8 V → 3.3 V default change, to prevent damage to 1.8 V flash chips. Board designers must set the appropriate voltage level in eFuse ».

## 3. Registres et bits touchés (ECO7 = révision 302)

Bases lues dans `esp32p4.js` : `EFUSE_BASE = 0x5012d000`, `DR_REG_LPAON_BASE = 0x50110000`, `DR_REG_PMU_BASE = LPAON + 0x5000 = 0x50115000`, `DR_REG_LP_SYS_BASE = LPAON`.

**Branche A — eFuse `DOWNLOAD_MODE_XPD_ON` programmé** (`EFUSE_RD_REPEAT_DATA1_REG` = `0x5012d034`, bit 16) : la ROM met déjà le XPD de la flash. Le code ne fait que **libérer** le « force-on » : lit `PMU_DATE_REG` (`0x501153fc`) ; si `(date & 0x3) == 0x3`, écrit `date & ~0x3` ; retour. Commentaire : relancer le chemin « XPD on » alors que la flash est déjà alimentée n'est pas sûr (le second attach peut échouer).

**Branche B — sinon** : séquence complète, **8 écritures** de registre (`writeReg`), chacune précédée d'une lecture-modification sauf la première, soit **7 lectures** ; elles portent sur **4 registres distincts** (`LP_SYSTEM…XPD_PAD_GROUP`, `PMU_EXT_LDO_P0_0P1A_ANA`, `PMU_EXT_LDO_P0_0P1A`, `PMU_DATE`). Le Python (`power_on_flash`, 5.6.0) exécute la même suite de 8 écritures ; seules les attentes diffèrent (50 µs côté Python, 1 ms côté JS). Branche A, pour comparaison : 2 lectures (eFuse, `PMU_DATE`) et **au plus 1 écriture** (`PMU_DATE`).

| Étape | Registre (adresse) | Opération |
| --- | --- | --- |
| 1 | `LP_SYSTEM_REG_ANA_XPD_PAD_GROUP_REG` (`0x5011010c`) | `= 1` (groupe de pads), attente 10 ms (écriture sèche, sans lecture) |
| 2 | `PMU_EXT_LDO_P0_0P1A_ANA_REG` (`0x501151bc`) | `\|= bit 27` (EN_CUR_LIM_0) |
| 3 | `PMU_EXT_LDO_P0_0P1A_REG` (`0x501151b8`) | `\|= bit 7` (FORCE_TIEH_SEL_0) |
| 4 | `PMU_DATE_REG` (`0x501153fc`) | `\|= 0x3` (**flash force-on**), attente ~50 µs (JS : 1 ms) |
| 5 | `…_ANA_REG` | `&= ~bit 27` |
| 6 | `PMU_EXT_LDO_P0_0P1A_REG` | `&= ~(0xff << 23)` (TARGET0_0) |
| 7 | idem | `\|= 0x80` (« update eFuse voltage to PMU ») |
| 8 | idem | `&= ~bit 7` ; attente 1,8 ms (stabilisation de l'alimentation) |

## 4. Effets sur l'alimentation de la flash (faits / limites)

- **Fait** : la séquence force l'alimentation de la flash (XPD) et transmet au PMU la tension définie par l'eFuse. Sur ECO7, le code distingue le cas où la ROM l'a déjà fait (branche A).
- **Fait** : l'amorce (`disableWatchdogs`) ne concerne pas le transport USB-OTG.
- **Non vérifié** : le comportement exact de la ROM P4 ECO7 en mode téléchargement quand l'eFuse `DOWNLOAD_MODE_XPD_ON` n'est pas programmé ; la valeur de cet eFuse sur notre carte (jamais lue).

## 5. Rôle de `SPI_ATTACH`

- **Fait** : `ESP_SPI_ATTACH` (0x0D) est une commande de la ROM qui configure les broches de la flash. esptool-js l'expose (`flashSpiAttach`) mais `main()` **ne l'appelle pas** : l'attache est laissée au stub. esptool Python, lui, appelle `attach_flash(esp)` avant les opérations flash, y compris en ROM (`cmds.py`).
- **Fait** : `readFlashId()` (JS) / `flash_id()` (Python) exécutent `RDID 0x9F` par le périphérique SPI (`runSpiflashCommand`), sans commande d'attache intégrée.
- **Fait** : `main()` lit le JEDEC **après** `runStub()` et avertit si `0xffffff` ou `0x000000` (« Failed to communicate with the flash chip »). Espressif ne lit donc jamais ce JEDEC en ROM seule dans le chemin JS.
- **Fait (stub instrumenté)** : `main.c` du stub appelle `stub_lib_flash_init(NULL)` au démarrage ; voir le §11 (audit d'`esp-stub-lib`) : le stub **attache lui-même** la flash par la ROM, mais n'exécute **pas** la séquence `powerOnFlash()` de l'hôte.

## 6. JEDEC `0x00ffffff` : hypothèses (aucune établie)

Fait d'observation : en « ROM seule » nous n'exécutons ni `powerOnFlash()` ni `SPI_ATTACH` avant `readFlashId()`, et la lecture renvoie tous les bits à 1.

| Hypothèse | Mécanisme | Ce qui la soutient / l'infirme |
| --- | --- | --- |
| H1 | Flash non alimentée (ECO7, eFuse `XPD_ON` non programmé) | Cohérent avec le commentaire d'esptool ; non testé |
| H2 | Pads SPI non routés / flash non attachée en ROM | `attach_flash` précède `flash_id` en Python ; non testé |
| H3 | H1 et H2 | — |
| H4 | Autre (flash absente, tension, bus) | A1 et D ont lu cette flash avec succès via `main()` : fait contre une panne matérielle |

Test discriminant **en lecture seule** (hors périmètre, nécessite une autorisation matérielle) : `ESP_READ_REG` sur `0x5012d034` (bit 16) et `0x501153fc` (bits 0–1), sans aucune écriture.

## 7. Risques d'appeler `powerOnFlash()` en session « ROM seule »

1. **Écritures de registres d'alimentation** (LP system, PMU) : seul un reset les annule ; la politique ROM seule les interdit (« aucune écriture »).
2. **Non idempotent** : le commentaire d'esptool indique que rejouer le chemin quand la flash est déjà alimentée peut échouer ; deux sessions successives sans reset poseraient problème.
3. **Tension** : une valeur d'eFuse inadaptée à la flash montée peut endommager une flash 1,8 V ; nous ne connaissons ni l'eFuse ni la tension de la carte.
4. **Pas nécessaire au chargement RAM** : il n'a aucun effet sur le timeout de run-003 (voir §8).

## 8. Adaptations du chargement RAM et de l'accès flash

| | Chargement RAM (diagnostic) | Accès flash (lecture par le stub) |
| --- | --- | --- |
| Adaptation requise par les références | `ESP_RAM_BLOCK = 0x800` si USB-OTG (`postConnect` / `_post_connect`) | `powerOnFlash()` (ECO6/ECO7) puis attache SPI (`SPI_ATTACH` ROM, ou stub) |
| Écritures matérielles | Aucune (taille de bloc côté hôte) | Écritures PMU/LP system ; `SPI_ATTACH` configure les broches |
| Statut dans IDLab | Corrigé dans la PR dédiée (0x800, sans autre effet) | **Non fait**, volontairement |

## 9. Conséquence pour la prochaine tentative T (réserve)

Si le bloc 0x800 permet de charger le stub, **la lecture flash du stub pourrait quand même échouer ou renvoyer 0xFF** si la flash n'est ni alimentée ni attachée (H1–H3). Garde-fou disponible sans rien changer : T publie `data_sha256` ; A1 a mesuré `236f5efcb63232f16796346beb3e01fdcc0061fc096e0f46a93905d7e3913ca2` pour la même région. Un SHA différent (par exemple 1 Mio d'octets `0xFF`) invalide la mesure et se distingue d'un problème de timing. Le stub attache la flash lui-même (§11), ce qui écarte H2 pour T ; H1 (alimentation) reste ouverte tant que l'eFuse `DOWNLOAD_MODE_XPD_ON` de la carte est inconnue.

## 10. Recommandations (aucune mise en œuvre ici)

1. Ne pas appeler `powerOnFlash()` ni migrer vers 0.7.1 (aucun correctif pertinent).
2. Avant un nouvel essai T : décider si la lecture seule de `0x5012d034` et `0x501153fc` (deux `ESP_READ_REG`) est autorisée, pour fixer H1.
3. ~~Lire `esp-stub-lib`~~ : fait au §11. Reste à connaître l'eFuse `DOWNLOAD_MODE_XPD_ON` de la carte (lecture seule, §6).
4. Si H1/H2 se confirment, instruire une PR et une autorisation matérielle distinctes pour une initialisation flash minimale en ROM seule.

## 11. Audit d'`esp-stub-lib` : `stub_lib_flash_init(NULL)` (commit `d61983fa4d1f66b9c088608c1f702ad877121279`, stub v1.2.2)

Sources lues dans l'arbre de build privé du stub instrumenté (`src-traced/esp-stub-lib`). **Fait** : les sources de flash (`src/flash.c`, `src/target/esp32p4/src/flash.c`, `src/target/common/src/flash.c`) et `main.c` sont **identiques** entre le stub standard et le stub instrumenté ; seul `src/usb_otg.c` diffère dans la bibliothèque (instrumentation).

### Chaîne d'appels (faits)
1. `main.c:65` : `stub_lib_flash_init(NULL);` — exécuté au démarrage du stub, après l'initialisation d'horloge, **avant** `stub_transport_init()` et **avant** l'envoi du `OHAI`. Le code de retour est **ignoré**.
2. `flash.c` : `stub_lib_flash_init(state)` = `stub_lib_flash_init_ex(state, STUB_LIB_FLASH_ATTACH_ALWAYS, false)`.
3. `stub_lib_flash_init_ex` : appelle `stub_target_flash_init(state, policy)`, puis lit l'identifiant (`stub_target_flash_get_flash_id()` → `esp_rom_spi_flash_update_id()`, fonction ROM), en déduit la taille ; **ID inconnu** : le code suppose la taille maximale de la puce (64 Mio pour le P4), journalise « Unknown flash ID, assuming … » et renvoie `STUB_LIB_ERR_UNKNOWN_FLASH_ID` (ignoré par `main.c`) ; puis `stub_target_flash_update_config(...)`.
4. P4, `stub_target_flash_init` : avec la politique `ATTACH_ALWAYS`, appelle `stub_target_flash_attach(0, false)` (la politique `IF_NEEDED`, qui teste l'état du cache, n'est pas utilisée ici), puis positionne `SPI_MEM_USR_COMMAND` dans `SPI_MEM_USER_REG(FLASH_SPI_NUM)`.
5. P4, `stub_target_flash_attach` : si `_rom_eco_version >= 7` **et** eFuse `DOWNLOAD_MODE_XPD_ON` **et** `(PMU_DATE_REG & 0x3) == 0x3` → `esp_rom_spiflash_boot_attach(ishspi, legacy, true)` ; sinon `esp_rom_spiflash_attach(ishspi, legacy)` (fonctions ROM, symboles `spi_flash_attach` / `spi_flash_boot_attach` dans `esp32p4.rom.api.ld`). Commentaire de la bibliothèque : « If DOWNLOAD_MODE_XPD_ON eFuse is set, ROM powers on the flash chip inside esp_rom_spiflash_attach. This power-on sequence cannot run twice, so skip it when PMU_DATE[1:0] already force the rail on. »
6. `_rom_eco_version` est une variable de la ROM (`0x4fc00014`) ; le routage des commandes SPI du stub dépend aussi de cette version (`stub_target_opiflash_exec_cmd`, variantes `eco1`…`eco7`).

### Ce que le stub ne fait pas (faits)
- Aucune **écriture** de `PMU_DATE_REG`, `PMU_EXT_LDO_P0_0P1A*` ou `LP_SYSTEM_REG_ANA_XPD_PAD_GROUP_REG` dans le code du stub ou de la bibliothèque : `PMU_DATE_REG` n'est que **lu** (décision du §5). Le stub n'exécute donc **pas** la séquence `powerOnFlash()` d'esptool.

### Dépendances non auditées
Le corps des fonctions ROM `spi_flash_attach`, `spi_flash_boot_attach` et `esp_rom_spi_flash_update_id` (code ROM fermé) : on ne connaît leur comportement que par le commentaire ci-dessus.

### Réponse à la question « le stub initialise-t-il lui-même la NOR du P4 ECO7 ? »
- **Oui pour l'attache SPI** : fait, systématique, avant le `OHAI`.
- **Pour l'alimentation de la flash, oui seulement par la ROM et seulement si l'eFuse `DOWNLOAD_MODE_XPD_ON` est programmé** (affirmation du commentaire ; ROM non lue). Sinon, aucune alimentation n'est exécutée par le stub : elle relève de l'hôte (`powerOnFlash()`), que la session ROM seule n'exécute pas.
- **Conséquences (hypothèses)** : (a) le JEDEC `0x00ffffff` lu par l'hôte sans attache (H2) ne préjuge pas de la lecture par le stub, qui attache lui-même ; (b) si l'eFuse n'est pas programmé et que la flash n'est pas déjà alimentée après BOOT + RESET (H1), le stub lira une flash non alimentée : tous bits à 1 ou ID inconnu (taille supposée 64 Mio). Les deux cas se détectent a posteriori par le `data_sha256` de T (A1 : `236f5efc…`).
- **Autre effet à noter** : `stub_lib_flash_init` s'exécute avant le `OHAI` ; une attache lente ou bloquante retarderait le salut, limité à 3000 ms côté hôte (`DEFAULT_TIMEOUT`). Aucune mesure n'existe sur ce point.
