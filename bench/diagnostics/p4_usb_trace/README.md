# IDLab — Instrumentation USB OTG P4 (prototype qualifié statiquement)

## Statut
Image instrumentée **compilée**, **testée côté logiciel** et **qualifiée statiquement** ; **aucune validation matérielle** : aucune image RAM n'a été chargée, aucun essai matériel exécuté.
La PR #67 et le lecteur standard ne sont pas modifiés. Le détail des quatre niveaux (compilé / testé / qualifié / validé sur matériel) est dans [`QUALIFICATION.md`](QUALIFICATION.md) ;
la procédure sur matériel, **non exécutée**, est dans [`RECIPE.md`](RECIPE.md).

## Base figée
- espressif/esp-flasher-stub : tag v1.2.2 (commit `23959b780454adf885916d42f2274ec648e96a94`)
- sous-module esp-stub-lib : d61983fa4d1f66b9c088608c1f702ad877121279
- cible : ESP32-P4 (RISC-V), `-DTARGET_CHIP=esp32p4` pour une puce en révision ≥ 3.00 (ROM « ECO5 et suivantes »), firmware de diagnostic **distinct**, lancé seulement après revue et consentement explicite.
- chaîne d'outils : `riscv32-esp-elf` GCC 15.1.0 (crosstool-NG `esp-15.1.0_20250607`), CMake ≥ 3.28, Ninja, pyelftools.
  Le stub standard compilé avec cette chaîne est identique au `esp32p4.json` d'esptool-js 0.7.0.

## Préparation hors banc
Dans un clone neuf du dépôt amont au tag v1.2.2, avec son sous-module :

    python3 bench/diagnostics/p4_usb_trace/build_stub.py \
        --upstream /chemin/esp-flasher-stub \
        --toolchain-bin /chemin/riscv32-esp-elf/bin \
        --out /chemin/NOUVEAU/dossier-de-preuves

Le script refuse de démarrer si le tag, le sous-module, les arbres propres, la version du compilateur, CMake ou pyelftools ne sont pas conformes ; il ne réutilise jamais un dossier existant.
Il produit les deux images (standard et instrumentée), leurs ELF et JSON, les journaux de build, `build-manifest.json` et `static-qualification.json`.
Les binaires restent dans ce dossier de preuves, **hors du dépôt** (seuls les SHA-256 sont consignés). Ne pas déclarer l'image qualifiée si `qualified_statically` n'est pas `true`.

`apply_trace.py` peut aussi être utilisé seul ; il vérifie le tag et le sous-module, écrit les deux fichiers ou aucun, et refuse une seconde application.

## Tests
- `node --test tests/p4-stub-trace.test.mjs tests/p4-stub-loader.test.mjs` (client de trace et chargeur RAM) ;
- `pytest python/tests/test_p4_trace_patch.py python/tests/test_p4_trace_tools.py` (générateur de patch, qualification statique, analyse) ;
- `IDLAB_ESP_FLASHER_STUB=/chemin/esp-flasher-stub pytest python/tests/test_p4_trace_host_harness.py` : banc natif qui compile les vrais fichiers C patchés avec une horloge simulée.
  Il valide la logique de l'instrumentation, pas la ROM, l'USB ni le compteur de cycles réel.

## Protocole
- Opcode diagnostique 0xDF : mêmes données et ACK cumulatifs que READ_FLASH, une fenêtre de 1 paquet (celle du stub amont), trame MD5 finale.
- Opcode 0xE0 : réponse standard contenant les compteurs au format IDT1 (64 octets). Ne jamais injecter des compteurs au milieu du flux flash.
  La sonde échoue fermée si le compteur de cycles n'avance pas.
- Les deux opcodes sont dans la plage plugin amont `0xD5`–`0xEF` : ils en masquent deux emplacements ; l'image ne doit jamais être combinée à des plugins.
- Le client `bench/web/p4-stub-trace.js` n'est pas intégré à l'interface, exige une marque de stub explicite et vérifie une réponse IDT1 avant toute lecture.
  Le chargeur `bench/web/p4-stub-loader.js` charge l'image en RAM seulement, avec consentement, SHA-256, puce, révision et session ROM vérifiés.

Format little-endian de 64 octets :
- 0: u32 magic « IDT1 » ; 4: u32 version 1.
- 8: u64 flash_cycles ; 16: u64 md5_cycles.
- 24: u64 send_cycles ; 32: u64 ack_cycles.
- 40: u64 fill_cycles.
- 48: u32 fill_calls ; 52: u32 packets ; 56: u32 ack_count ; 60: u32 bytes.

Ce sont des **cycles CPU**, pas des microsecondes. Le stub amont règle le CPU à 200 MHz, valeur **non mesurée** : elle est toujours fournie explicitement à la conversion et recoupée avec le temps mur.
L'instrumentation accumule des deltas 32 bits en compteurs 64 bits ; un intervalle individuel plus long qu'un tour complet du compteur (21,47 s à 200 MHz) n'est pas couvert.

Les compteurs fill_cycles sont un sous-ensemble de send_cycles : **ne pas les additionner**. Les cycles d'attente ACK couvrent le retour USB et la synchronisation avec l'hôte, après le retour de send_frame().
Les cycles send incluent l'encodage SLIP et la transmission synchrone.

## Garde-fous
- Aucun remplacement transparent du stub fourni par esptool-js 0.7.0.
- Aucune écriture/effacement NOR dans cette variante ; le chargeur n'utilise que les commandes RAM de la ROM ; récupération par réinitialisation ROM si nécessaire, avec l'accord de l'opérateur.
- La valeur int retournée par la routine ROM CDC n'est pas interprétée : sémantique à vérifier avant d'en tirer un diagnostic d'erreur.
- Qualification préalable obligatoire : compilation propre P4, analyse des sections/adresses, tests, exactitude du protocole et comparaison SHA-256 avec le stub standard.
- En cas de problème, arrêt et conservation des preuves, pas de reconnexion/rejeu automatique ; une erreur après le début du flux est `sessionIndeterminate`.

## Décision après mesure
Voir la table des verdicts de `RECIPE.md` : `fill` dominant → `esp_rom_cdc_acm_fifo_fill()` / FIFO ROM ; `send` dominant sans `fill` → SLIP et émission octet par octet ; attente d'ACK dominante → chemin retour USB et synchronisation hôte.
Les données actuelles P4 (23,1 s en lecture complète, 2,175 s en MD5 seule) justifient ce découpage mais ne localisent pas à elles seules les 20,9 secondes.
