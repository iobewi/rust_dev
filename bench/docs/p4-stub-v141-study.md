# ESP32-P4 ECO7 : étude du stub esp-flasher-stub v1.4.1 comme référence de lecture

**Statut : étude documentaire. Rien n'a été développé, déployé ni exécuté ; aucune commande matérielle ; esptool n'est pas installé dans le devcontainer.** Sources : esptool `v5.6.0`, esp-flasher-stub tags `v1.2.2`, `v1.3.0`, `v1.4.0`, `v1.4.1`, et la bibliothèque esp-stub-lib aux SHA épinglés par ces tags (`d61983fa…` pour v1.2.2, `26e5798f…` pour v1.4.1). Ce qui est lu dans le code n'est pas observé sur la carte.

## 1. Ce que contient exactement le stub v1.4.1 de plus que le v1.2.2 du banc

esptool 5.6.0 embarque le stub P4 `esp32p4.json` dont le condensat est celui de la mise à jour « v1.4.1 » (2026-10-14). Le banc (esptool-js 0.7.0 et 0.7.1, qui n'a pas changé) charge le v1.2.2 (2026-08-19). Sur les 36 commits de la bibliothèque entre les deux, **seuls ceux-ci touchent le P4** :

| Changement | Concerne notre cas ? |
|---|---|
| Support ECO8 (`esp_rom_opiflash_exec_cmd_eco8`, branche `else` après `== 7`) | Non : le P4 observé est ECO7 ; la branche ECO7 appelle la même fonction qu'avant |
| Attache flash : `_rom_eco_version >= 7` devient `== 7` | Non pour ECO7 (comportement identique) |
| Suspension du cache L2 « autoload seulement » (`cache.c`) | **Incertain.** Le message du commit décrit un « Cache error » parasite quand l'application reprend ses accès après la sortie du stub ; il ne parle pas de données de lecture corrompues. Lien avec nos MD5 variables : **non établi** |
| Délais d'effacement et d'écriture 4 octets augmentés | Non : écriture et effacement uniquement |
| Interface SPI esclave, fichiers de registres | Non |

**Le chemin de lecture ECO7 et la séquence d'attache ECO7 sont inchangés entre v1.2.2 et v1.4.1.** Il n'y a donc pas d'argument tiré du code pour attendre qu'un v1.4.1 corrige l'instabilité observée. Ce n'est pas non plus une raison de l'exclure : c'est une expérience peu coûteuse si le moyen de l'exécuter est conforme (section 4), dont le résultat, quel qu'il soit, est informatif.

## 2. Séquence d'initialisation ECO7 et gestion d'erreurs du stub (lecture du code)

Initialisation (`stub_lib_flash_init_ex`, même logique en v1.2.2 et v1.4.1 pour ECO7) :
1. Attache la flash. En ECO7, si l'eFuse `DOWNLOAD_MODE_XPD_ON` est positionné et que `PMU_DATE[1:0]` impose déjà l'alimentation, appel de `esp_rom_spiflash_boot_attach` ; sinon `esp_rom_spiflash_attach`. Le commentaire du code précise que la séquence d'alimentation ECO7 ne peut pas s'exécuter deux fois.
2. Lit l'identifiant JEDEC ; `0x19` donne 32 Mio. Un identifiant inconnu donne une taille nulle : le stub suppose alors la taille maximale de la puce (64 Mio pour le P4), journalise et renvoie `STUB_LIB_ERR_UNKNOWN_FLASH_ID`.
3. Au-delà de 16 Mio, active le mode « grande flash » (adressage 4 octets), puis configure la ROM (`esp_rom_spiflash_config_param`).
4. Positionne `SPI_MEM_USR_COMMAND` sur SPI1.

**Lecture pour une flash de plus de 16 Mio** (le cas des 32 Mio de cette carte, y compris pour l'adresse `0x2000`) : `stub_target_flash_4byte_read` lit par morceaux de 16 octets avec la commande `0x0C`, une adresse de 32 bits et 8 cycles factices, en mode FASTRD, par la fonction ROM `esp_rom_opiflash_exec_cmd_eco7`. **Cette fonction ne renvoie aucun code d'erreur** : une lecture SPI erronée est silencieuse. (Une première version de nos documents disait à tort que le stub appelait la ROM `esp_rom_spiflash_read`.)

Conséquence importante : **le MD5 final du stub ne protège que le transfert série, pas la lecture SPI.** Il est calculé sur les octets que le stub vient de lire, erronés ou non. C'est ce que PC-7 a montré : MD5 du stub égal au MD5 des octets reçus pour les deux lectures, alors que les lectures différaient entre elles. La vérification de ce MD5 par esptool Python (« Digest mismatch », `loader.py:1719-1727`) n'aurait donc pas détecté notre problème non plus.

Gestion d'erreurs côté hôte (esptool 5.6.0) : trames de données trop courtes (« Corrupt data »), trame de condensat absente ou de longueur différente de 16 (« Expected digest »), condensat différent (« Digest mismatch »). Rien de tout cela ne contrôle la fidélité de la lecture SPI.

Entrée ECO7 côté hôte : esptool 5.6.0 et esptool-js 0.7.0 exécutent le même `power_on_flash` (contournement du bogue ROM « deuxième entrée en mode téléchargement » quand `DOWNLOAD_MODE_XPD_ON` est programmé). **Le banc l'exécute donc déjà** ; l'état de cet eFuse sur la carte reste inconnu.

## 3. Ce qu'apporterait réellement une lecture de référence par esptool 5.6.0

| Apport | Valeur |
|---|---|
| Stub v1.4.1 au lieu du v1.2.2 | Expérience d'élimination « un stub plus récent change-t-il le résultat ? » ; aucun indice code qu'il le fera |
| Vérification du MD5 final par l'outil | Protège le transfert série seulement : déjà démontré intègre sur deux lectures par nos propres mesures |
| Implémentation officielle complète (attache ECO7, paramétrage de taille, reset) | Valeur de référence pour une comparaison indépendante du code du banc |
| Lectures répétées | Statistique supplémentaire ; une seule exécution ne tranche pas |

Les trois recommandations de l'avis reçu sont cohérentes avec ce constat, avec la nuance ci-dessus : le gain attendu sur la fiabilité est incertain, pas nul.

## 4. Comment l'utiliser sans contourner les garanties de Device Lab

| Voie | Garanties conservées | Coût et réserves |
|---|---|---|
| **1. Embarquer le stub v1.4.1 dans l'agent du navigateur** | Toutes (réservation, journal, preuves, propriétaire unique du port) | esptool-js n'offre **aucune option pour fournir un autre stub** : le JSON est choisi en interne, et aucune version publiée (0.7.1 incluse, 2026-11-02) n'embarque le v1.4.1. Il faudrait un esptool-js reconstruit ou patché (JSON `esp32p4.json` de 5.6.0, même format) et l'héberger dans `bench/web` au lieu du CDN actuel. C'est une modification du code servi : développement, revue du SHA, déploiement contrôlé, consentement. À ne pas lancer maintenant |
| **2. esptool 5.6.0 sur le PC de l'opérateur, port libéré** | Pas de mécanisme concurrent (le navigateur libère le port, le banc voit la carte déconnectée) ; les garanties de réservation et de journal ne couvrent pas cette phase | Déroge à `AGENTS.md` (esptool et accès direct aux ports interdits pour contourner le banc) : décision explicite de l'opérateur requise, preuves saisies à la main (fiche à préparer), carte à rendre au banc ensuite |
| **3. Ne pas l'utiliser** | — | Reste sans comparaison avec une implémentation officielle |

Le dépôt n'a aucun moyen d'obtenir le v1.4.1 dans le banc sans la voie 1.

## 5. Ce que cette étude change dans la stratégie

- Elle **ne confirme pas** l'hypothèse « le v1.2.2 est la cause » : le chemin de lecture ECO7 est identique en v1.4.1.
- Elle déplace la suspicion vers **le chemin de lecture 4 octets du stub** (commande `0x0C`, fonction ROM ECO7, fréquence et dummy bits, état du contrôleur SPI1), que nous ne contrôlons pas depuis le banc, et vers l'état matériel (alimentation, horloge) à l'entrée dans chaque session.
- Elle indique qu'une comparaison avec une lecture qui **n'emprunte pas** ce chemin est la mesure la plus informative : par exemple le MD5 ROM d'une plage sous 16 Mio (le ROM lit par sa propre routine), ou la lecture lente par commandes utilisateur SPI1 (mécanisme suspect lui aussi, voir l'étude du chemin ROM).
- Elle rappelle que l'objectif reste une **lecture fiable des 32 Mio** pour installer le bootloader IOBEWI OTA avec un moyen de récupération : tant que la lecture d'une plage de 4 Kio varie, une sauvegarde de 32 Mio ne serait pas fiable et PC-BACKUP reste suspendu.

## 6. Risques et limites

- Tout vient de la lecture du code (tags publics) ; la version exacte du ROM ECO7 de la carte n'est pas observée (le stub dispatche sur `_rom_eco_version`).
- Le v1.4.1 n'a pas été comparé octet à octet au v1.2.2 hors des commits de la bibliothèque ; le stub est recompilé à chaque mise à jour.
- Le lien entre la suspension du cache L2 et nos mesures n'est pas établi.
- Utiliser esptool Python sur ce port suppose que le navigateur l'ait libéré ; ne jamais lancer en concurrence. Aucune écriture, aucun effacement, aucune modification d'eFuse dans cette étude.
- Les entrées répétées en mode téléchargement sont précisément la situation du bogue ECO7 d'esptool : une deuxième connexion peut échouer sans rejeu automatique.
