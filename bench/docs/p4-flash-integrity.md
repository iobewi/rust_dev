# Intégrité de lecture flash ESP32-P4 : diagnostic MD5

**Statut : conçu et qualifié en SIMULATION uniquement. NON exécuté sur matériel.** Aucune lecture, identification, réinitialisation, écriture ni effacement n'a été faite pour ce lot. Une PR ouverte ou une CI verte ne valent pas autorisation d'exécution ; un essai matériel exige la revue du SHA, un déploiement contrôlé et un message de consentement explicite de l'opérateur (carte, scénario, commit, effets).

## 1. Constat à expliquer

Trois lectures de la même plage (4096 octets à `0x2000`) ont donné trois condensats SHA-256 différents (run `p4-read-v1`, puis deux lectures de `p4-jedec-read-v1`). L'identifiant JEDEC brut est resté `0x1940C8`. Aucune cause n'est établie : ni la flash, ni le stub, ni la liaison série, ni le traitement côté hôte.

Comparaison hors ligne des trois fichiers (outil `bench/tools/bitdiff.py`, aucun contenu affiché) :

| Paire | Octets | Bits | vers 0 / vers 1 | Octets à 1 bit |
|---|---|---|---|---|
| run 1 lecture 1 → run 2 lecture 1 | 19 | 22 | 8 / 14 | 17 |
| run 1 lecture 1 → run 2 lecture 2 | 15 | 21 | 19 / 2 | 11 |
| run 2 lecture 1 → run 2 lecture 2 | 21 | 25 | 24 / 1 | 18 |

Constats bruts : 25 positions distinctes au total, dont 5 communes aux trois paires ; la plupart des écarts sont à 1 bit ; le sens (vers 0 ou vers 1) n'est pas constant d'une paire à l'autre ; plusieurs écarts sont espacés de multiples de 16 (par exemple `0xa3, 0xb3, 0xc3, 0xd3`). **Ces régularités sont des observations, pas des causes.** La corrélation avec une taille de paquet est impossible : 4096 octets tiennent dans un seul paquet.

## 2. Ce que font les outils actuels

- esptool-js 0.7.0 `readFlash` (stub, opcode `0xD2`) lit les paquets, acquitte cumulativement et s'arrête à la longueur demandée. Le stub envoie ensuite une trame finale de 16 octets : le MD5 des octets qu'il a lus en flash, calculé avant l'envoi de chaque paquet. **esptool-js ne la lit jamais** : la commande suivante vide l'entrée (`flushInput`), la trame est jetée sans être vérifiée.
- `flashMd5sum` (`ESP_SPI_FLASH_MD5`, `0x13`) existe : le stub lit la plage par blocs de 4096 octets et répond 16 octets bruts (la ROM : 32 caractères ASCII).
- Le stub embarqué pour le P4 est de la génération récente (rév1 si la révision de puce est < 300). L'analyse du code source `espressif/esp-flasher-stub` (branche master, pas exactement la version embarquée) confirme ce comportement ; elle ne remplace pas une mesure.

## 3. Solution retenue

Deux mesures supplémentaires, indépendantes, et une analyse pure.

| Élément | Rôle |
|---|---|
| `flash.md5` (nouvelle commande, lecture seule, non privilégiée, sans grant) | MD5 de la plage **calculé par la puce**. Format de réponse vérifié (32 hex stub, 32 ASCII ROM) ; jamais de MD5 vide ou par défaut : sinon `md5_response_invalid` |
| `flash.read` avec `stub_digest:true` (booléen strict, défaut faux) | Lit, après chaque bloc de 1 Mio, la trame finale du stub, sans nouvelle commande. États observés : `received`, `absent` (délai 3 s), `partial`, `invalid`. Un port fermé reste une déconnexion. Sans l'option : comportement strictement inchangé |
| `recipes/qualification_campaign/integrity.py` | Analyse pure : applique la grille ci-dessous, sépare observations et hypothèses, ne nomme jamais de cause |
| `bench/tools/bitdiff.py` | Comparaison hors ligne de plusieurs lectures (positions, masques, sens, espacements, alignements) |

`flash.md5` n'est volontairement **pas** ajoutée aux commandes permises en quarantaine ni en maintien : un état incertain ne doit pas être sondé par une commande nouvelle.

## 4. Séquence de la recette `P4-FLASH-INTEGRITY` (PC-7)

`identify`, `flash.md5`, `flash.read(stub_digest)`, `flash.md5`, `flash.read(stub_digest)`, `flash.md5`, tous avec `return_console:true` (chaque commande ouvre une session de chargeur neuve), plage `0x2000` / 4096 octets, P4 / 32 Mio uniquement.

Prérequis vérifiés **avant toute commande** : consentement `P4-FLASH-INTEGRITY` dans la session privée écrite par l'opérateur ; condensat des assets de l'agent égal à `integrity.expected_assets_sha256` ; capacité `flash.md5` annoncée ; cible conforme. Sinon BLOCKED.

Preuves conservées immédiatement : fichiers `read-N-00002000.bin`, un JSON par opération (MD5 et SHA-256 côté client, état du condensat du stub, diagnostics JEDEC), le diagnostic brut d'un échec, `integrity-analysis.json`. Arrêt à la première opération en échec ; aucun rejeu ; une issue `indeterminate` reste telle quelle.

Verdicts : PASS seulement si les cinq mesures sont complètes, valides et concordantes (ligne R4). Une valeur manquante, partielle ou invalide est un FAIL étiqueté « incomplet », pas un succès. Un PASS ne dit que l'intégrité **de cette observation**.

## 5. Grille d'interprétation

| Ligne | Observation | Lecture (hypothèse) |
|---|---|---|
| R1 | MD5 du stub stable, MD5 des octets reçus variable | corruption du transfert ou du traitement hôte fortement suspectée |
| R2 | MD5 du stub variable | instabilité en amont du transfert, ou dans le calcul du stub, possible |
| R3 | MD5 de la puce stable, `READ_FLASH` variable | chemins de lecture différents, ou corruption pendant le transfert |
| R4 | tout concorde | intégrité de cette observation établie, rien de plus |
| R5 | un MD5 manque ou a échoué | diagnostic incomplet, pas de conclusion |

Limite de fond : les MD5 du stub et de la puce transitent par la **même liaison série** ; deux commandes peuvent lire la flash par des chemins différents. Une divergence isolée ne prouve donc jamais une panne UART ni une panne flash. Ne conclure à rien à partir d'un seul essai.

## 6. Contrôles logiciels (simulation)

- Node : `tests/flash-integrity.test.mjs` (états de la trame finale, `flash.md5` stub/ROM/invalide/erreur/bornes/identité stricte, `flash.read` avec et sans `stub_digest`, plusieurs blocs, absence de nouvelle tentative) ; `tests/jedec-diagnostics.test.mjs` mis à jour (`chip_description`).
- Python : `python/tests/test_pc_integrity.py` (session, grille, recette contre le simulateur : concordance, corruption du transfert, flash variable, MD5 puce discordant, trame finale absente/partielle/invalide, réponse MD5 invalide, échec de lecture, consentement, cible, assets, validation de protocole, bitdiff).
- Ils ne prouvent rien sur la vraie puce, le stub ou Web Serial.

## 7. Déploiement et limites

Le changement de protocole modifie l'empreinte du code serveur : le déployer exige un SHA relu, un redémarrage de l'identifiant du lanceur et un rechargement de la page, avec l'accord de l'opérateur. Rien n'a été déployé ; `/workspace` reste sur le commit déployé.

Questions ouvertes : le stub embarqué exact n'a pas été comparé octet à octet à la source consultée ; le délai de 3 s de la trame finale n'est pas mesuré ; l'ordre des commandes peut influencer la mesure (d'où la répétition) ; les écarts observés ne sont pas expliqués. La sauvegarde complète du P4 (PC-BACKUP) reste suspendue tant que l'intégrité de lecture n'est pas établie.
