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

**Branche B — sinon** (séquence complète, 9 écritures) :

| Étape | Registre (adresse) | Opération |
| --- | --- | --- |
| 1 | `LP_SYSTEM_REG_ANA_XPD_PAD_GROUP_REG` (`0x5011010c`) | `= 1` (groupe de pads), attente 10 ms |
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
- **Fait (stub instrumenté)** : `main.c` du stub appelle `stub_lib_flash_init(NULL)` au démarrage ; le contenu de cette fonction (bibliothèque `esp-stub-lib`) n'a **pas** été lu : on ignore si elle alimente ou attache la flash.

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

Si le bloc 0x800 permet de charger le stub, **la lecture flash du stub pourrait quand même échouer ou renvoyer 0xFF** si la flash n'est ni alimentée ni attachée (H1–H3). Garde-fou disponible sans rien changer : T publie `data_sha256` ; A1 a mesuré `236f5efcb63232f16796346beb3e01fdcc0061fc096e0f46a93905d7e3913ca2` pour la même région. Un SHA différent (par exemple 1 Mio d'octets `0xFF`) invalide la mesure et se distingue d'un problème de timing. Le stub se lance par `main.c` → `stub_lib_flash_init(NULL)` (non audité).

## 10. Recommandations (aucune mise en œuvre ici)

1. Ne pas appeler `powerOnFlash()` ni migrer vers 0.7.1 (aucun correctif pertinent).
2. Avant un nouvel essai T : décider si la lecture seule de `0x5012d034` et `0x501153fc` (deux `ESP_READ_REG`) est autorisée, pour fixer H1.
3. Lire `esp-stub-lib` (`stub_lib_flash_init`) pour savoir si le stub alimente/attache lui-même la flash.
4. Si H1/H2 se confirment, instruire une PR et une autorisation matérielle distinctes pour une initialisation flash minimale en ROM seule.
