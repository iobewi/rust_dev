# IDLab — Qualification du diagnostic USB OTG de l'ESP32-P4

Dossier de qualification **logicielle et statique**. Aucune opération n'a été faite sur une carte pour l'établir.
Les quatre niveaux ci-dessous sont distincts et ne se déduisent pas les uns des autres.

| Niveau | État | Preuve |
| --- | --- | --- |
| 1. Code **compilé** | oui | stub standard et stub instrumenté compilés pour ESP32-P4 avec `-Werror`, 0 avertissement ; deux builds indépendants donnent les mêmes SHA-256 |
| 2. **Tests logiciels** réussis | oui | 26 tests JavaScript, 53 tests Python (dont 10 exécutés contre le vrai C en natif avec une horloge simulée) |
| 3. Binaire **qualifié statiquement** | oui | 16/16 contrôles ELF/JSON pour chaque image, voir section 5 |
| 4. **Validation matérielle** | **non** | aucun chargement RAM, aucune lecture, aucun reset n'a eu lieu |

« Qualifié statiquement » signifie : image cohérente, qui tient dans les régions RAM déclarées par le script d'édition de liens
du stub. Cela ne dit rien du comportement sur la puce (ROM, timing USB, compteur de cycles réel).

## 1. Environnement IDLab : inventaire factuel

Relevé dans le conteneur de développement existant (image `docker/Dockerfile`, `.devcontainer/`), avant toute installation.

| Élément | Constat |
| --- | --- |
| Rust | `rustc`/`cargo` 1.99.1, cibles `riscv32imc`, `riscv32imac`, `riscv32imafc-unknown-none-elf`, toolchain `esp` (espup 0.19), `espflash` 4.7.0 |
| C / build | `gcc` 12.2 (hôte), `clang`, CMake **3.25.1**, Ninja 1.11.1, Python 3.12, Node 22 |
| Compilateur **RISC-V Espressif** | **absent** : `espup` n'installe ici que `xtensa-esp-elf` 16.1.0 ; aucun `riscv32-esp-elf-gcc` |
| CMake | 3.25.1 < **3.28** exigé par `esp-flasher-stub` |
| `pyelftools` | absent (requis par l'amont pour l'analyse ELF) |
| Crates Rust du dépôt | **aucune** : `rust_dev` ne contient ni `Cargo.toml` ni crate ; le banc est Python (serveur, client) et JavaScript (agent navigateur) |
| Chargement RAM | possible : esptool-js 0.7.0 expose `memBegin`/`memBlock`/`memFinish` ; `runStub()` charge le stub standard par ce moyen |
| Réutilisables | client Web Serial et agent (`bench/web`), protocole et validation (`bench/protocol`), artefacts, grants, réservations, client Python, CI `bench.yml` (`pytest python/tests`, `npm test`) |

**Écarts pour compiler le stub P4** (le fait qu'une toolchain Rust soit présente ne garantit pas la toolchain C) :
`riscv32-esp-elf-gcc`, CMake ≥ 3.28, `pyelftools`. Comblés pour cette qualification **hors du dépôt et du conteneur**, dans des répertoires isolés :
- `riscv32-esp-elf` GCC 15.1.0, crosstool-NG `esp-15.1.0_20250607`, exactement celui que `tools/setup_toolchains.sh` de l'amont épingle ;
  archive de 267 310 332 octets, SHA-256 `9bfe9b4fe08bd07614d085482301f5206a15a075ce4e9cf1c9c3d9d3e0d6b110`, vérifié contre le digest de l'API GitHub **et** contre le fichier de sommes du release ;
- CMake 3.31.10 (roue PyPI) et pyelftools 0.33 dans un environnement virtuel dédié.

**Modification minimale proposée pour l'image de développement** (non appliquée : la CI `devcontainer.yml` reconstruit l'image) :
ajouter à `docker/esp-tools.json` l'archive `riscv32-esp-elf-15.1.0_20250607-x86_64-linux-gnu.tar.xz` avec son SHA-256 ci-dessus, `pip install pyelftools 'cmake>=3.28'`,
et exposer `riscv32-esp-elf/bin` dans le `PATH`. Sans cela, `build_stub.py` refuse de démarrer avec le blocage exact.

## 2. Arbitrage Rust / C

| Option | Faisabilité ici | Comparabilité | Décision |
| --- | --- | --- | --- |
| **A. Stub Espressif C instrumenté, pilotage Rust/JS** | **démontrée** : build reproductible, 0 avertissement | **maximale** : seule la lecture de cycles et 4 compteurs sont ajoutés | **retenue** |
| B. Instrumentation côté Rust/navigateur seule | oui (déjà en place : profilage des arrivées brutes, PR #67) | ne voit pas l'intérieur de la ROM : n'isole ni `send_frame`, ni `fill`, ni le MD5 | complément, déjà acquis |
| C. Stub entièrement Rust | non justifiée | perdue : ré-implémenter le protocole esptool, le transport USB CDC et la lecture flash changerait ce qu'on mesure | **écartée** |

Justification. (1) Le stub standard P4 embarqué dans **esptool-js 0.7.0** (`esp32p4.json`) est **identique octet pour octet** au stub que cette chaîne compile depuis `v1.2.2`
(texte 8 176 o, données 204 o, entrée `0x4ff51456`, mêmes adresses) : la base de comparaison est exactement le stub de production, et la toolchain reproduit l'image officielle.
(2) La question posée porte sur ce que fait **ce** stub avec la ROM USB CDC ; un stub réécrit répondrait à une autre question. (3) Le C est limité à l'instrumentation de deux fichiers (118 lignes ajoutées, 1 supprimée) ;
tout le reste (orchestration, chargement, vérification, analyse, preuves) est du JavaScript du banc et du Python, sans déplacer de responsabilité vers le serveur.
Aucune réécriture n'est engagée. Pour un correctif amont éventuel, l'option A est aussi la seule directement proposable.

## 3. Revue de la PR #68 : constats et corrections

| # | Point du mandat | Constat | Action |
| --- | --- | --- | --- |
| 1 | Compatibilité `v1.2.2` | le patch s'applique et compile sur le tag exact | préflight : tag, sous-module et arbres propres exigés |
| 2 | Commit du sous-module | `esp-stub-lib` = `d61983fa4d1f66b9c088608c1f702ad877121279` (gitlink du tag `v1.2.2` = `23959b78…`) | vérifié par le script avant d'écrire quoi que ce soit |
| 3 | Points d'insertion | uniques, 2 fichiers (`src/command_handler.c`, `esp-stub-lib/src/usb_otg.c`), le bon `usb_otg.c` (celui du transport actif, `ESP_ROM_USB_OTG_NUM = 5` sur P4) | **corrigé** : écriture atomique (rien n'est écrit si un point échoue), refus d'une seconde application |
| 4 | Compilation P4 | OK, `-Werror -Wconversion -Wshadow`, 0 avertissement ; `rdcycle` accepté en `-march=rv32imc` | build reproductible `build_stub.py` |
| 5 | ABI / protocole | réponse maximale `MAX_RESPONSE_DATA_SIZE = 64` = rapport IDT1 (assert statique ajouté) ; octets d'état ajoutés par `s_send_response` ; client JS conforme à `checkCommand` d'esptool-js 0.7.0 (5ᵉ argument = longueur de réponse, 6ᵉ = délai) et à `readFlash` (`transport.read` renvoie un `Uint8Array`, ACK cumulatif de 4 octets) | assert statique ; tests |
| 6 | Intégrité du flux et ACK cumulatifs | la fenêtre est **1 paquet par construction amont** (`max_unacked_packets = 1`) ; la boucle n'est pas modifiée, seuls des compteurs sont ajoutés | banc natif sur le vrai C : octets, paquets et ACK exacts |
| 7 | Recouvrement RAM | aucun : `.text` 8 950 o dans iram (0x4500, 8 714 o libres), DRAM 77 578 o sur 0x18000 (≈ 20,7 ko libres) ; le stub n'installe pas sa pile | qualification statique |
| 8 | Compteurs CPU | ESP-IDF lit le compteur P4 par le CSR standard `cycle` (le cœur n'a pas les CSR de performance propriétaires) : `rdcycle` est cohérent ; deltas 32 bits sûrs jusqu'à 21,47 s à 200 MHz, cumuls 64 bits ; le stub amont règle le CPU à **200 MHz** (`CPU_FREQ_MHZ`, CPLL 400 MHz / 2) | **ajouté** : test de vivacité du compteur dans la sonde `0xE0` (échec fermé si le compteur n'avance pas) ; recoupement compteurs / temps mur |
| 9 | Erreurs, timeouts, déconnexion | côté C : erreur flash arrête la trace et l'erreur est reportée sur la commande suivante (comportement amont) ; ACK mal formé laisse un écart `paquets ≠ ACK` que le client détecte. Côté JS : tout échec après le début du flux est `sessionIndeterminate`, sans relance | tests des deux côtés |
| 10 | Changements amont | 37 commits depuis `v1.2.2` (jusqu'à `v2.0.0`, 2026-11-13) : transports SPI/SDIO, plugins, ECO8 ; la logique `READ_FLASH` et la plage plugin sont inchangées | on reste sur `v1.2.2`, qui est le stub d'esptool-js 0.7.0 ; à requalifier si esptool-js change |

**Points relevés et traités ou documentés**
- **Collision d'opcodes** : `0xDF` et `0xE0` sont dans la plage plugin amont `0xD5`–`0xEF` ; les `case` explicites masquent ces deux emplacements. Sans effet sur cette image (aucun plugin n'y est chargé),
  mais l'image ne doit jamais être combinée à des plugins. Documenté dans le C et ici. Les opcodes demandés par le mandat sont conservés.
- **Sentinelle ACK** : `ack_started == 0` pouvait confondre un compteur valant 0 avec « pas d'ACK en attente » ; remplacé par un booléen.
- **Compteur valant zéro / inhibé** : sonde `0xE0` en échec fermé, `RESPONSE_CMD_NOT_IMPLEMENTED`.
- **Cible** : `-DTARGET_CHIP=esp32p4` est correct pour votre carte : esptool-js choisit `esp32p4.json` pour une révision ≥ 3.00 (`getChipRevision() = major×100 + minor`), et la carte du banc est en révision 3.2. Le chargeur refuse toute révision < 3.00.
- **Effets de bord hérités** : à une demande de reset par RTS, le stub amont (standard comme instrumenté) efface `FORCE_DOWNLOAD_BOOT` et positionne les drapeaux de persistance USB. Aucun réglage persistant n'est ajouté ici.

## 4. Ce qui a été construit

| Pièce | Rôle |
| --- | --- |
| `apply_trace.py` | génère l'image instrumentée : préflight, atomique, idempotence refusée |
| `build_stub.py` | build reproductible des deux images + manifeste ; refuse tout outil ou source non conforme ; ne réutilise jamais un dossier de preuves |
| `qualify_stub.py` | qualification statique ELF/JSON (régions, chevauchements, JSON = ELF, symboles, `rdcycle`, table d'opcodes liée `{0xDF, 0xE0}`) |
| `host_harness/` | banc natif : compile les **vrais** fichiers C patchés avec une horloge simulée et rejoue 8 scénarios |
| `analyze_trace.py` | analyse de la recette : intégrité, stabilité, cycles→µs, recoupement d'horloge, décomposition, verdict |
| `bench/web/p4-stub-trace.js` | client des opcodes diagnostiques, conversions cycles/µs, erreurs « session indéterminée » |
| `bench/web/p4-stub-loader.js` | chargeur **RAM seul** vérifié (consentement, SHA-256, puce, révision, session ROM, fenêtres RAM, identification) |

Rien de ce qui précède n'est référencé par le chemin de lecture standard (`esp-engine.js`, `ui.js`, `agent.js`) : le comportement par défaut du banc est inchangé.

## 5. Preuves de compilation et de qualification statique

Build du 2026-11-19, chaîne `riscv32-esp-elf-gcc` 15.1.0 (esp-15.1.0_20250607), CMake 3.31.10, Ninja 1.11.1, pyelftools 0.33, sources `esp-flasher-stub` v1.2.2 (`23959b78…`) + `esp-stub-lib` `d61983fa…`.

| Image | ELF (SHA-256) | JSON de chargement (SHA-256) |
| --- | --- | --- |
| standard | `becaef313c091fd91ea8c627543e00aef1c0625756ad3e3ed43c2e5cd19e0b4d` | `8cae9e8aec3de3433394cc347ebe49f54d21fcfc04e088a404bca5ae0eb07870` |
| instrumentée | `0efe01a2834b2b03287483cf34638484868806e9bceb5b9cac508057b1082967` | `ad4f92596691a0557db5dc0faaf236d550392b2a361e119a670d0d7cc70d800c` |

| | standard | instrumentée | écart |
| --- | --- | --- | --- |
| `.text` (iram `0x4ff50000`) | 8 174 o | 8 950 o | +776 |
| `.bss` (dram `0x4ff60000`) | 77 240 o | 77 304 o | +64 |
| `.data` | 204 o | 274 o | +70 |
| entrée | `0x4ff51456` | `0x4ff515ee` | |
| lectures de cycles (`rdcycle`) | 0 | 12 | |

Contrôles passés (16 sur 16 par image) : trois sections présentes ; chaque section dans sa région ; aucun recouvrement ; entrée dans `.text` ; JSON à six clés exactes ;
adresses et octets du JSON égaux à l'ELF (segments complétés à 4 octets par des zéros, règle de l'amont) ; image standard sans symbole `idlab_*`, sans `rdcycle`, sans table d'opcodes ;
image instrumentée avec ses compteurs, `rdcycle` ≥ 10 et la table d'opcodes liée exactement `DF E0`.
Les deux builds (dossiers `build-001` et `build-002`) ont produit des SHA-256 identiques : la compilation est déterministe.

Le JSON standard compilé ici est égal, champ par champ, au `esp32p4.json` embarqué dans esptool-js 0.7.0 (comparaison du contenu : les octets de fichier peuvent différer par la mise en forme du JSON).

Fréquence CPU : **200 MHz** d'après le code du stub (`stub_target_clock_init`, `CPU_FREQ_MHZ 200`, CPLL 400 MHz / 2). **Non mesurée.** Elle est fournie explicitement à l'analyse (`--cpu-hz`) et recoupée avec le temps mur du run tracé ; un écart supérieur à 20 % donne le verdict `CLOCK_ASSUMPTION_SUSPECT`.

## 6. Intégration dans IDLab : état

**Câblé de bout en bout (logiciel uniquement, aucun matériel)** : commande dédiée `diagnostic.p4_usb_trace` (jamais une valeur de `reader` de `flash.read`).

| Couche | Ce qui est fait | Fichiers |
| --- | --- | --- |
| Protocole | commande dans `CAPABILITIES` et `PROGRAM` ; paramètres figés (`esp32p4`, `0x8000`, 1 Mio, 4096, 200 MHz supposé) ; artefact + SHA-256 complet ; JSON du stub validé (clés exactes, fenêtres IRAM/DRAM) ; consentement dédié comparé strictement (`1` ≠ `true`) lié à la carte, au SHA-256 et à la région ; transport USB natif exigé | `bench/protocol/__init__.py` |
| Serveur | portée `program` + `allow_destructive` + grant local ; exclusivité par carte dans les deux sens ; artefact épinglé tant que l'opération est active ; drapeau durable `diagnostic_resident` levé au démarrage, quarantaine à toute fin ; **seule `mark` est acceptée** tant qu'il est levé ; seul `recover` (geste local) le baisse | `bench/server/core.py` |
| Agent | commande privilégiée (grant local, bornes de bail) et dans le journal | `bench/web/agent.js`, `agent-journal.js` |
| Moteur | capacité annoncée seulement pour un P4 USB natif avec entrée manuelle activée par l'opérateur ; **session ROM dédiée** (`detectChip('no_reset')`, jamais `runStub()`, aucun reset) ; revalidation locale complète ; SHA-256 du stub téléchargé ; *hold* levé avant la première écriture sur la puce ; après le lancement : `indeterminate`, sans reprise, sans reset, sans console | `bench/web/esp-engine.js`, `p4-diagnostic.js` |
| Chargeur / lecteur | inchangés (déjà qualifiés) : SHA-256, puce, révision, session ROM, signature, ACK cumulatifs, MD5 final, IDT1, cohérence des compteurs | `p4-stub-loader.js`, `p4-stub-trace.js` |
| Interface | panneau discret « Diagnostic USB OTG P4 (expérimental) », visible seulement si la capacité est offerte ; consentement RAM = case dédiée, usage unique, remise à zéro à chaque nouveau stub | `index.html`, `ui.js` |
| Client Python | même verrou que l'écriture : consentement de campagne **et** de commande | `python/src/iobewi_bench/client.py` |
| Recette | pilote `idlab_recipe.py` : **affiche le plan par défaut**, n'exécute que sur `--execute` avec une autorisation de l'opérateur correspondant à la carte, au scénario et au SHA-256 ; les gestes locaux restent manuels | `idlab_recipe.py` |

**Décision sur la session ROM.** esptool-js charge le stub standard dès `main()` : une session ordinaire d'IDLab n'est donc jamais une session ROM. Le diagnostic ouvre sa propre session (`detectChip` seul) et exige que l'opérateur ait mis la carte en mode ROM (BOOT + reset, entrée manuelle activée) ; si le stub standard tourne encore, le chargeur refuse avant toute écriture (`diagnostic_stub_requires_a_rom_session`) et rien n'est retenu. Aucun reset n'est jamais fait par IDLab.

**Corrections issues de la revue indépendante (commit suivant `2be8062`).**
1. *MD5* : le lecteur recalcule le MD5 des octets reçus et le compare à celui du stub ; divergence = `idlab_trace_md5_mismatch`, session indéterminée (test de corruption avec trames valides).
2. *Retour en ROM* : `recover` est refusé (`rom_proof_required`) tant que la carte est `diagnostic_resident` sans `rom_verified: true`. L'agent produit cette preuve lui-même (`verifyRomReturn` : synchronisation ROM seule, sans reset ni écriture, stub absent, puce P4) ; le geste de l'opérateur (BOOT + reset) reste nécessaire mais ne suffit plus. Le mécanisme `recover` existant est réutilisé, pas dupliqué. La confiance dans la preuve repose sur l'agent, propriétaire de l'USB.
3. *Liste d'empreintes qualifiées* : le serveur n'exécute que les SHA-256 de `BENCH_P4_STUB_SHA256S` (liste vide = fonction désactivée, échec fermé, `diagnostic_stub_not_qualified`) ; la validation structurelle du JSON reste appliquée en plus. L'agent ne reçoit pas cette liste : il fait confiance au serveur pour la politique et vérifie l'empreinte elle-même.

4. *Rechargement de page* (second retour de revue) : le message `registered` du serveur porte désormais `diagnostic_resident` ; l'agent en déduit l'état et impose la preuve ROM même si le moteur a été reconstruit. Un serveur qui n'indique pas le drapeau ne permet jamais de lever une quarantaine (`diagnostic_state_unknown`). Tests de régression : séquence « diagnostic → rechargement → enregistrement → récupération ».

**Reste non fait ou non prouvé.** Aucun test navigateur de bout en bout (Playwright absent : seuls des contrôles statiques de balisage ont été écrits) ; aucun essai sur matériel ; la durée de `detectChip` en `no_reset` sur la vraie ROM du P4 et le comportement réel de la ROM face aux commandes RAM ne sont **pas** démontrés ; l'intégration dans la PR #68 n'est pas faite (voir le rapport).

## 7. Tests exécutés

| Suite | Résultat |
| --- | --- |
| `node --test tests/p4-stub-trace.test.mjs tests/p4-stub-loader.test.mjs` | 26 / 26 (16 trace + 10 chargeur) ; les cinq gardes critiques du chargeur ont été retirées une à une : chaque retrait fait échouer un test |
| `pytest python/tests/test_p4_trace_patch.py` | 11 / 11 |
| `pytest python/tests/test_p4_trace_tools.py` | 32 / 32 |
| `pytest python/tests/test_p4_trace_host_harness.py` avec `IDLAB_ESP_FLASHER_STUB` | 10 / 10 contre le vrai C (ignorés sans ce checkout) |
| `pytest tests/test_p4_usb_trace_core.py` (broker + protocole) | 31 / 31 ; dix gardes serveur retirées une à une (puis la comparaison d'empreinte refaite proprement) : chaque retrait fait échouer au moins un test |
| `pytest tests/test_p4_usb_trace_http.py` (vrai serveur HTTP + agent websocket scripté) | 7 / 7 : nominal, refus de consentement / SHA / portée / paramètres, stub non conforme, capacité absente, concurrence + isolation multi-cartes, perte d'agent + reconnexion, résultat indéterminé |
| `node --test tests/p4-diagnostic.test.mjs` (P4 simulé, moteur réel) | 14 / 14 ; neuf gardes agent/moteur retirées une à une : chaque retrait fait échouer au moins un test |
| `node --test tests/p4-diagnostic-ui.test.mjs` | 5 / 5 : **statique seulement**, pas un test navigateur |
| `pytest python/tests/test_p4_trace_idlab_recipe.py` | 4 / 4 |
| Contrôles GitHub | CI `software` verte sur les têtes PR #67 et #68 d'origine ; **non rejouée** sur ces modifications, et la CI n'avait pas compilé le patch C |

Limites du banc natif : horloge simulée, transport scripté, hôte 64 bits ; il valide la logique d'instrumentation, pas la ROM, l'USB ni le compteur réel.
