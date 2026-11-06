# Issue #2, phase 2 : contrats des recettes ciblées (R1, R4) et procédure de récupération (R5/R6)

**Statut : contrats et premier code, pour revue. Rien n'a été exécuté sur matériel ; aucun accès matériel, aucun grant, aucune écriture flash, aucun redémarrage serveur.**
Audit de référence : [issue-2-coverage-audit.md](issue-2-coverage-audit.md). Les critères ci-dessous sont écrits **avant** tout run ; une correction crée une nouvelle version,
une nouvelle revue et un nouveau dossier de preuves. Un résultat inconnu reste INDETERMINATE. Le retour de la bannière ROM n'est **pas** un démarrage applicatif
(`boot_verified` vaut toujours `false`).

## 1. R1, exigence 1 : S3 + pont UART externe

Trois recettes séparées (un effet matériel par recette, chacune avec son consentement) :

| Id | Effet | État |
|---|---|---|
| **S3R-1** | un reset EN (RTS), bannière ROM `ESP-ROM:esp32s3` dans la même fenêtre de preuve | **code écrit**, testé en simulation (`test_s3_uart_reset.py`) |
| **S3R-2** | maintien BOOT (IO0 par DTR) et ROM en mode téléchargement | **conception ouverte**, voir ci-dessous |
| **S3R-3** | retour console après une opération du chargeur (lecture) | contrat ci-dessous, code non écrit |

### S3R-1 (code)
Prérequis : session privée avec chip `esp32s3`, transport déclaré `external_uart`, consentement `S3R-RESET`, option `--qual-allow-reset`, `--bench`. Sinon BLOCKED sans preuve ni commande.
- **PASS** : `reset` s'achève et le marqueur est vu après le curseur pris **avant** le reset, sans rupture de continuité. Sur un pont UART externe le port USB de l'hôte n'est pas celui de la carte : une rupture de continuité n'est **pas** attendue.
- **INDETERMINATE** : rupture de continuité ou issue du reset inconnue ; fenêtre fermée explicitement, jamais rejouée.
- **FAIL** : reset réglé, aucune rupture, pas de bannière dans le délai.
- **BLOCKED** : session ou option manquante, pas de capacité `reset`.
Nature de l'effet : redémarrage volontaire de la carte, pas d'écriture flash, pas de grant. Un seul reset par run. Limite : le marqueur est dérivé de la puce déclarée.

### S3R-2 : maintien BOOT (conception ouverte, non codée)
Constat sur le moteur (`web/esp-engine.js`) : `io0` maintient IO0 bas (DTR) pendant `duration_ms` (1..10000) **sans** reset ; `reset` (RTS) est une commande séparée ; deux commandes ne
s'exécutent pas en même temps (`Device is busy`) ; `io0` est une commande **privilégiée** (grant local requis). Une carte n'entre en mode téléchargement que si IO0 est bas **pendant** un reset.
Le banc n'a donc pas de séquence « IO0 bas + reset » en une commande. Options à trancher en revue, sans rien exécuter :
1. armer un déclencheur à reset différé (`trigger` avec action `reset` et délai) puis lancer `io0` sur une durée couvrant ce reset ; dépend du comportement de `busy` pendant le déclencheur (à lire et tester en simulation) ;
2. ajouter au moteur une commande combinée explicite (changement du cœur du banc, donc nouvelle revue) ;
3. déclarer le maintien BOOT hors du périmètre testable avec le banc et laisser l'exigence 1 partielle.
Critère proposé si l'option 1 ou 2 est retenue : PASS si la ROM annonce le mode téléchargement (marqueur à fixer **avant** le run à partir de la documentation de la ROM du S3, pas à partir des résultats) ; FAIL si une ROM normale est vue ; INDETERMINATE sinon. Grant requis : oui.

### S3R-3 : retour console après opération du chargeur (contrat)
Une lecture `flash.read` avec `return_console=true` d'une région déjà qualifiée (lecture seule, deux lectures égales), puis observation du retour console.
PASS si l'opération indique le retour console **et** qu'un événement UART postérieur au curseur est reçu sans rupture ; ce n'est qu'une preuve de transport. Aucune écriture, pas de grant.

## 2. R4, exigence 4 : programmation multi-images et manifeste réel

À écrire après revue de ce contrat.
- **Cible** : S3/UART (région d'essai à approuver à nouveau) puis C3. Trois régions **disjointes** de 8 Kio (ou un schéma approuvé par l'opérateur), jamais le secteur d'une partition ni la table de partitions.
- **Manifeste** : `CampaignManifest` v1 avec plusieurs `write_regions`, politique de sauvegarde et de restauration, `max_interruptions: 0`.
- **Déroulé** : sauvegardes vérifiées (deux lectures égales) de chaque région ; `flash.write` d'un plan à plusieurs images (motifs distincts, SHA-256 connus) **sans** `erase` global ; relecture indépendante de chaque région (égale au motif) ;
  vérification que les octets **hors régions** d'une fenêtre de contrôle (avant, entre et après les régions) sont inchangés ; restauration de chaque région et comparaison finale.
- **PASS** : toutes les régions égales à leur motif puis à leur sauvegarde, fenêtre de contrôle inchangée, aucune opération non résolue, grant révoqué. **FAIL** : toute différence. **INDETERMINATE** : issue inconnue d'une écriture ; restauration locale explicite, jamais de rejeu.
- Autorisations : région(s), carte, procédure de restauration, grant local par run.

## 3. R5/R6 : procédure de récupération (à relire avant toute recette destructive)

Objet : injection de fautes **déterministes** (exigences 5 et 6) : coupure USB ou arrêt du serveur à une phase donnée d'une programmation ; ACK ou résultat perdu. **Aucune recette destructive ne sera écrite
tant que cette procédure n'est pas relue.**

1. **Avant tout essai** : région d'essai disjointe, sauvegarde vérifiée (deux lectures égales, copie privée, SHA-256 consignés), carte et région nommées par l'opérateur, état propre (aucune quarantaine, réservation, opération non résolue, grant à 0).
2. **Un essai à la fois**, chacun avec son autorisation explicite (carte, région, phase visée, geste de coupure). Phases déterministes : après sauvegarde et avant effacement ; pendant l'effacement ; pendant l'écriture ; avant la relecture.
3. **Pendant** : le seul geste est celui annoncé (par ex. débrancher puis rebrancher). Aucun rejeu, aucune relance du serveur, aucune suppression du verrou ou du journal.
4. **Après** : l'opération est INDETERMINATE ou échouée ; le client ne l'améliore pas. Récupération **locale** par l'opérateur dans la page, puis `retire_after_recovery` ; nouvelle fenêtre de preuve ; **relecture** de la région d'essai ; restauration depuis la sauvegarde vérifiée avec consentement et grant ; comparaison finale égale à la sauvegarde.
5. **Si la région ne se restaure pas** : arrêt, aucune autre opération ; la carte reste dans cet état connu jusqu'à décision de l'opérateur. Une carte non amorçable est possible sur une région d'essai : c'est pourquoi la région est choisie sans partition ni table.
6. **Critères** : un essai est PASS seulement si l'état ambigu n'a **jamais** été présenté comme un succès (issue inconnue conservée), si la récupération a rétabli la sauvegarde et si aucun rejeu n'a eu lieu ; sinon FAIL ou INDETERMINATE selon les preuves.
7. **Arrêt immédiat** : opération non résolue après récupération, restauration différente, quarantaine persistante, bannière d'erreur de la ROM.
Questions pour la revue : quelle phase est réellement déterministe côté navigateur (le débranchement se fait au geste, pas à l'octet) ; peut-on instrumenter une coupure côté agent pour la rendre reproductible ; faut-il une carte sacrifiable.

## 4. Comparateur

Les scénarios `S3R-*` ne figurent pas dans `qualification_criteria.json`. Ajouter leurs critères change le SHA épinglé : nouvelle version des critères, nouvelle revue, puis nouvelle épingle avant tout run.
