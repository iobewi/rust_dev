# Audit — divergence des données de T (run-004) et de A1/A2 (run-005)

Date : 2026-11-20. Audit **statique, hors matériel**. Constat : A1 = A2 (SHA-256 `236f5efc…`, ≈ 23,09 s, lecture standard stable) ; T lit la même région (`0x8000`, 1 Mio) en 23,07 s avec le SHA-256 `d5497bfb…` (MD5 `edac8f9c…`) ; `analyze_trace.py` : INVALID. Le MD5 annoncé par le stub est égal au MD5 recalculé par l'hôte : les octets reçus sont ceux que le stub a envoyés ; la divergence est donc **en amont de l'émission** (octets lus par le stub) ou **dans le contexte** de la lecture. Les **faits** viennent du code cité ; les **hypothèses** sont marquées.

## 1. Sources examinées
| Objet | Source |
| --- | --- |
| Stub standard A1/A2 | `esptool-js@0.7.0` `lib/targets/stub_flasher/esp32p4.json` (révision ≥ 3.00) |
| Stub standard compilé | `build-final/src-stock` (esp-flasher-stub v1.2.2, `esp-stub-lib` `d61983fa…`) |
| Stub instrumenté T | `build-final/src-traced` (même tag, patch `apply_trace.py`) |
| Hôte A1/A2 | `esp-engine.js` `readFlash` (lecteur `reference`) → `esptool-js` `ESPLoader.readFlash` |
| Hôte T | `p4-stub-trace.js` `readFlashWithP4StubTrace`, `p4-diagnostic.js` |

**Fait** : le JSON embarqué par esptool-js 0.7.0 est **identique** au stub standard compilé (texte 8176 octets, données 204 octets, mêmes SHA-256 de segments, entrée `0x4ff51456`, `data_start` `0x4ff72db8`). Le stub instrumenté diffère : texte **8952** octets, données **276** octets, entrée `0x4ff515ee`, `data_start` `0x4ff72df8`.

## 2. Différences réelles entre stub standard et instrumenté (diff des sources)
Seuls deux fichiers diffèrent : `esp-stub-lib/src/usb_otg.c` et `src/command_handler.c`. `main.c` et **toutes les sources de flash** (`flash.c`, `target/esp32p4/src/flash.c`, `target/common/src/flash.c`, `flash_4byte.c`) sont **identiques**.
- `usb_otg.c` : `stub_lib_usb_otg_tx_flush` encadre l'appel inchangé `esp_rom_cdc_acm_fifo_fill(...)` par deux lectures du compteur de cycles (`rdcycle`) et incrémente deux compteurs statiques.
- `command_handler.c` : deux opcodes ajoutés (`0xDF` lecture tracée, `0xE0` rapport IDT1). La lecture tracée appelle **la même** `s_read_flash` ; dans `s_read_flash_post_process`, des lectures de `rdcycle` entourent `stub_lib_flash_read_buff`, `stub_lib_md5_update`, `send_frame` et la réception d'ACK. **Aucune écriture dans le tampon de données** ; les arguments (`aligned_offset`, `aligned_size`, `actual_data`) et l'ordre des appels sont inchangés ; la seule branche ajoutée sort en erreur (`RESPONSE_FAILED_SPI_OP`) sans émettre de données.
- Conséquence sur la mémoire : ≈ 60 octets de variables statiques (16 dans `usb_otg.c`, 44 dans `command_handler.c`) supplémentaires dans `.bss` (qui commence à `0x4ff60000`, 77 Ko avant `.data`) ; aucun chevauchement constaté avec le texte (`0x4ff50000` + 8952 octets).

## 3. Chemin de lecture : comparaison de référence
| | A1/A2 (lecture standard) | T (lecture instrumentée) | Écart / conséquence possible |
| --- | --- | --- | --- |
| Fonction hôte | `ESPLoader.readFlash(addr, size, cb)` | `readFlashWithP4StubTrace(loader, addr, size)` | aucun effet sur le contenu |
| Opcode / fonction du stub | `ESP_READ_FLASH` `0xD2` → `s_read_flash` | `0xDF` → **même** `s_read_flash` | identique |
| Arguments | `(addr, size, 0x1000, 1024)` | `(addr, size, 4096, 1)` | le stub ignore le 4ᵉ (`max_unacked_packets = 1` en dur) : identique |
| Adresse et longueur | `0x8000`, 1 048 576 | `0x8000`, 1 048 576 | identiques |
| Source des octets | `stub_lib_flash_read_buff(aligned_offset, data, aligned_size)` | idem | **même fonction** ; pour une flash > 16 Mio : `stub_target_flash_4byte_read` par blocs `FLASH_READ_CHUNK_SIZE`, via `exec_read_cmd_4b` |
| Tampon | `uint8_t data[4102]` aligné 4, sur la pile | idem | identique ; offset 4 aligné : pas de `memmove` |
| Traitements | MD5 sur `actual_data`, `send_frame(actual_data)` | idem + compteurs de cycles | passifs (§2) |
| Hôte : réception | `transport.read`, concaténation cumulative, ACK = octets cumulés | `transport.read`, `data.set(p, received)`, ACK = octets cumulés | sémantique identique ; T vérifie `p.length ≤ 4096` et `received + p.length ≤ size` |
| Vérification | MD5 hôte calculé sur l'artefact | MD5 hôte = MD5 du stub (sinon `idlab_trace_md5_mismatch`) | **T prouve l'intégrité stub → hôte** |
| **Contexte d'initialisation** | session normale : `main()` → `postConnect()` (dont `powerOnFlash()` ECO7) → stub standard → lecture | session « ROM seule » : détection sans reset, **pas** de `postConnect()` → stub instrumenté chargé en RAM (blocs 0x800) → lecture | **seule différence de fond** (§4) |

## 4. Anomalies confirmées et hypothèses
**Anomalie confirmée dans le chemin de lecture, le traitement ou la transmission : aucune.** Le code de lecture, les adresses, le découpage en 256 paquets de 4 Kio, le calcul MD5 et l'émission sont identiques ; l'instrumentation est passive ; l'hôte de T conserve les octets reçus sans décalage (le MD5 l'établit).

La divergence vient donc de **ce que lit le stub** dans un **état matériel différent**. Hypothèses (aucune démontrée) :
1. **H-attach** — `stub_target_flash_attach` (P4) choisit entre `esp_rom_spiflash_boot_attach(…, true)` et `esp_rom_spiflash_attach(…)` selon `_rom_eco_version >= 7`, l'eFuse `DOWNLOAD_MODE_XPD_ON` **et** `(PMU_DATE_REG & 0x3) == 0x3`. Or, en session normale, `powerOnFlash()` de l'hôte **modifie `PMU_DATE_REG`** (libération du « force-on » ou séquence complète) **avant** le stub ; en « ROM seule », cette étape n'a pas lieu. Les deux sessions peuvent donc faire exécuter au stub **deux fonctions ROM d'attache différentes**, avec des configurations de lecture SPI possiblement différentes. C'est la différence de code la plus directe ; elle dépend de valeurs jamais lues (eFuse, `PMU_DATE_REG`).
2. **H-alimentation** — état d'alimentation de la flash différent (voir `AUDIT-powerOnFlash-ECO7.md`) ; la flash est lisible (données ni tout-`0xFF` ni tout-`0x00`).
3. **H-config SPI/horloge** — paramètres de lecture SPI1 (mode, cycles factices, diviseur) hérités de l'état ROM différemment selon la session ; l'horloge CPU déduite de T (≈ 160 MHz) n'a pas été comparée à celle d'A1.
4. Exclues par le code : une altération des tampons par le traçage, une erreur de découpage/adresses côté hôte, une perte ou une duplication de paquet (le MD5 de bout en bout le détecterait).
Limite de l'audit : le code ROM (`spi_flash_attach`, `spi_flash_boot_attach`, `esp_rom_opiflash_exec_cmd_eco7`) est fermé ; le script de liens et la carte mémoire complète n'ont pas été audités.

## 5. Suite : instrumentation (cette PR)
L'audit n'établit pas la cause : cette PR ajoute au résultat de T un **SHA-256 par paquet de 4 Kio reçu et validé** (`packet_hashes` : numéro, adresse NOR absolue, taille effective, SHA-256, rang et état de l'ACK), calculé **après** le flux et après la prise du temps (aucun effet sur les ACK ni sur `host_duration_ms`), sans exposer d'octet de flash. L'outil `compare_packet_hashes.py` compare ces empreintes à `A2.bin` (privé) : nombre de paquets identiques/différents, plages, motif périodique, répartition entre paquets vides (`0xFF`) et paquets avec données. Une empreinte de paquet **localise** les paquets différents ; elle ne donne pas les octets différents dans un paquet.
Ce que ces empreintes pourront dire : tous les paquets contenant des données diffèrent et les vides non (→ contenu lu en mode différent) ; différences périodiques (→ défaut de cadence ou de configuration) ; un seul bloc (→ incident local). Elles ne tranchent pas à elles seules entre H-attach, H-alimentation et H-config SPI : la lecture seule de l'eFuse et de `PMU_DATE_REG` (deux `ESP_READ_REG`) resterait nécessaire et exige une autorisation matérielle distincte.
Aucun matériel n'est utilisé par cette PR ; une nouvelle tentative T (récupération, grant, autorisation explicite) reste nécessaire pour produire les empreintes.
