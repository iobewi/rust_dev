# `diagnostic.p4_flasher_read` — lecture seule avec le stub flasher Rust (ESP32-P4 ECO7)

Commande IDLab qui charge en RAM, via la session « ROM seule », le stub flasher Rust (`esp-rs-stub-lib`) puis exécute
**uniquement** un plan borné de lectures. Elle existe parce que `diagnostic.p4_usb_bringup` est figé sur OHAI + 2 PING/PONG et
que `diagnostic.p4_usb_trace` utilise un opcode propriétaire : aucun des deux ne peut exercer `READ_FLASH` / `SPI_FLASH_MD5`
du stub flasher, et rien ne permettait de bloquer ses commandes d'écriture.

## Contrat
- **Objectif** : lire des régions de la NOR, calculer leur SHA-256 et leur MD5 côté hôte, les comparer au MD5 calculé par le stub, mesurer le débit.
- **Effets** : exécution de code en RAM (la carte reste en quarantaine + `diagnostic_resident` ; reset externe obligatoire pour revenir en ROM). Aucune écriture, aucun effacement.
- **Prérequis** : session ROM seule sur USB-OTG natif, ESP32-P4 révision 302 exactement ; scopes `control` + `program` ; grant local ; `allow_destructive=True` sur la réservation et sur la commande (comme `bringup`) ; SHA-256 du JSON listé dans `BENCH_P4_FLASHER_SHA256S` (liste distincte de `BENCH_P4_BRINGUP_SHA256S` : l'une n'autorise pas l'autre).
- **Arrêt** : première erreur, longueur incohérente, MD5 différent, statut d'erreur du stub ou délai dépassé → l'opération s'arrête, reste `indeterminate`/en quarantaine, sans nouvel envoi ni reset.

## Arguments
`chip='esp32p4'`, `stub_artifact`, `stub_sha256`, `scenario='read_only_v1'`, `plan`, `consent`.

`plan` (1 à 32 étapes, objets à clés exactes) :
| étape | champs | notes |
|---|---|---|
| `attach` | `op` | optionnelle, uniquement en première position (`SPI_ATTACH`, 4 octets) |
| `read` | `op, address, size, packet_size` | `READ_FLASH` ; `packet_size` ∈ {1024, 2048, 4096} ; `size` ≤ 4 Mio |
| `md5` | `op, address, size` | `SPI_FLASH_MD5` |

Bornes : `0 ≤ address`, `address + size ≤ 32 Mio` ; volume total des lectures ≤ 16 Mio. Aucune étape d'écriture, d'effacement, de registre ou de paramètres n'existe dans le vocabulaire.

`consent` (comparaison stricte) : `scenario` (= nom de la commande), `device`, `stub_sha256`, `plan_sha256`, `ram_code_execution: true`, `flash_write_forbidden: true`, `rom_reset_may_be_required: true`. `plan_sha256` est le SHA-256 du texte canonique `op,address,size,packet_size` joint par `;` (adresse/taille/paquet à 0 s'ils n'existent pas) : le consentement est lié au plan exact.

## Défense en profondeur
1. **Serveur** (`protocol.validate_p4_flasher_read`) : plan, bornes, consentement, SHA-256 + liste blanche, scopes, grant.
2. **Agent** (`p4-flasher-read.js`) : revalide tout localement avant d'écrire sur le port.
3. **Émetteur** : la classe `ReadOnlyFlasher` ne sait construire que `0x0d`, `0x13`, `0xd2`. Tout autre opcode est refusé *avant* l'écriture (`diagnostic_flasher_opcode_not_allowed`) ; le résultat publie `opcodes_sent` et `commands_sent`.
4. Le stub lui-même n'est PAS une barrière : il accepte `FLASH_*`, `ERASE_*` et `WRITE_REG` (registres SPI1). C'est l'hôte qui les interdit.

## Résultat
Par étape `read` : `bytes`, `packets`, `duration_ms`, `throughput_kib_s` (octets utiles / 1024 / s, mesuré du `READ_FLASH` au dernier paquet), `data_sha256` de la capture, `md5_host`, `md5_stub`, `md5_match`. Les données elles-mêmes ne sont jamais renvoyées ni journalisées : deux captures sont comparées par `data_sha256`. Puis `opcodes_sent`, `nor_written: false`, `stub_resident`, `requires_operator_reset`.

## Limites assumées
- Validé **uniquement en simulation** (stub simulé dans `tests/p4-flasher-read.test.mjs`) : le format des trames, la trame MD5 finale de 16 octets et les acquittements cumulatifs viennent de la lecture du code du stub (PR #14 de `esp-rs-stub-lib`) et d'esptool-js 0.7.0, pas d'une observation matérielle.
- Pas de panneau dans l'interface locale : la commande se soumet par l'API.
- Le client Python (`BenchClient`) exige désormais l'opt-in destructif pour `bringup` et `flasher_read`, ce qui corrige l'écart constaté en A-3.
- La comparaison à une référence indépendante de la NOR n'est pas fournie par la commande.
- La persistance durable des erreurs P4 (#81) s'applique aussi à cette commande ; les champs de résultat ajoutés à `SAFE_FIELDS` ne contiennent que des empreintes et des nombres.

## Plan T1–T5 et règles de verdict
`bench/tools/p4_flasher_read_t1_t5.py` fixe le plan (22 étapes, 2,18 Mio lus) et les règles de verdict hors ligne (`evaluate`), écrites avant le run et non modifiables pendant. Trois niveaux de preuve sont distingués : `md5_host` et `data_sha256` viennent des mêmes octets reçus (non indépendants entre eux) ; le MD5 final de `READ_FLASH` est calculé par le stub sur les octets qu'il envoie (détecte la corruption de transport) ; `SPI_FLASH_MD5` relit la NOR (détecte une lecture instable, mais partage la routine de lecture du stub). Seule une référence externe (sauvegarde privée) peut révéler un décalage ou un repli d'adresse à 16 Mio ; sans elle, T3 reste `INDETERMINATE`.

**Débit.** `throughput_kib_s` = octets utiles / 1024 / s, chronométré de la réponse `READ_FLASH` au dernier acquittement cumulatif (trame MD5 finale exclue) ; il dépend du contenu (SLIP échappe `0xC0` et `0xDB`). Les 45,4 Kio/s historiques sont un repère, pas une base : aucun gain n'est annoncé sans mesure de l'ancien stub sur la même plage avec le même chronométrage. T5 compare des SHA-256 (les octets bruts ne sont pas conservés).
**Garde d'autorisation.** `check()` (réservation, grant, échéance) est évalué avant l'attente de chaque paquet **et** immédiatement avant son acquittement, car l'ACK autorise le stub à lire et émettre le suivant. **T3** exige les cinq empreintes de référence ; une table incomplète donne `INDETERMINATE`.
