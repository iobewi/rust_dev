# Audit du chemin de lecture flash du stub v1.2.2 sur ESP32-P4 ECO7 (v3.2)

**Statut : audit documentaire. Aucun développement, aucun déploiement, aucune opération matérielle, aucune nouvelle campagne proposée.** Il répond à : pourquoi le stub chargé par esptool-js 0.7.0 rend-il des lectures instables (PC-7, PC-8) alors que les trois MD5 de la ROM sont identiques (PC-9) ?

## 1. Sources exactes

- Le stub : esp-flasher-stub **v1.2.2** (tag `6f8dc174cd97…`, 2026-08-19). Son JSON `esp32p4.json` est identique (condensat du champ `text`) à celui embarqué par esptool-js 0.7.0 et 0.7.1 et par le commit esptool `4b64d421`. Pour une puce de révision ≥ 300, esptool-js choisit `esp32p4.json`, pas `esp32p4-rev1.json` (le P4 observé est v3.2 = 302).
- La bibliothèque : esp-stub-lib `d61983fa4d1f…`, épinglée par ce tag (sous-module `esp-stub-lib`). Ce n'est ni `esptool-legacy-flasher-stub` ni une version plus récente.
- Le firmware ROM du P4 n'est pas disponible en source : tout ce qui s'exécute dans la ROM (`esp_rom_opiflash_exec_cmd_ecoN`, `SPI_FLASH_MD5`) est hors audit.

## 2. Réponses aux six questions

**1. Où se choisit le mode de lecture.** `esp-stub-lib/src/flash.c` : variable `large_flash_mode` (`:32`), posée dans `stub_lib_flash_init_ex` (`:83`) par `if (STUB_FLASH_4BYTE_ADDR_SUPPORTED && flash_size > MIB(16))` (`:101`), et consommée par `stub_lib_flash_read_buff` (`:142-155`) : si le mode est actif, appel de `stub_target_flash_4byte_read(FLASH_SPI_NUM, …)` (`:152`), sinon de `stub_target_flash_read_buff` (`:155`), c'est-à-dire la ROM `esp_rom_spiflash_read` (`target/common/src/flash.c:166`). Condition de compilation : `SOC_SPI_FLASH_4B_ADDR_SUPPORTED`, définie à 1 pour le P4 (`target/esp32p4/include/soc/soc_caps.h:18`). `main.c:65` appelle `stub_lib_flash_init(NULL)` une fois, au démarrage (`enable_4byte_cache_mode` faux).

**2. Dépend-il de la capacité, de l'adresse, ou des deux ?** **De la capacité seulement**, une fois pour toutes au démarrage. `flash_size` vient de l'identifiant lu par la ROM (`stub_target_flash_get_flash_id` → `esp_rom_spi_flash_update_id`, `target/common/src/flash.c:138`, puis `stub_target_flash_id_to_flash_size`, `:62`, où l'octet de capacité `0x19` donne 32 Mio). Un identifiant illisible donnerait une taille nulle, remplacée par le maximum de la puce (64 Mio pour le P4) : le mode « grande flash » serait actif dans les deux cas. `read_buff` ne regarde jamais l'adresse.

**3. Une lecture sous 16 Mio peut-elle utiliser la commande 3 octets ?** **Non, avec ce stub et cette flash.** Le mode étant global, `0x2000` est lu en 4 octets. Aucune commande de l'hôte ne le change : `SPI_ATTACH` (`command_handler.c:631`) rappelle seulement l'attache ; `SPI_SET_PARAMS` (`:642`) met à jour la configuration en RAM de la ROM (`stub_lib_flash_update_config`, `flash.c:53`) sans toucher `large_flash_mode`. esptool-js n'envoie d'ailleurs ni l'un ni l'autre.

**4. Séquence exacte de lecture `0x0C`.**
- `stub_target_flash_4byte_read` (`flash_4byte.c:215`) : attend que la flash soit prête, puis boucle **par morceaux de 16 octets** (`FLASH_READ_CHUNK_SIZE`, `:41`, « taken from ROM SPI_BUFF_BYTE_READ_NUM ») ; la valeur de retour est toujours `STUB_LIB_OK` une fois la boucle faite.
- `exec_read_cmd_4b` (`:60`) : commande `CMD_FSTRD4B` = `0x0C` sur 8 bits, adresse sur **32 bits**, **8 cycles factices**, `mode = SPI_FLASH_FASTRD_MODE`, données reçues sur `8 × 16 = 128` bits dans le tampon de l'appelant, `cs_mask = CS0`, `is_write_erase_operation = false`. Elle ne fait que renseigner une structure et appeler `stub_target_opiflash_exec_cmd`.
- `stub_target_opiflash_exec_cmd` (`target/esp32p4/src/flash.c:98`) : aiguille d'après la variable ROM `_rom_eco_version` (adresse ROM `0x4fc00014`, `ld/esp32p4.rom.extra.ld:9`) vers une fonction **de la ROM** : eco2, eco5, eco1 (`< 5`), eco6, sinon eco7 (`esp_rom_opiflash_exec_cmd_eco7` = `0x4fc10b40`, `:14`). Cette fonction renvoie `void` : **aucun code d'erreur**, donc une lecture SPI erronée est invisible pour le stub.
- Fréquence, contrôleur : le stub n'écrit jamais le registre d'horloge de SPI1 ni la source d'horloge de la flash sur P4 (seul le fichier de l'ESP32 classique le fait). La fréquence de lecture est donc celle que la ROM a réglée avant lui. `stub_target_flash_init` (`esp32p4/src/flash.c:211`) attache la flash (`esp_rom_spiflash_attach` ou `boot_attach`, `:183-209`) et positionne `SPI_MEM_USR_COMMAND` sur SPI1 (`:220`). Ce que la fonction ROM fait ensuite (attente de fin, lecture des registres de données, sensibilité aux cycles factices) **n'est pas auditable**.
- Côté `READ_FLASH` (`command_handler.c:764`) : lit `packet_size` (4096) octets d'un coup dans un tampon de 4102 octets en pile, met à jour le MD5, envoie la trame, avec un seul paquet non acquitté (`:775`) ; `SPI_FLASH_MD5` (`:690`) lit par blocs de 4096 octets dans un tampon de 4096 octets en pile. **Les deux appellent la même `stub_lib_flash_read_buff`** : donc le même chemin, ce qui est cohérent avec MD5 de la puce et lectures variables ensemble en PC-7.

**5. Comparaison avec `SPI_FLASH_MD5` de la ROM.** Non faisable en source : le code ROM n'est pas public. Ce que les sources permettent d'affirmer : la ROM expose `esp_rom_spiflash_read` (`ld/…rom.ld:121`, `0x4fc00158`), que le stub n'utilise que pour une flash ≤ 16 Mio ; que le stub ne l'appelle donc pas pour nos 32 Mio ; qu'esptool signale que la ROM n'adresse pas pleinement au-delà de 16 Mio. **Que le MD5 de la ROM passe par `esp_rom_spiflash_read` et une commande 3 octets reste une inférence**, non vérifiée.

**6. Alternative ou correctif d'Espressif ?**
- Entre v1.2.2 et v1.4.1, le chemin de lecture ECO7 est **inchangé** (seuls ECO8 et la suspension du cache L2 changent côté P4). Le mode « cache 4 octets » ajouté ensuite (esp-stub-lib #151, 2026-10-15) sert au fonctionnement de l'application depuis le cache pour OpenOCD, pas à `READ_FLASH`.
- **Un stub antérieur sans le changement d'alimentation et d'horloge existe** : les JSON de `esptool v5.3.0` et `v5.3.1` (2026-06) ont un `text` identique entre eux, antérieur à esp-stub-lib #120 (fusionné le 2026-07-16), et prennent en charge l'ECO7. Les stubs v1.1.0, v1.2.0 et v1.2.2 contiennent #120 (vérifié par comparaison des commits) ; v1.0.0 ne le contient pas. Les utiliser demanderait de charger ce JSON à la place du v1.2.2, ce qu'esptool-js ne permet pas sans l'embarquer soi-même ; ils n'ont pas non plus le correctif v1.2.2 « skip ROM flash XPD when PMU latch is set ».
- Le stub « legacy » (génération 1) n'existe que pour `esp32p4-rev1` : il ne couvre pas l'ECO7.

## 3. Constat important, hors des six questions : le stub change l'alimentation et l'horloge du cœur

`main.c:47-54` appelle `stub_lib_clock_init()` **avant** l'initialisation de la flash (`:65`) pour le P4. `target/esp32p4/src/clock.c` : `stub_target_switch_to_dcdc` (`:106`) bascule le cœur HP du régulateur interne vers le **DCDC externe** (valeurs de `dbias` issues des eFuses, traitement « ECO6+ » du retour DCDC pour une révision ≥ 301) et `stub_target_apply_cpu_cpll_div2` (`:147`) fait passer le CPU de ~100 MHz (CPLL/4) à **200 MHz** (CPU_FREQ_MHZ, `:33`), avec APB/SYS/MEM réglés comme IDF. Le commentaire du code précise que le suivi de tension par PVT est désactivé dans le stub et que le réglage par défaut est jugé suffisant. Le mode ROM ne fait rien de cela.

Contexte public (issue esp-flasher-stub #122, ouverte, 2026-11-11) : sur un P4 rev3.2 d'un autre fabricant, le stub v5.6.0 plante au moment de ce passage à la voie « plus gourmande en puissance » ; le développeur d'Espressif suspecte une alimentation du cœur (VDD_HP) insuffisante et cherche à améliorer le « DCDC handoff » ; le stub fourni par esptool 5.3.0 fonctionne sur la même carte, et la ROM sans stub aussi. Le TRM (§15.4.1) confirme que la puce démarre sur le régulateur HP interne et qu'il est « recommandé » de passer au DCDC externe ensuite. Le schéma Waveshare extrait montre un convertisseur externe `U4 MP1605GTF-Z` piloté par `EN_DCDC`/`FB_DCDC` ; sa capacité n'a pas été évaluée. **Rien ne permet de dire que notre carte est dans ce cas.**

## 4. Régularité des écarts déjà enregistrés (analyse hors ligne des cinq lectures du stub sur `0x2000`/4096, aucun contenu affiché)

- Cinq lectures (PC-1, deux de PC-6, deux de PC-7) : aucune n'a pour MD5 celui de la ROM, et le **vote majoritaire** des cinq a lui-même un MD5 différent de celui de la ROM : le vote n'est donc pas une référence fiable (des écarts peuvent être communs à toutes les lectures).
- Par rapport au vote : 66 octets déviants. **Au-delà des 32 premiers octets, les 31 écarts tombent tous sur les octets 1 à 3 de leur morceau de 16 octets** (positions modulo 16 = 1, 2 ou 3), alors que 3 positions sur 16 donneraient environ 6 par hasard. Dans les 32 premiers octets, les écarts touchent toutes les positions. 79 % des écarts sont un seul bit.
- Les mêmes positions reviennent d'une lecture à l'autre (par exemple `0xa3`, `0x102`, `0x122`, `0x142`, `0x1e2`, `0x242`, `0x5e1`, `0x601`, `0x611` dans deux lectures chacune).
- Ces écarts **ne ressemblent pas à une copie de l'octet voisin** (égalité avec l'octet 16 plus tôt ou plus tard : 9 et 10 cas sur 66).
- Cela relie les écarts à la **structure en morceaux de 16 octets** du chemin de lecture (sections 2, point 4), pas à la liaison série ni au contenu de la flash en général. Ce n'est pas une explication : la cause du motif n'est pas établie.

## 5. Lien avec PC-7, PC-8 et PC-9

- **PC-7** (stub) : MD5 de la puce et lectures variables, MD5 final du stub égal à celui des octets reçus : l'instabilité apparaît **dans la lecture par le stub, avant l'envoi**, et pas sur la liaison. Cohérent avec §2.4 (même fonction de lecture pour `READ_FLASH` et `SPI_FLASH_MD5`, sans retour d'erreur) et avec §4 (régularité par morceaux de 16 octets).
- **PC-8** (stub) : l'identifiant JEDEC lu à la deuxième commande est invalide (`0xFF7F0000`), alors qu'il est valide à la première. Cette lecture passe par des registres SPI1 pilotés commande par commande par le stub. **Non expliqué** par l'audit.
- **PC-9** (ROM, sans stub) : trois MD5 identiques, et l'identification de la deuxième commande passe. **Mais PC-9 change deux choses à la fois** par rapport au stub : le chemin de lecture (ROM contre lecture 4 octets du stub) **et** l'environnement matériel (la ROM ne bascule pas sur le DCDC externe et reste à ~100 MHz ; le stub oui, à 200 MHz). PC-9 ne peut donc pas distinguer « le chemin de lecture du stub » de « l'état d'alimentation et d'horloge créé par le stub ».

## 6. Conclusion : **C**

- **A (chemin alternatif sans modifier le stub) : non pour le v1.2.2 sur ce matériel.** Aucune commande de l'hôte ne désactive le mode 4 octets ; le seul substitut est un autre binaire de stub (JSON d'esptool 5.3.x, avant le changement d'alimentation et d'horloge), à embarquer côté hôte, au prix de correctifs ECO7 plus récents absents.
- **B (changement limité du stub) : pas démontré nécessaire.** Aucun correctif connu d'Espressif ne cible le chemin 0x0C ECO7 ; le seul chantier actif lié (issue #122) concerne le passage DCDC/horloge.
- **C : les sources ne tranchent pas.** Trois pistes restent ouvertes, sans que les observations disponibles les départagent : (1) la fonction ROM de lecture 4 octets ECO7 appelée par morceaux de 16 octets (le motif de §4), (2) l'environnement d'alimentation et d'horloge créé par le stub avant la lecture, (3) la combinaison des deux. La ROM étant hors audit, la comparaison de la question 5 reste une inférence.

## 7. Limites

Le code de la ROM n'est pas lisible ; `_rom_eco_version` de cette carte n'est pas observé ; la version exacte du firmware ROM de l'ECO7 n'est pas connue ; l'analyse de §4 repose sur un vote majoritaire non validé ; l'état de l'eFuse `DOWNLOAD_MODE_XPD_ON` est inconnu ; aucune cause n'est établie.
