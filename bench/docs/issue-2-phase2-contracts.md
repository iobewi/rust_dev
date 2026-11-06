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
| **S3R-3** | retour console après une opération du chargeur | `S3R-READ` | code (`test_s3_uart_console_return.py`), testé en simulation |

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
- séquence pour le circuit d'auto-reset **classique à deux transistors** (esptool `ClassicReset`), table de vérité (DTR, RTS) → (EN, IO0) : (0,0)→(1,1), (1,1)→(1,1), (0,1)→EN bas, (1,0)→IO0 bas ; asserter **les deux** ne provoque donc **pas** de reset (défaut relevé par la revue de `c28b18a`) :
  1. `DTR=0,RTS=1` : EN bas, IO0 haut (puce en reset) ; pause 100 ms ;
  2. `DTR=1,RTS=0` : EN relâché pendant que IO0 est bas (la ROM échantillonne le strapping au front montant de EN) ; maintien `hold_ms` ;
  3. `DTR=0,RTS=0` : **relâche des deux lignes** ;
- **hypothèse de câblage, non mesurée** : le pont de la carte utilisée implémente ce circuit classique (même hypothèse que `reset` et `io0` existants). Un pont sans ce circuit peut se comporter autrement : à confirmer sur le schéma de la carte réelle avant la qualification ; un PASS ne vaut que pour cette carte ;
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
Limites : prouve une émission en mode téléchargement, pas un démarrage applicatif ; la correspondance DTR→IO0 et RTS→EN suppose le câblage usuel du pont, **non mesuré** (même logique que `io0` et `reset` existants). Les tests Node vérifient la séquence à travers un **modèle de la table de vérité du circuit** (EN bas avec IO0 haut, un seul front montant de EN avec IO0 bas, lignes relâchées à la fin) et rejettent l'ancienne séquence (DTR et RTS ensemble) ; ce modèle ne remplace pas une mesure du câblage réel. Le simulateur émet un faux marqueur `SIM-DOWNLOAD-MODE` qui n'a aucune valeur de preuve sur matériel.

### S3R-3 : retour console après une opération du chargeur (contrat renforcé)
Un événement UART quelconque ne prouve pas que le chargeur a rendu la main à la console. Preuve exigée, dans l'ordre :
1. l'opération `flash.read` (région déjà qualifiée en lecture seule, `return_console=true`) est **réglée** et son accusé accepté par le serveur ;
2. un événement de transport **explicite** de retour console (« console opened ») postérieur au curseur ;
3. le texte UART n'est qu'un indice : si l'on veut une sortie console effective, il faut un **marqueur déterministe distinct**, fixé avant le run ; aucun marqueur applicatif n'est supposé et `boot_verified` n'est jamais invoqué.
Code : `test_s3_uart_console_return.py` (consentement `S3R-READ`, une plage de `readonly.reads` déjà qualifiée stable, aucun geste, aucun grant). PASS = 1 + 2 (preuve de **transport**) ; FAIL si l'opération est réglée et qu'aucun événement de retour n'arrive ; INDETERMINATE si continuité rompue ou issue inconnue.

## 2. R4, exigence 4 : programmation multi-images et manifeste réel

Code : `test_r4_multi_image.py` ; testé en **simulation** (le simulateur sait injecter un effacement global implicite pour vérifier que l'oracle le voit). Session : 2 à 4 `approved_regions`, consentement `R4`, `--bench-allow-destructive`, `r4.control_windows` (au moins une fenêtre non vide avant le run, sinon BLOCKED : un oracle tout `0xFF` ne détecte pas un effacement) et `r4.owned_partitions` (seules partitions pouvant recouvrir une région).
- **Région possédée et sauvegardée** : chaque région d'essai est explicitement approuvée par l'opérateur, sauvegardée (deux lectures égales), **hors** bootloader, table de partitions et données actives. Une partition de test **dédiée** qu'il possède peut servir ; il n'y a pas d'exclusion absolue des partitions.
- **Manifeste** : `CampaignManifest` v1 avec plusieurs `write_regions` ; politique de sauvegarde et de restauration ; `max_interruptions: 0`.
- **Unité atomique** : l'unité d'approbation et de restauration est la **région** (secteurs de 4 Kio entiers), jamais l'image ; le plan `flash.write` multi-images est un seul envoi qui écrit toutes les images ou aucune garantie d'atomicité n'est revendiquée (le résultat partiel reste INDETERMINATE).
- **Oracle « pas d'effacement global »** : une fenêtre locale ne suffit pas. Avant et après l'essai, lecture et SHA-256 par segments de **toute la flash lisible** (ou de toutes les zones hors régions approuvées que l'opérateur déclare), comparaison aux sauvegardes d'avant ; seules les régions approuvées ont changé.
- **Relectures et artefacts** : toutes les relectures de R4 (régions, fenêtres) passent par `ReadbackRetention` : l'artefact serveur n'est libéré qu'après relecture de la copie privée et égalité de sa taille et de son SHA-256 ; une copie absente ou différente **conserve** l'artefact serveur. Limite connue, hors de cette PR : `Orchestrator.backup` re-vérifie sa copie locale avant de supprimer l'artefact, mais `Orchestrator.compare` supprime l'artefact après calcul du SHA sans re-vérifier la copie privée que `read_flash` a écrite.
- **Portée de l'oracle** : les fenêtres de contrôle détectent une modification **uniquement dans les zones qu'elles couvrent** ; elles ne démontrent **pas** l'intégrité de toute la flash, et aucune affirmation « pas d'effacement global » ne va au-delà d'elles.
- **Échec après le début de l'écriture** : **une seule** restauration contrôlée (restaurer chaque région depuis sa sauvegarde vérifiée, puis comparer), **seulement** si toutes les opérations sont réglées et le handle intact. Avec une opération non résolue ou indéterminée, une continuité rompue ou un échec de la restauration elle-même, **rien n'est rejoué** : la récupération reste à l'opérateur et le champ de restauration l'indique. Les fenêtres de contrôle ne sont jamais « restaurées » (non approuvées en écriture). Si le grant est encore actif après l'échec, la recette le signale.
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

### 3 bis. Conception proposée de l'injection côté agent (à relire ; aucun code)
- **Opt-in triple** : option d'agent explicite au démarrage de la page, scénario de campagne `FAULT-*` et consentement par essai ; absent par défaut, inerte sans les trois ; refusé si la carte n'est pas déclarée sacrifiable.
- **Points nommés** et un seul actif par essai : `before_erase`, `after_erase`, `after_n_blocks(n)`, `before_readback`. L'agent s'arrête **au point** et lève une panne simulée côté transport (fermeture du port) ou côté liaison serveur (fermeture du WebSocket), jamais une coupure d'alimentation.
- **Journal de l'injection** : l'agent consigne dans ses événements le point atteint, l'action et l'absence de rejeu (compteur d'exécutions par opération), ce qui fournit la preuve indépendante d'absence de rejeu exigée par le critère 7.
- **Aucune coupure du serveur** avec cette méthode : l'arrêt du serveur pendant une écriture reste une étape séparée, non synchronisée sur une phase, à décider avec la procédure de redémarrage coordonné.
- **À trancher en revue** : forme du consentement, carte sacrifiable, chemin de restauration local (point 4), réconciliation du journal après coupure.

## 4. Critères épinglés

`qualification_criteria.json` (SHA `2dfc47e2…760760c`) et les anciens runs restent **inchangés**. Les scénarios `S3R-*` et `R4` y sont inconnus, et le comparateur n'a pas de contrôle propre à ces scénarios : l'étendre (critères **et** vérifications) est un travail séparé, à relire, avant tout run. Il faut une **nouvelle version candidate** des critères (nouveau fichier ou nouvelle version
explicite), revue avant tout run `S3R-*`, avec son propre SHA épinglé ; les campagnes déjà produites restent comparables à l'ancienne version.
