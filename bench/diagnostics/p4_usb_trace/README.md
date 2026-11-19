# IDLab — Instrumentation USB OTG P4 (prototype)

## Statut
Prototype source uniquement. Aucune image RAM n'a été produite ou chargée, aucun essai matériel exécuté. La PR #67 et le lecteur standard ne sont pas modifiés.

## Base figée
- espressif/esp-flasher-stub : tag v1.2.2
- sous-module esp-stub-lib : d61983fa4d1f66b9c088608c1f702ad877121279
- cible : ESP32-P4 (RISC-V), firmware de diagnostic **distinct**, lancé seulement après revue et consentement explicite.

## Préparation hors banc
Dans un clone neuf du dépôt amont au tag v1.2.2, initialiser tous les sous-modules et vérifier l'identifiant exact du sous-module. Ensuite :

    python3 /chemin/rust_dev/bench/diagnostics/p4_usb_trace/apply_trace.py /chemin/esp-flasher-stub
    cmake -S /chemin/esp-flasher-stub -B build-p4 -G Ninja -DTARGET_CHIP=esp32p4
    cmake --build build-p4

La compilation exige l'outillage RISC-V et les scripts du projet amont. Le patch échoue si ses points d'insertion ne sont pas uniques. Construire et vérifier le JSON/ELF, les adresses RAM et les limites de taille avant d'envisager un chargement.

## Protocole
- Opcode diagnostique 0xDF : mêmes données et ACK cumulatifs que READ_FLASH, une fenêtre de 1 paquet, trame MD5 finale.
- Opcode 0xE0 : réponse standard contenant les compteurs au format IDT1 (64 octets). Ne jamais injecter des compteurs au milieu du flux flash.
- Le client bench/web/p4-stub-trace.js n'est pas intégré à l'interface, exige une marque de stub explicite et vérifie une réponse IDT1 avant toute lecture.

Format little-endian de 64 octets :
- 0: u32 magic « IDT1 » ; 4: u32 version 1.
- 8: u64 flash_cycles ; 16: u64 md5_cycles.
- 24: u64 send_cycles ; 32: u64 ack_cycles.
- 40: u64 fill_cycles.
- 48: u32 fill_calls ; 52: u32 packets ; 56: u32 ack_count ; 60: u32 bytes.

Ce sont des **cycles CPU**, pas des microsecondes. La fréquence CPU devra être mesurée/confirmée. L'instrumentation accumule des deltas 32 bits en compteurs 64 bits ; un intervalle individuel plus long qu'un tour complet du compteur n'est pas couvert.

Les compteurs fill_cycles sont un sous-ensemble de send_cycles : **ne pas les additionner**. Les cycles d'attente ACK couvrent le retour USB et la synchronisation avec l'hôte, après le retour de send_frame(). Les cycles send incluent l'encodage SLIP et la transmission synchrone.

## Garde-fous
- Aucun remplacement transparent du stub fourni par esptool-js 0.7.0.
- Aucune écriture/effacement NOR dans cette variante ; récupération par réinitialisation ROM si nécessaire.
- La valeur int retournée par la routine ROM CDC n'est pas interprétée : sémantique à vérifier avant d'en tirer un diagnostic d'erreur.
- Qualification préalable obligatoire : compilation propre P4, analyse des sections/adresses, tests, exactitude du protocole et comparaison SHA-256 avec le stub standard.
- En cas de problème, arrêt et conservation des preuves, pas de reconnexion/rejeu automatique.

## Décision après mesure
- fill_cycles important : examiner esp_rom_cdc_acm_fifo_fill() / gestion FIFO ROM.
- send_cycles important mais fill_cycles faible : examiner SLIP et émissions octet par octet.
- ack_cycles important : examiner le chemin retour USB et la synchronisation hôte.

Les données actuelles P4 (23,1 s en lecture complète, 2,175 s en MD5 seule) justifient ce découpage mais ne localisent pas à elles seules les 20,9 secondes.
