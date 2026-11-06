# Issue #2, phase 2 : contrats des recettes ciblées (R1, R4) et procédure de récupération (R5/R6)

**Statut : contrats et code pour revue. Rien n'a été exécuté sur matériel ; aucun accès matériel, aucun grant, aucune écriture flash, aucun redémarrage serveur.**
Audit de référence : [issue-2-coverage-audit.md](issue-2-coverage-audit.md). Les critères ci-dessous sont écrits **avant** tout run ; une correction crée une nouvelle version,
une nouvelle revue et un nouveau dossier de preuves. Un résultat inconnu reste INDETERMINATE. Le retour de la bannière ROM n'est **pas** un démarrage applicatif
(`boot_verified` vaut toujours `false`). Les tests de cette tranche sont des **simulations** : ils prouvent des décisions de code, jamais le comportement d'une carte.

## 1. R1, exigence 1 : S3 + pont UART externe

Trois recettes séparées (un effet matériel par recette, chacune avec son consentement) :

| Id | Effet | Consentement / option | État |
|---|---|---|---|
| **S3R-1** | un reset EN (RTS), bannière ROM `ESP-ROM:esp32s3` dans la même fenêtre de preuve | `S3R-RESET`, `--qual-allow-reset` | code, testé en simulation |
| **S3R-2** | entrée en mode téléchargement par la commande atomique `boot.download` | `S3R-BOOT`, `--qual-allow-bootloader`, grant local | code (moteur + recette), testé en simulation |
| **S3R-3** | retour console après une opération du chargeur | `C3R-READ`-équivalent S3 (à définir à la revue) | contrat, code non écrit |

### S3R-1
Prérequis : session `esp32s3` / `external_uart`, `S3R-RESET`, `--qual-allow-reset`, `--bench`. Sinon BLOCKED **avant** toute preuve ni commande.
Les cas BLOCKED détectés **après** `case.start()` (par exemple une capacité `reset` absente) laissent au contraire une trace de campagne (dossier, `report.json`) : seule la
vérification préalable de la session est sans trace.
- **PASS** : `reset` s'achève et le marqueur est vu après le curseur pris **avant** le reset, sans rupture de continuité. Sur un pont UART externe le port USB de l'hôte n'est pas celui de la carte : une rupture de continuité n'est **pas** attendue.
- **INDETERMINATE** : rupture de continuité ou issue du reset inconnue ; fenêtre fermée explicitement, jamais rejouée.
- **FAIL** : reset réglé, aucune rupture, pas de bannière dans le délai. **BLOCKED** : voir ci-dessus.

### S3R-2 : commande atomique `boot.download` (décision de revue : option « commande atomique »)
Un déclencheur différé combiné à `io0` est **écarté** (dépend de `busy` et des temporisations).
**Contrat de la commande** (`bench/protocol`, `web/esp-engine.js`, `web/agent.js`) :
- séquence : `DTR=1,RTS=1` (IO0 bas, EN bas) ; pause 100 ms ; `DTR=1,RTS=0` (EN relâché, IO0 encore bas : la ROM échantillonne le strapping) ; maintien `hold_ms` ; `DTR=0,RTS=0` (**relâche des deux lignes**) ;
- argument `hold_ms` **obligatoire**, entier 100..3000, aucun défaut ; champs inconnus refusés ;
- réservée au pont **UART externe** (`external_uart_required` sinon) et à la console ouverte (`console_required` sinon) ; jamais annoncée sur USB natif ;
- **privilégiée** : grant local vivant exigé par le serveur (`grant_required` sinon) et par l'agent (`PRIVILEGED`) ;
- exclusivité : une seule opération à la fois (`busy`), pas de combinaison avec un déclencheur ;
- pauses **coopératives** par tranches de 50 ms qui revérifient bail, grant et échéance ; une révocation ou une expiration coupe le maintien ;
- relâche systématique des deux lignes sur **tous** les chemins où le transport répond (erreur d'écriture d'un signal, bail ou grant terminés) ; si la relâche elle-même échoue, l'état des lignes est inconnu : le moteur passe en **récupération opérateur** (`signal_release_failed`), il ne se déclare pas au repos ;
- aucun rejeu automatique ; résultat `{download_mode_requested:true, method:"en+io0", hold_ms, boot_verified:false}` : la commande **demande** le mode, elle ne le vérifie pas.
**Recette** `test_s3_uart_boot.py` : session `esp32s3`/`external_uart`, consentement `S3R-BOOT`, `--qual-allow-bootloader`, `bootloader.marker` **fixé avant le run d'après la documentation de la ROM du S3**, sans défaut (BLOCKED sinon) ;
l'opérateur donne le grant le plus court et ne fait ensuite rien (expiration naturelle) ; la carte reste en mode téléchargement jusqu'à son prochain reset (geste opérateur hors recette).
- **PASS** : commande réglée, marqueur vu après le curseur, même fenêtre de preuve, sans rupture. **FAIL** : réglée, pas de marqueur dans le délai. **INDETERMINATE** : rupture de continuité ou issue inconnue. **BLOCKED** : voir plus haut ou grant non observé.
Limites : prouve une émission en mode téléchargement, pas un démarrage applicatif ; la correspondance DTR→IO0 et RTS→EN suppose le câblage usuel du pont, **non mesuré** (même logique que `io0` et `reset` existants). Le simulateur émet un faux marqueur `SIM-DOWNLOAD-MODE` qui n'a aucune valeur de preuve sur matériel.

### S3R-3 : retour console après une opération du chargeur (contrat renforcé)
Un événement UART quelconque ne prouve pas que le chargeur a rendu la main à la console. Preuve exigée, dans l'ordre :
1. l'opération `flash.read` (région déjà qualifiée en lecture seule, `return_console=true`) est **réglée** et son accusé accepté par le serveur ;
2. un événement de transport **explicite** de retour console (« console opened ») postérieur au curseur ;
3. le texte UART n'est qu'un indice : si l'on veut une sortie console effective, il faut un **marqueur déterministe distinct**, fixé avant le run ; aucun marqueur applicatif n'est supposé et `boot_verified` n'est jamais invoqué.
PASS = 1 + 2 (preuve de **transport**) ; FAIL si l'opération est réglée et qu'aucun événement de retour n'arrive ; INDETERMINATE si continuité rompue ou issue inconnue.

## 2. R4, exigence 4 : programmation multi-images et manifeste réel

À écrire après revue de ce contrat.
- **Région possédée et sauvegardée** : chaque région d'essai est explicitement approuvée par l'opérateur, sauvegardée (deux lectures égales), **hors** bootloader, table de partitions et données actives. Une partition de test **dédiée** qu'il possède peut servir ; il n'y a pas d'exclusion absolue des partitions.
- **Manifeste** : `CampaignManifest` v1 avec plusieurs `write_regions` ; politique de sauvegarde et de restauration ; `max_interruptions: 0`.
- **Unité atomique** : l'unité d'approbation et de restauration est la **région** (secteurs de 4 Kio entiers), jamais l'image ; le plan `flash.write` multi-images est un seul envoi qui écrit toutes les images ou aucune garantie d'atomicité n'est revendiquée (le résultat partiel reste INDETERMINATE).
- **Oracle « pas d'effacement global »** : une fenêtre locale ne suffit pas. Avant et après l'essai, lecture et SHA-256 par segments de **toute la flash lisible** (ou de toutes les zones hors régions approuvées que l'opérateur déclare), comparaison aux sauvegardes d'avant ; seules les régions approuvées ont changé.
- **PASS** : chaque région égale à son motif puis à sa sauvegarde, zones hors régions inchangées, aucune opération non résolue, grant révoqué. **FAIL** : toute différence. **INDETERMINATE** : issue inconnue d'une écriture ; restauration locale explicite, jamais de rejeu.
- Autorisations : carte, régions, procédure de restauration, grant local par run.

## 3. R5/R6 : procédure de récupération (non approuvée ; aucune recette destructive tant qu'elle n'est pas relue)

Objet : injection de fautes **déterministes** (exigences 5 et 6). Constats qui **excluent** deux hypothèses : `retire_after_recovery` et la page **ne débloquent pas** à eux seuls une opération incertaine ; un arrêt du serveur ne peut pas être synchronisé sur une phase exacte d'une écriture depuis le client.

1. **Injection côté agent** : les coupures se déclenchent par une **instrumentation de l'agent** à un point d'exécution nommé (avant effacement, après effacement, après N blocs, avant relecture) et non par un geste humain imprécis. Cette instrumentation est une modification du cœur du banc : conception, code et revue **avant** toute recette, opt-in explicite, désactivée par défaut, inerte en production.
2. **États à décrire pour chaque faute** (à compléter à la revue) : opération (`indeterminate`/`failed`), bail (libéré ou conservé), grant (expiré/révoqué), verrou et journal SQLite (inchangés, jamais supprimés), quarantaine de l'agent, génération/epoch, artefacts serveur retenus.
3. **Réconciliation du journal** : après redémarrage du serveur, les opérations incertaines restent `indeterminate` ; l'opérateur reprend la main localement, **sans rejeu** ; aucune opération n'est promue en succès par le client ni par le comparateur.
4. **Chemin de restauration local, indépendant du serveur** lorsque celui-ci est indisponible : défini par l'opérateur (par exemple l'outil de programmation officiel exécuté **sur son poste**, hors devcontainer et hors banc), à partir de la **sauvegarde privée vérifiée** (SHA-256 consignés), avec lecture de contrôle indépendante. Ce chemin doit être validé sur une région d'essai **avant** toute injection destructive. Je n'exécute rien de cela.
5. **Avant tout essai** : région disjointe de bootloader, table et données actives ; sauvegarde vérifiée ; carte et région nommées ; état propre (grant 0, aucune réservation, aucune quarantaine, aucune opération non résolue).
6. **Un essai à la fois**, chacun avec son autorisation explicite (carte, région, point d'injection, procédure de restauration). Aucun rejeu, aucune relance non coordonnée du serveur, aucune suppression du verrou ou du journal.
7. **Critères** : un essai est PASS seulement si (a) l'état ambigu n'a jamais été présenté comme un succès, (b) la région est restaurée **et** comparée égale à la sauvegarde, (c) l'**absence de rejeu est démontrée indépendamment** (journal des opérations et événements de l'agent : une seule exécution du point injecté, aucune soumission supplémentaire). Une restauration égale sans preuve d'absence de rejeu n'est **pas** un PASS.
8. **Arrêt immédiat** : opération non résolue après récupération, restauration différente, quarantaine persistante, signal de relâche en échec (`signal_release_failed`), erreur de la ROM.
Questions pour la revue : choix des points d'injection ; forme de l'instrumentation agent ; faut-il une carte sacrifiable ; validation du chemin de restauration local.

## 4. Critères épinglés

`qualification_criteria.json` (SHA `2dfc47e2…760760c`) et les anciens runs restent **inchangés**. Les scénarios `S3R-*` y sont inconnus. Il faut une **nouvelle version candidate** des critères (nouveau fichier ou nouvelle version
explicite), revue avant tout run `S3R-*`, avec son propre SHA épinglé ; les campagnes déjà produites restent comparables à l'ancienne version.
