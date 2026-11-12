# ESP32-P4 : initialisation flash du stub et différences entre sessions de chargeur

**Statut : analyse documentaire et préparation en SIMULATION. Aucune opération matérielle.** Elle fait suite au run PC-7 v1 (FAIL : trois MD5 de la puce distincts, deux lectures distinctes, MD5 du stub égal à celui des octets reçus pour chaque lecture). Les sources consultées sont publiques ; ce qui n'est pas vérifié est dit tel quel.

## 1. Quel stub tourne sur le P4 observé

- L'agent rapporte `ESP32-P4 (revision v3.2)` pour les cinq identifications du run. esptool-js 0.7.0 choisit `esp32p4-rev1.json` si la révision est < 300, sinon `esp32p4.json` : le P4 observé charge donc **`esp32p4.json`** (révision 302, silicium « ECO7 »).
- Ce fichier, extrait du paquet embarqué, a le même condensat que celui du commit esptool `4b64d421` (2026-08-19) « Update flasher stub to v1.2.2 » : le stub chargé est **esp-flasher-stub v1.2.2**. esptool-js 0.7.1 (2026-11-02) embarque le même fichier.
- Il contient donc : le support ECO7 (rév. 3.2, 2026-03-25), le correctif des erreurs au démarrage ECO7 (2026-03-31) et « skip ROM flash XPD when PMU latch is set » (v1.2.2). Il **ne contient pas** les stubs v1.3.0 (2026-09-21), v1.4.0, v1.4.1 (2026-10-14, mise à jour P4 ECO8) ni v1.5.0. Le contenu exact de ces mises à jour pour le P4 n'est pas établi ici ; rien ne prouve qu'elles concernent le défaut observé.

## 2. Ce que fait le stub au démarrage et pour lire

Source : esp-flasher-stub (master) et esp-stub-lib ; la version 1.2.2 n'a pas été relue ligne à ligne, les différences possibles avec master ne sont pas connues.

- À l'initialisation, `stub_lib_flash_init` rattache la flash (`stub_target_flash_attach`) puis lit l'identifiant JEDEC, en déduit la taille (`0x19` : 32 Mio) et configure la ROM (`esp_rom_spiflash_config_param`). Sur le P4, le rattachement dépend de l'ECO : en ECO7, si l'eFuse `DOWNLOAD_MODE_XPD_ON` est positionné et que `PMU_DATE[1:0]` impose déjà l'alimentation, le stub appelle `esp_rom_spiflash_boot_attach` ; sinon `esp_rom_spiflash_attach`, qui peut mettre la flash sous tension. Le commentaire du code dit que la séquence de mise sous tension ECO7 **ne peut pas être exécutée deux fois**.
- `READ_FLASH` et `SPI_FLASH_MD5` lisent la flash par la **même fonction** (`stub_lib_flash_read_buff`, qui appelle la ROM `esp_rom_spiflash_read`), par blocs de 4096 octets. Les deux commandes n'ont donc pas des chemins de lecture différents côté stub ; la ligne R3 de la grille (« chemins de lecture différents ») est peu plausible pour ce stub.
- Le stub règle aussi l'horloge (fichiers `clock.c`, « CPLL root mux select ») et positionne `SPI_MEM_USR_COMMAND` sur SPI1.

## 3. Ce qui change d'une session de chargeur à l'autre

Avec `return_console=true`, chaque commande : entre dans le chargeur ROM (séquence DTR/RTS, câblage inféré), charge le stub, lit l'identifiant JEDEC par des écritures directes dans les registres SPI1 (`runSpiflashCommand`, avec seulement 10 interrogations du registre de commande), exécute la commande, puis **rend la console** (le firmware redémarre). Entre deux mesures de PC-7 il y a donc : un redémarrage du firmware applicatif, une nouvelle entrée en mode téléchargement, un nouveau chargement du stub et une nouvelle initialisation de l'horloge et du contrôleur flash. Aucune des cinq mesures de PC-7 n'a été faite dans un contexte matériel continu : **on ne sait pas si l'instabilité existe à l'intérieur d'une session.**

Constat utile : la plage `0x2000` est la zone du bootloader, relue à chaque démarrage et qui amorce correctement. Les écarts de PC-7 (28 octets, 44 bits, 22 vers 0 et 22 vers 1) ne sont donc pas visibles dans le résultat du boot, ce qui n'exclut ni une sensibilité de la lecture du stub (fréquence, horloge, séquence) ni une instabilité propre à ce contexte. C'est un constat, pas une cause.

## 4. Hypothèses, par ordre d'examen

| Hypothèse | Ce qui la soutient | Ce qui la contredit ou manque |
|---|---|---|
| Initialisation flash/horloge différente d'une session à l'autre | Le code montre une séquence d'attache dépendante de l'ECO et non rejouable ; chaque mesure de PC-7 est dans une session neuve | Aucune mesure à l'intérieur d'une session |
| Stub 1.2.2 antérieur à des correctifs P4 ultérieurs (1.3.0 à 1.4.1) | Des stubs plus récents existent | Contenu exact des correctifs non établi |
| Lecture instable par le chemin ROM (signal, fréquence, mode) | Variation d'un bit à la fois, dans les deux sens, y compris pour `SPI_FLASH_MD5` (même fonction de lecture) | Rien ne distingue flash, contrôleur et horloge |
| Corruption de la liaison série entre le stub et le PC | Aucune : le MD5 du stub égale celui des octets reçus | Observé deux fois seulement |
| Défaillance du banc | Aucun indice : six opérations correctes, aucun timeout | |

## 5. Variante préparée : `P4-FLASH-INTEGRITY-SESSION` (PC-8), en simulation seulement

Mêmes adresse et taille que PC-7 ; seule la session change. Séquence : `flash.md5`, `flash.read` (avec `stub_digest`), `flash.md5`, tous avec `return_console=false`, puis **un** retour console explicite. Il n'y a pas de `identify` séparé : `identify` n'accepte aucun argument et rend toujours la console, il ne peut pas ouvrir une session qui reste.

- **Le contrôle d'identité reste strict** : l'agent relit l'identifiant JEDEC à chaque commande, y compris dans la session (ni cache, ni assouplissement, ni retry). Au premier run matériel, une deuxième commande dans la même session a échoué en phase `identify`. L'agent rapporte désormais la valeur brute (`jedec_id`) et le rang : si l'échec se reproduit, c'est la mesure, elle est conservée telle quelle et la séquence s'arrête. Le défaut n'est pas « corrigé » dans le code ; sa cause n'est pas établie.
- **Preuve de continuité** exigée : rangs d'identification 1, 2, 3 et aucun événement « console opened » avant le retour explicite ; sinon FAIL, même si les MD5 concordent.
- Verdicts et limites : ceux de PC-7 ; un PASS dit seulement que cette observation, dans une session, était cohérente, et n'efface pas la variation vue entre sessions. Il n'autorise ni PC-BACKUP ni une autre plage.
- Simulation : `python/tests/test_pc_integrity_session.py` (continuité, identification invalide à la deuxième commande, session non continue, octets reçus corrompus, condensat final absent/partiel/invalide, consentement distinct de PC-7, cibles et assets).

## 6. Lecture croisée des deux scénarios (à interpréter, pas à présumer)

| PC-7 (sessions séparées) | PC-8 (une session) | Ce que cela suggère, à confirmer |
|---|---|---|
| variable | cohérent | la variation apparaît entre initialisations plutôt qu'à l'intérieur d'une session |
| variable | variable | la variation existe aussi dans une session : lecture ROM, flash ou signal plutôt qu'initialisation |
| variable | échec d'identification en 2e commande | le défaut d'identification est lui-même à instruire (valeur brute) avant de conclure |

Une seule observation de chaque ne suffit pas : prévoir plusieurs répétitions, décidées par l'opérateur.

## 7. Ouvert

Contenu exact du stub 1.2.2 face à master et aux versions 1.3.0 à 1.4.1 ; effet réel de la séquence d'attache ECO7 ; fréquence de lecture effective du stub ; cause de l'échec d'identification observé au premier run. Aucun essai matériel (PC-8 ou autre) sans revue du SHA, déploiement contrôlé et consentement explicite de l'opérateur.
