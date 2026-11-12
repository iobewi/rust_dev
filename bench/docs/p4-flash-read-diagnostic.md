# Diagnostic de la deuxième lecture ESP32-P4 (G1, PC-1)

**Statut : analyse statique. Aucun accès matériel n'a été fait pour ce document.** Ce qui suit distingue ce qui est démontré, ce qui est fortement étayé et ce qui reste inconnu.

## Les faits du run `p4-read-v1` (observations, voir le bilan privé)
- `PC-0` : `identify` réussi, `esp32p4`, flash 33 554 432 octets, `flash_id` 0x1940C8.
- `PC-1` : première `flash.read` de 4096 octets à `0x2000` réussie (`return_console=false`, premier octet `0xE9`). Deuxième `flash.read` identique, dans la même session de loader : échec `hardware_command_failed`.
- Diagnostic complet de l'échec, relu sur le serveur : `phase=identify`, `error_type=Error`, `elapsed_ms=43`, `serial_reads=11`, `serial_bytes=132`, `serial_timeouts=0`, `last_read_ms=4`, `last_packet_bytes=12`.
- La carte est restée saine : même génération, pas de quarantaine, aucune opération pendante.

Ces observations ont été obtenues avec un consentement `PC-READ` inscrit par l'assistant et non donné par l'opérateur : elles restent des observations, pas une qualification autorisée.

## Faits démontrés par le code
1. **Un seul chemin produit `Unknown flash capacity`** : `web/esp-engine.js`, méthode `identity()`. Aucun autre fichier du dépôt ne contient ce texte.
2. **`identity()` est appelée avant chaque commande de flash** (`execute`, phase `identify`) : première commande comme suivantes. Le loader n'est rouvert que s'il n'existe pas (`openLoader` renvoie `this.loader` s'il est présent), donc la deuxième commande relit l'identifiant sur le loader et le stub déjà actifs.
3. **L'erreur ressortit en `hardware_command_failed`** parce que l'exception d'origine est une `Error` JavaScript simple, sans code ni type de transport : `hardwareError()` retombe alors sur ce code. Une erreur d'esptool-js s'appellerait `ESPError` (la classe fixe son nom ; l'agent conserve ce nom dans `error_type`). Le nom `Error` observé écarte donc une erreur levée par esptool-js.
4. **Dans `identity()`, la seule `Error` simple qui peut survenir une fois les réponses reçues est `Unknown flash capacity`** : elle suit la lecture de l'identifiant. La lecture de la MAC (`readMac`) vient après ce test.
5. **Le nombre d'échanges correspond à `readFlashId`.** Dans esptool-js 0.7.0, `readFlashId()` appelle `runSpiflashCommand(0x9F, [], 24)`. En comptant les lectures et écritures de registres de cette fonction, elle fait `9 + k` échanges, `k` étant le nombre de sondages de fin de commande. Avec `k = 2`, cela donne 11, soit `serial_reads = 11`. Les 132 octets valent 11 paquets de 12 octets. Aucun timeout : toutes les réponses sont arrivées.
6. **La valeur de capacité est `(id >> 16) & 0xFF`**, acceptée entre 12 et 26 inclus (2^12 = 4 Kio à 2^26 = 64 Mio). Une première lecture à 0x1940C8 donne 0x19 = 25 = 32 Mio.

## Hypothèse fortement étayée (non prouvée)
La deuxième lecture de l'identifiant JEDEC a renvoyé une valeur dont l'octet de capacité n'est pas dans l'intervalle 12 à 26, ce qui lève `Unknown flash capacity`. Les points 3 à 5 convergent. Aucune trace conservée ne contient la valeur brute : **elle n'a jamais été relevée**.

## Ce qui est inconnu
- La **valeur JEDEC brute** de la deuxième identification (0, tout à un, une autre valeur).
- La **cause** pour laquelle le P4 la renvoie dans cette situation. Pistes, aucune démontrée :
  1. Après une `read flash` par le stub, l'état du contrôleur SPI1 que `runSpiflashCommand` configure (registres USR, USR1, USR2, MISO_DLEN, W0 ; base `0x5008D000` pour le P4 dans esptool-js 0.7.0) n'est pas celui attendu pour une nouvelle commande 0x9F.
  2. Un reste de la lecture précédente dans le flux série. Cette piste est **affaiblie** par le décompte d'octets : les 132 octets correspondent aux 11 échanges de `readFlashId` seuls.
  3. Un effet propre au P4 ou à la révision de la puce avec ce stub.
- Si ce comportement existe aussi sur le C6 : non testé, hors de ce mandat.
- Pourquoi les recettes S3/C3 enchaînent des lectures sans problème : non établi.

## Différences entre la première et la deuxième commande
| | 1re commande | 2e commande |
|---|---|---|
| Loader | ouvert par `identity()` (`loader.main()` : connexion, stub, changement de débit à 460 800) | déjà ouvert, stub actif |
| État précédent | port juste ouvert | une commande de lecture du stub vient de se terminer |
| `identity()` | première lecture de l'identifiant | deuxième lecture, sur le même loader |

## Instrumentation préparée (non déployée)
Branche `feat/p4-backup-and-jedec-diagnostics`, fichiers `web/esp-engine.js` et `web/hardware-errors.js` :
- Une identification invalide lève `flash_capacity_unknown` au lieu de `hardware_command_failed`, avec trois nombres dans le diagnostic : `jedec_id` (valeur brute de 24 bits lue), `identify_index` (rang de cette identification dans la session de loader) et `loader_stub` (1 si le stub tourne).
- **Aucune vérification n'est supprimée, aucune identité n'est mise en cache, aucun retry n'est ajouté.** Le test de capacité est inchangé ; seul son échec porte plus d'information. L'identité physique reste relue avant chaque commande.
- Les champs passent par la liste blanche de diagnostics (valeurs numériques uniquement, aucun texte libre). Le serveur conserve l'objet d'erreur tel que l'agent l'envoie : aucune modification serveur.
- 8 tests Node (`tests/jedec-diagnostics.test.mjs`) : une 2ᵉ identification invalide rapporte la valeur brute et son rang, sans second `readFlashId` ni lecture de flash ; une capacité valide mais différente reste `physical_identity_changed` ; bornes 11, 12, 26, 27, 255 ; valeur 0xFFFFFFFF ; liste blanche.
- **Déploiement** : ce sont des fichiers de la page servie. Ils n'ont pas été déployés sur le banc actif. Les servir demande d'abord que la branche soit relue et qu'une validation soit donnée ; recharger la page déconnecte les agents.

## Prochain essai matériel recommandé (non autorisé, à décider par l'opérateur)
Préconditions : instrumentation relue et déployée par décision explicite ; page rechargée et port du P4 reconnecté ; consentement écrit **par l'opérateur** pour un scénario nommé, avec ses effets (entrée en loader, redémarrage du P4).

1. `identify` (identité de référence, `flash_id` connu).
2. `flash.read` de 4096 octets à `0x2000` avec `return_console=false` : doit réussir comme au premier essai.
3. Immédiatement, la même lecture : si elle échoue, le diagnostic contient désormais `jedec_id`, `identify_index=2` et `loader_stub`.
4. Lecture seule, aucune écriture, aucun reset par commande, aucun retry.

Ensuite, selon la valeur relevée :
- **Valeur nulle ou tout à un** : état du contrôleur SPI ; examiner une remise en état avant la lecture d'identifiant (par exemple relancer l'attache SPI du loader) **à condition de ne rien masquer** : l'échec doit rester visible si la valeur reste invalide.
- **Valeur plausible mais différente** : le test de capacité est trop strict, à discuter.
- **Aucune des deux** : investiguer le flux série.

### Stratégies de correction possibles (aucune appliquée)
- **A.** Mesurer l'identité physique une fois par session de loader puis la réutiliser. Affaiblit la garantie « identité relue avant chaque commande » : à valider explicitement.
- **B.** Rouvrir le loader si l'identifiant est invalide. Revient à un retry automatique : écarté.
- **C.** Contournement de recette : `return_console=true` pour chaque lecture, donc une session de loader neuve par commande (valeur par défaut de `backup.return_console` dans PC-BACKUP). **Non vérifié sur matériel.**
- **D.** Corriger la cause (état SPI) si la mesure la confirme, sans toucher aux garanties d'identité.

La correction ne doit pas être choisie avant la mesure de `jedec_id`.

## Résultat de l'essai P4-JEDEC-READ (2026-11-12, commit déployé `5bde67b7b`, autorisé par l'opérateur)
Observations, preuves privées dans `$BENCH_STATE_DIR/qualification-evidence/PC-6-20261112T180914Z`. **Verdict de la recette : FAIL** (les deux lectures diffèrent).

- Les deux lectures de 4096 octets à `0x2000`, chacune avec retour console, **ont réussi** : plus d'échec en phase `identify` et aucun `flash_capacity_unknown`. L'essai ne teste pas le mode enchaîné (`return_console=false`).
- Le JEDEC brut rapporté pour l'identification de chaque commande est **`0x1940C8`** (1 654 984), identique pour `identify` et pour les deux lectures, `identify_index=1`, `loader_stub=1`. Octet de capacité `0x19` = 32 Mio. (Une version précédente de ce document donnait par erreur `0x193040` ; seul l'octet de capacité était juste.)
- **Les deux lectures ont des SHA-256 différents** : 21 octets diffèrent sur 4096 (25 bits). La lecture du premier essai matériel, au même endroit, diffère aussi des deux autres (15 et 19 octets) : **trois lectures, trois empreintes**.
- Structure des différences : presque toutes sont d'un seul bit (18 octets sur 21 entre les deux lectures de l'essai) ; sur les 25 bits, 24 passent de 1 à 0 et 1 de 0 à 1 ; les décalages sont réguliers (0xb à 0x11, puis 0xa3, 0xb3, 0xc3, 0xd3 espacés de 0x10, puis 0x102, 0x122, 0x142, 0x162, 0x1e2, 0x242, puis 0x601, 0x641).
- Aucune lecture n'est toute à `0xFF`.

### Ce que cela établit, et ce que cela n'établit pas
- **Établi :** la lecture de flash du P4 par le banc n'est **pas déterministe** à cet endroit, avec ce chemin (esptool-js 0.7.0, stub, 460 800 bauds, CH343). Une sauvegarde complète faite avec ce chemin n'est donc pas fiable en l'état : la vérification actuelle de PC-BACKUP compare la copie locale à l'artefact du serveur, **pas** les octets lus à ceux de la flash.
- **Non établi :** la cause. Pistes, aucune démontrée : (1) corruption sur la liaison série (esptool-js 0.7.0 ne vérifie pas le condensat MD5 renvoyé par le stub après la lecture) ; (2) réglage du mode ou de la fréquence de lecture de la flash par le stub pour le P4 ; (3) alimentation ou intégrité du signal de la flash ; (4) contenu réellement variable (peu plausible dans une zone d'amorce).
- Le biais vers des bits passant de 1 à 0 et la régularité des décalages ne ressemblent pas à un bruit série uniforme ; c'est un indice, pas une conclusion.
- **Non rattaché à la panne précédente :** l'échec en phase `identify` de la deuxième lecture ne s'est pas reproduit avec une session de loader neuve par commande. Cela ne prouve pas que le mode enchaîné soit réparé.

### Diagnostic suivant à envisager (nécessite une nouvelle autorisation et un nouveau code relu)
Comparer, pour la même plage, le **MD5 calculé par la puce** (commande `flash md5sum` du loader, qui lit la flash en interne, sans passer par la liaison série) aux MD5 des octets reçus. Si le MD5 de la puce est stable d'une fois à l'autre et ne correspond pas aux octets reçus, la corruption est sur la liaison ; s'il varie lui aussi, elle est du côté de la lecture de la flash.
