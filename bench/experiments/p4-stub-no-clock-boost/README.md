# Variante expérimentale du stub P4 sans bascule DCDC / 200 MHz sur UART

**Statut : étude et préparation hors matériel. Rien n'est chargé sur le P4, rien n'est déployé, le stub de production (celui d'esptool-js 0.7.0) n'est pas remplacé.** Les binaires ne sont pas dans le dépôt : ils se reconstruisent avec `build.sh` et sont conservés, avec leurs empreintes, dans le dossier de preuves privé (`$BENCH_STATE_DIR/qualification-evidence/stub-variant-v122-noboost-20261112/`).

## 1. Question et variante

Les lectures du stub v1.2.2 sont instables sur notre P4 v3.2 (PC-7, PC-8) alors que le MD5 de la ROM est stable (PC-9). Le stub v1.2.2 diffère du v1.0.0 par un point pour le transport UART : il exécute `stub_lib_clock_init()` (bascule du cœur sur le DCDC externe, CPU de la fréquence de la ROM à 200 MHz). La variante **ne le fait pas pour le transport UART du P4** ; elle le garde pour USB et SDIO et ne change rien pour les autres puces. Le chemin de lecture ECO7 est conservé tel quel.

Le patch (`p4-uart-no-clock-boost.patch`, 1 fichier, +17/−10 lignes, `src/main.c`) ajoute une garde de compilation `STUB_P4_UART_NO_CLOCK_BOOST`. **Sans la macro, le code compilé est identique à v1.2.2 (octet pour octet).**

## 2. Reproductibilité

`./build.sh` (environnement vierge : 33 s) :
1. télécharge les sources aux commits épinglés et vérifie leur arbre (`esp-flasher-stub` `6f8dc174…`, `esp-stub-lib` `d61983fa…`) ;
2. vérifie la chaîne de compilation (`riscv32-esp-elf` 15.1.0_20250607, SHA-256 `9bfe9b4f…`, publiée par Espressif) et les outils (cmake 4.4.4, ninja 1.13.2, pyelftools 0.33 ; la CI d'Espressif utilise ceux d'Ubuntu 24.04) ;
3. construit v1.2.2 non modifié et **exige** que son `esp32p4.json` soit identique à celui publié (embarqué par esptool-js 0.7.0) : **c'est le cas** (`text` `722c15bb…`, `data` `82d043b5…`, entrée `1341461590`) ;
4. applique le patch, construit avec la macro éteinte puis allumée, et compare.

Deux exécutions indépendantes ont donné les mêmes empreintes.

## 3. Comparaison des binaires et des configurations

| | Référence v1.2.2 | Patch, macro éteinte | Variante (macro allumée) |
|---|---|---|---|
| SHA-256 de l'ELF (16 premiers car.) | `becaef313c091fd9` | `becaef313c091fd9` | `42fed3f5624492b3` |
| `text` du JSON | `722c15bbe563df34` | identique | `cc2865bba8c3984e` |
| `data`, entrée, `text_start`, `data_start`, `bss_start` | — | identiques | **identiques** |
| Tailles de sections, table des symboles (noms, adresses, tailles) | — | identiques | **identiques** |
| Fichier `.map` | — | identique | identique (aux noms temporaires LTO près) |
| Commande de compilation de `main.c` | — | identique | identique, sauf `-DSTUB_P4_UART_NO_CLOCK_BOOST` |
| Commande d'édition de liens | — | identique | identique, sauf la même définition |
| Octets de `.text` modifiés | — | 0 | **669 sur 8174, tous dans `esp_main`** |
| Instructions de `esp_main` | 938 | 938 | 938, mêmes appels de fonctions ROM |

L'éditeur de liens inlinant tout dans `esp_main`, les 669 octets viennent de la réorganisation des registres et des branches autour de la nouvelle condition. Vérification dans le désassemblage de la variante : deux branches nouvelles (`+0x52` et `+0x5c`) sautent, pour le transport UART, **directement à `+0x29c`**, qui est l'appel de `stub_target_flash_attach` (début de `stub_lib_flash_init`), c'est-à-dire **après** tout le bloc : régulateurs PMU, diviseurs APB/SYS/MEM/CPU, sélection de la racine d'horloge, `ets_delay_us(10000)` et reprogrammation du diviseur UART. Pour USB OTG et USB-Serial/JTAG, le bloc est exécuté comme avant. Aucun émulateur du P4 n'est disponible : l'équivalence est établie par le source, la compilation et ce désassemblage, pas par exécution.

## 4. Dépendances de `stub_lib_clock_init()` (v1.2.2)

| Ce que l'appel modifie | Qui en dépend | Conséquence de l'absence de bascule |
|---|---|---|
| Régulateurs PMU, DCDC (`clock.c:106`) | Rien dans le stub | La carte reste sur le régulateur HP interne, état de la ROM |
| Diviseurs APB/SYS/MEM/CPU, racine CPLL (`:147`) | Rien dans le stub | Le CPU garde la fréquence de la ROM |
| `esp_rom_set_cpu_ticks_per_us(200)` (`:184`) | `ets_delay_us`, donc `stub_lib_delay_us` (attentes de la flash, 7 appels dans `esp_main`) | Reste calibré par la ROM : les délais restent cohérents avec la fréquence réelle |
| `s_cpu_freq` / `stub_target_get_cpu_freq()` | **Aucun appelant** dans le stub ni la bibliothèque (vérifié par recherche) | Sans objet |
| Attente de 10 ms (`src/clock.c`) | Rien | Sans objet : elle ne sert qu'à laisser les horloges se stabiliser après la bascule (correctif ECO7, esp-stub-lib #87/#91) |
| Diviseur UART (`main.c`) | Calculé à partir de la fréquence du cristal (`(xtal << 4) / baud`), **pas du CPU** | Sans objet : le saut de la reprogrammation après bascule est neutre |

La fréquence de la ROM n'est pas connue avec certitude : le commentaire du code dit ~100 MHz (CPLL/4), mais esp-stub-lib #124 note que la ROM laisse souvent la racine sur le cristal après un reset matériel, soit 40 MHz. La variante ne change aucun des deux cas ; elle exécute le stub tel que le faisaient les versions ≤ v1.0.0 (stubs d'esptool 5.3.x) sur UART, avec le même chemin de lecture.

## 5. Hypothèses de temporisation et risques

- **Temporisations** : les attentes du stub comptent des itérations de `delay_us(1)` calibrées par la ROM ; elles restent cohérentes. Aucune ne suppose les 200 MHz.
- **Débit UART** : à fréquence CPU plus basse, la réception à haut débit (460800, valeur par défaut du banc, ou 921600) a moins de marge ; c'était la motivation de l'activation de la bascule sur UART (esp-flasher-stub #96). Risque de **trames perdues**, pas de risque matériel ; un débit plus faible l'écarte. Les stubs ≤ v1.0.0 ont fonctionné ainsi (issue #122 : sondes à 115200 et 921600 réussies sur un autre P4 v3.2).
- **Matériel** : le stub s'exécute en RAM ; ce chemin ne contient ni écriture ni effacement tant que l'hôte n'envoie que des lectures ; aucune modification d'eFuse ; l'attache de la flash et la séquence XPD sont **inchangées**. Le DCDC n'est pas sollicité : l'état d'alimentation est celui de la ROM. Aucun risque matériel nouveau n'est identifié.
- **Risques logiciels résiduels** : un stub qui ne démarre pas (récupération par un nouveau reset, comme pour tout échec de stub) ; des lectures différentes (c'est précisément ce qu'on mesurerait).
- **Ce n'est pas une validation** : aucune exécution n'a eu lieu.
- **Différences avec les stubs d'esptool 5.3.x** : la variante n'a que ce changement par rapport au v1.2.2, ce qui la rend plus propre que ces stubs (qui diffèrent aussi par l'attache, la garde `PMU_DATE` et le gestionnaire de commandes).

## 6. Procédure proposée (non exécutée, à décider)

0. **Revue** du patch, du script et des résultats par le réviseur indépendant ; relancer `build.sh` pour vérifier les empreintes.
1. **Moyen de chargement** : esptool-js n'a aucune option pour un autre stub. Voies : (a) un agent du navigateur modifié qui injecte ce JSON uniquement sur demande explicite (code servi modifié : revue, déploiement contrôlé, consentement) ; (b) esptool Python 5.6.0 comme bibliothèque sur le PC de l'opérateur, port libéré dans la page (`StubFlasher.STUB_DIR` permet de pointer un dossier de stub), ce qui déroge à `AGENTS.md` et suppose une décision explicite de l'opérateur et des preuves saisies à la main.
2. **Mesure** : mêmes plage et gabarit que PC-7 (`0x2000`, 4096 octets, trois MD5 et deux lectures, une session par commande), avec le stub de référence **et** la variante le même jour, pour ne pas comparer à d'anciens résultats. Le MD5 de la ROM (`ec81b89835f389ad33ffa0851531dcec`) sert de vérité de terrain : un MD5 du stub égal à celui-ci est correct.
3. **Critères écrits avant** : variante stable et égale au MD5 de la ROM, référence instable → le facteur DCDC/200 MHz est impliqué ; variante instable comme la référence → il est écarté ; variante qui ne démarre pas ou lectures incohérentes avec la ROM → non concluant, sans en déduire de cause.
4. **Arrêt** au premier échec, aucune relance ; retour par reset ; aucune écriture, aucun effacement. Débit de liaison à choisir pour limiter le risque de §5.

Hors périmètre : le C6, GPIO54, PC-BACKUP, toute décision de remplacer le stub de production.
