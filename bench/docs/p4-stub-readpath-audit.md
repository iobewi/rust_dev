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

- **Erratum (complément, §8)** : les « 31 écarts » comptaient des occurrences relatives à un vote majoritaire ; le recalcul sans vote donne 22 positions distinctes, toutes dans les positions 1 à 3, et corrige la vraisemblance annoncée. Le §8 refait ce calcul et ajoute deux constats.

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


## 8. Complément (2026-11-12) : réponses à la revue, deux axes, isolement du facteur

**Rien n'a été développé, déployé ni exécuté sur matériel ; aucune campagne n'est proposée.**

### 8.1 Origine du découpage et calcul des écarts (vérification)

- **Origine du découpage.** Les cinq lectures commencent à `0x2000`, multiple de 16. `READ_FLASH` lit `packet_size` = 4096 octets d'un seul appel à `stub_lib_flash_read_buff` ; `stub_target_flash_4byte_read` découpe ensuite à partir de l'adresse de départ en morceaux de 16 octets. Le morceau *k* couvre `0x2000 + 16k` à `+15`, et « position dans le morceau » = `décalage mod 16` = `adresse flash mod 16`. Même découpage pour `SPI_FLASH_MD5`.
- **Calcul des écarts, première version.** Les écarts étaient calculés par rapport à un **vote majoritaire** des cinq lectures. Ce vote n'est pas une référence : son MD5 diffère de celui de la ROM. Le motif pouvait être un artefact du vote.
- **Recalcul sans vote** (comparaison de chaque paire de lectures, 10 paires, de 15 à 33 octets différents par paire) : **22 positions distinctes** au-delà des 32 premiers octets, **les 22 dans les positions 1 à 3 d'un morceau**, c'est-à-dire les octets 1 à 3 du **premier mot de 32 bits** (11, 7 et 4 respectivement pour les octets 1, 2 et 3 du mot). Dans les 32 premiers octets, toutes les positions sont touchées. Sous « positions uniformes parmi 16 », la probabilité est de l'ordre de 1e-16 ; sous une hypothèse plus prudente où les écarts ne seraient visibles que là où le contenu change d'un morceau au suivant (47 % de ces positions sont dans les positions 1 à 3), de l'ordre de 6e-8. Le motif tient donc sans vote, mais l'estimation de 1e-23 donnée plus haut comptait à tort des occurrences répétées.
- **Constat nouveau 1.** La zone lue est à **86,5 % de `0xFF`** ; aucun des 22 écarts n'est sur un octet `0xFF`.
- **Constat nouveau 2 (signature de décalage d'échantillonnage d'un bit).** Sur les 52 écarts d'un seul bit, **43 donnent un bit égal à l'un des deux bits voisins dans l'ordre série** (octets par adresse croissante, bit de poids fort d'abord), contre environ 20 (95e centile 25) si un bit quelconque du même octet était inversé au hasard. Un échantillonnage décalé d'un bit ne produit une erreur visible que là où deux bits voisins diffèrent, ce qui s'accorde avec le point précédent. C'est une **compatibilité avec une erreur de synchronisation**, pas une preuve ; la référence reste incertaine (le vote n'est pas validé) et l'ordre série est supposé.
- **Limites** : cinq lectures de la même plage de 4 Kio ; aucune lecture n'a le MD5 de la ROM.

### 8.2 Axe 1 : le transfert des 128 bits

- **Ce qui est public** : l'en-tête ROM du P4 documente `esp_rom_opiflash_exec_cmd(spi_num, mode, cmd, cmd_bit_len, addr, addr_bit_len, dummy_bits, mosi…, miso…, cs_mask, is_write_erase_operation)` : « dummy_bits : bit length of dummy field », mode de type `esp_rom_spiflash_read_mode_t` (FASTRD vaut 4, comme dans la bibliothèque du stub). C'est l'assistant OPI de la ROM (« for MX25UM25645G »). **ESP-IDF ne l'appelle pas sur P4 pour une flash quad ordinaire** (ses usages sont des chemins octal) ; le stub s'en sert comme exécuteur générique de commande utilisateur pour l'adressage 4 octets depuis esp-stub-lib #51 (2026-02) et #87 (variante ECO7). La lecture de référence d'IDF sur P4 (`spimem_flash_ll_get_buffer_data`) copie les mots du tampon de données SPI1 en mémoire ; `spimem_flash_ll_set_extra_dummy` est vide sur P4.
- **Ce qui n'est pas auditable** : le corps de la fonction ROM (ordre de copie des mots, attente de fin, gestion des cycles factices). Toute affirmation sur l'ordre des mots ou les limites de transaction à l'intérieur de la ROM serait une hypothèse.
- **Ce que les données contraignent** : une erreur d'ordre de mots serait systématique sur tous les morceaux ; or, après les 32 premiers octets, seuls les octets 1 à 3 du premier mot sont touchés, et seulement par moments. Cela évoque un effet de **début de la phase de données** (bits 8 à 31 après l'octet 0) plutôt qu'un défaut d'ordre. Ni les cycles factices ni la limite de transaction ne peuvent être vérifiés d'après les sources.

### 8.3 Axe 2 : ce que modifie exactement `stub_lib_clock_init()` (P4, v1.2.2)

Appelée au démarrage (`main.c:47-54`, `#if` compilé pour `ESP32P4`), puis attente de **10 ms** (`esp-stub-lib/src/clock.c`, commentaire : « ESP32-P4 ECO7 needs more than other revisions »).

| Étape (`target/esp32p4/src/clock.c`) | Registres | Effet |
|---|---|---|
| Basculement vers le DCDC externe (`:106`) | `PMU_HP_ACTIVE_HP_REGULATOR0` (XPD, DBIAS = valeur des eFuses + 16, puis XPD retiré), `PMU_LP_ACTIVE_LP_REGULATOR0` (DBIAS), `PMU_DCM_CTRL` (retour DCDC, demande d'activation), `PMU_POWER_DCDC_SWITCH`, `PMU_HP_ACTIVE_BIAS` (DCM_VSET = 27), `LP_SYSTEM_REG_SYS_CTRL` (LP_FIB_SEL = 0xEF si révision ≥ 301) | Le cœur passe du régulateur HP interne au convertisseur externe ; attentes de 1 ms et 10 µs. Suivi de tension PVT désactivé (commentaire du code) |
| Horloges (`:147`) | `HP_SYS_CLKRST` ROOT_CLK_CTRL2 (APB ÷2), CTRL1 (SYS ÷1, MEM ÷1), CTRL0 (CPU ÷2) avec impulsions de mise à jour, `LP_CLKRST_HP_CLK_CTRL` (racine = CPLL) | CPU de ~100 MHz à **200 MHz** ; CPLL non recalibrée (400 MHz de la ROM) ; `esp_rom_set_cpu_ticks_per_us(200)` |

**Non modifié** : la configuration de la CPLL, la source et les diviseurs de l'horloge de la flash (MSPI), les registres d'horloge de SPI1, l'alimentation de la flash (faite par l'attache). Le stub ne touche donc pas directement à la fréquence de lecture de la flash ; en revanche, les bus APB, SYS et MEM, sur lesquels se trouvent les registres de SPI1 et la mémoire qui reçoit les données, changent de fréquence.

**Documentation d'Espressif pour l'ECO7.** esp-stub-lib #87 (2026-03-25) : l'attente après l'initialisation d'horloge passe de 50 µs à 5 ms parce que des cartes plantaient (Guru Meditation) ; #91 (2026-03-31) : « some boards would crash … immediately after the init » (le code actuel attend 10 ms). Errata du P4 : **ROM-816** (v3.2) confirme le risque de blocage si la séquence d'alimentation de la flash s'exécute deux fois avec `rom_download_xpd_on` ; **aucune erratum listée ne décrit des erreurs de données sur des lectures SPI1 en v3.2** (MSPI-751 ne concerne que la v3.0 et la PSRAM). Issue #122 (ouverte) : plantage au passage DCDC sur un autre P4 v3.2, attribué par Espressif à l'alimentation du cœur.

**Historique, point important.** Jusqu'au stub **v1.0.0** (donc les stubs d'esptool 5.3.x), `main.c` n'appelait `stub_lib_clock_init()` **que pour les transports USB OTG, USB-Serial/JTAG et SDIO**, pas pour l'UART (commentaire : « concerns about instability … DBIAS voltage not being set »). Le **v1.1.0** (2026-07-23, « enable UART clock boost on safe chips ») l'a étendu à l'UART pour le P4, avec la nouvelle séquence DCDC/200 MHz du PR #120. **Notre carte, par pont CH343 (UART), est donc passée d'un stub qui ne changeait ni l'alimentation ni l'horloge à un stub qui le fait.** Entre le v0.8.1/v1.0.0 et le v1.2.2, le chemin de lecture 4 octets (`flash.c`, `flash_4byte.c`, répartition ECO7) est **inchangé** (le seul écart de `flash.c` est une garde de compilation sans effet sur P4).

### 8.4 Isoler un facteur : ce que disent les sources, sans rien construire

- **Dans les sources**, le facteur est isolé par **un seul appel gardé** (`main.c:47-54`, fonction `stub_lib_clock_init()` et le réglage du diviseur UART qui le suit). Un stub qui ne l'exécuterait pas sur UART garderait exactement le même chemin de lecture, la même attache ECO7 et le même contrôle d'erreurs. **Rien n'a été construit** et construire un tel stub suppose une chaîne de compilation RISC-V et un moyen de le charger (esptool-js n'en offre aucun, voir l'étude du stub v1.4.1).
- **Le stub publié d'esptool 5.3.x n'est pas une comparaison à un seul facteur** : outre l'absence de basculement DCDC/200 MHz en UART, il appelle `stub_lib_flash_attach(0, false)` après l'initialisation (supprimé ensuite), n'a pas la garde `PMU_DATE` sur l'attache ECO7, et son `command_handler.c` diffère (+78/−15 lignes). Son seul intérêt est de montrer qu'un P4 v3.2 par UART a fonctionné sans cette bascule (issue #122, autre carte).
- **Sûreté, sur le papier** : le stub ne s'exécute qu'en RAM, il n'y a ni écriture ni effacement dans ce chemin de lecture, et l'absence de bascule DCDC laisse la carte dans l'état d'alimentation de la ROM, qu'elle connaît déjà. Aucun chemin destructeur n'est identifié ; le risque principal est un stub qui ne démarre pas (récupération par réinitialisation) ou qui lit autrement. **Ce n'est pas une validation** : aucune exécution n'a eu lieu, rien n'est autorisé.

### 8.5 Conclusion mise à jour

Toujours **C**, mais plus étroit : le chemin de lecture ECO7 (inchangé entre les versions), un motif de lecture structuré par morceaux de 16 octets compatible avec un décalage d'échantillonnage, et un facteur d'environnement (DCDC, 200 MHz, bus rapides) qui n'existait pas pour l'UART avant le stub v1.1.0. Les sources ne permettent pas de dire lequel agit. Aucune erratum documentée ne correspond.
