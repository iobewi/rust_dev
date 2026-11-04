# Préparation de la qualification finale — issue #2

Ce document confronte chaque ligne de l'issue #2 aux contrôles logiciels réellement
présents et organise les essais physiques restants. Une simulation avec de vraies
interfaces HTTP/WS/SSE reste une **simulation du matériel**. Ce travail ne coche
aucune case matérielle et ne clôture pas l'issue.

Les bilans historiques S3 sur pont UART, S3 USB Serial/JTAG et le contrôle court C3
ont des preuves et limites propres, attachées aux sources effectivement exécutées.
Leurs archives privées restent chez Lionel. Ils ne sont pas reportés comme PASS
sur un nouveau SHA. Le lot C3 de `qualification-c3.md` doit encore être rejoué.

## Matrice de l'issue #2

Chaque ligne ci-dessous correspond à une exigence de l'issue. Les noms de fonctions
ou de tests permettent de retrouver le contrôle, sans dépendre d'un numéro de ligne.

| ID | Exigence | Preuves logicielles existantes ou ajoutées | Essais réels à attacher avant validation |
|---|---|---|---|
| Q01 | S3 UART : connexion, ROM, EN, BOOT, retour console | `engine.test.mjs` : reset et gardes ; `agent.test.mjs` : retour après ACK ; `browser.mjs` : parcours UART ; `session-lifecycle.test.mjs` : fermeture/reconnexion | Même S3, pont externe : port fermé réellement, boot capturé, impulsion EN/IO0 bornée, retour chargeur/console et observation des effets. Les scripts historiques BOOT ne sont pas une nouvelle recette publiée. |
| Q02 | S3 Serial/JTAG : identité/flash, renumérotation/OTG, EN/BOOT | `engine.test.mjs` : VID/PID reconnu, reset interne, OTG non reconnu, IO0 refusé ; `browser.mjs` : sélection UART/JTAG ; v2 M1/M6/M8 | Rejouer lectures et reset sur Serial/JTAG ; observer disparition/renumérotation réelle. OTG ne doit pas être confondu avec Serial/JTAG. Firmware OTG requis s'il faut provoquer cette transition. |
| Q03 | C3 : famille, flash, partitions, reset | `python/tests/test_c3_recipes.py` ; recettes `qualification_v2/test_c3.py` | Lancer le lot C3 dédié, relever table réelle et bannière ROM. Le contrôle privé précédent ne couvrait pas partitions/reset. |
| Q04 | Multi-images/manifeste, SHA et absence d'effacement global | `protocol.test.mjs` : résolution manifeste et erase=false ; `engine.test.mjs` : digests/secteurs ; `flash-op.test.mjs` : écritures/MD5/échecs ; `test_http.py::test_complete_simulation_with_independent_api_client` ; préparation hors ligne ci-dessous | Région autorisée, sauvegarde propre à la carte, programmation réelle de deux images, égalité des relectures, témoins inchangés, restauration vérifiée. Le simulateur RAM ne prouve pas le comportement de l'effaceur physique. |
| Q05 | Coupures USB/réseau, onglet, serveur : pas de rejeu/blocage/récupération | `test_core.py` : disconnect/indeterminate/no replay ; `agent.test.mjs` : ACK perdu, hold persistant, génération ; `session-lifecycle.test.mjs` : même port uniquement ; `test_fault_campaign.py` : redémarrage serveur au même port et ancienne instance refusée ; v2 M8/M8b | Débranchement lecture puis écriture autorisée, liaison agent/serveur coupée, onglet fermé/rechargé, serveur redémarré. Vérifier génération/epoch, absence de nouvelle soumission et récupération explicite après relecture. |
| Q06 | Timeout/résultat/ACK tardif : aucun retour console prématuré | `agent.test.mjs` : délais d'exécution et ACK exact avant retour ; `test_http.py::test_late_result_acknowledges_server_quarantine` ; `test_core.py::test_timeout_late_success_is_evidence_only` ; `python/tests/test_client.py` : POST perdu et settled | Injection ciblée au transport de recette, avec IDs/horodatages. Préciser ce qui est une injection logicielle pendant un essai réel. Ne pas couper au hasard une écriture non sauvegardée. |
| Q07 | Déclencheurs : désarmement/réarmement/reset différé/concurrence | `engine.test.mjs` : motif fragmenté, one-shot, disarm et rearm ; `agent.test.mjs` : lease revoked et lifetime ; `campaign-guards.test.mjs` : action différée inhibée si expirée/révoquée/busy/quarantaine/chargeur | Motif déterministe du firmware ou générateur de recette, action unique, expiration/révocation/désarmement avant échéance et concurrence. Pas de PASS sur un motif absent. |
| Q08 | Capture soutenue, arrière-plan, SSE et pertes | `engine.test.mjs` : trames binaires bornées ; `console-buffer.test.mjs` : brut intact/UTF-8/rétention ; `api-client.test.mjs` : SSE fragmenté ; `test_fault_campaign.py` : 3 Mio via WS/SSE, fermeture/reprise à next, pertes source et anneau ; `session-lifecycle.test.mjs` : wake lock | Source réelle déterministe, hash/longueur de capture archivés, onglet masqué puis visible, consommateur SSE externe. La simulation 3 Mio ne mesure ni débit USB réel ni scheduling d'un navigateur masqué. |
| Q09 | Grants/révocation/leases/deux cartes de même famille | `test_core.py` : scopes/consentement/grant/expiry ; `test_ui_sessions.py` : autorité locale ; v2 M5 ; `browser.mjs` : deux agents ; `test_fault_campaign.py` : deux S3 simulées, routage strict et déconnexion isolée | Grants depuis la page, révocation, expiration et essais réels à deux cartes. C3 + S3 est utile mais ne remplace pas deux cartes de même famille. |
| Q10 | HTTP LAN et sécurité navigateur | `test_core.py::test_http_allowlist_private_literal_and_permissions` ; `test_ui_sessions.py` / `test_local_forward.py` ; `campaign-guards.test.mjs` : refus avant fetch, no-cors/omit/error, abort/échec et verified=false | Navigateur/version/origine effectifs, service LAN de recette contrôlé, refus hors liste, redirection et permissions réseau. Une réponse opaque n'est jamais un succès HTTP vérifié. |

Les diagnostics HTTP LAN restent volontairement filtrés : les refus/échecs du
moteur exposent actuellement `hardware_command_failed`, phase `control`, avec type
d'erreur sûr ; ils ne reprennent pas les messages arbitraires ni identifiants de
l'URL. Les tests vérifient surtout l'absence de fetch pour une origine interdite et
l'absence de faux succès. Une meilleure distinction des causes peut être traitée
séparément ; elle n'est pas revendiquée ici.

## Tests ajoutés par cette préparation

- `test_fault_campaign.py` utilise un vrai serveur isolé et des agents WS synthétiques :
  deux cartes de même famille, refus d'un ready provenant du mauvais agent,
  déconnexion de B sans déplacement de l'opération A ; 3 Mio UART synthétiques
  consommés par deux abonnements SSE successifs sans perte/duplication ; lacunes
  explicites ; redémarrage du serveur au même port avec refus de l'ancien Board
  avant tout POST.
- `campaign-guards.test.mjs` contrôle l'action différée à son échéance et les appels
  fetch simulés. Aucune requête n'est émise sur un vrai réseau LAN.
- `test_flash_preparation.py` vérifie sauvegarde, géométrie, permissions locales,
  manifests sans effacement et impossibilité d'écraser un dossier de préparation.

Reproduire sans matériel :

```sh
python -m pip install -r bench/requirements.txt
python -m pip install -e './bench/python[pytest]'
python -m pytest bench/python/tests bench/recipes -q
# Node 22 ou plus récent, même environnement Python actif :
npm test --prefix bench
```

Les parcours Chromium de la CI restent des simulations des ports. Ne pas les
convertir en PASS matériel dans le bilan final.

## Ordre de passage pour la campagne réelle

1. **Inventaire et versions.** Choisir un SHA unique des recettes, relever séparément
   client, serveur et page réellement chargée. Observer ou déclarer le transport,
   sans déduire le VID/PID des seules capacités. Cartes identifiées et états propres.
2. **Lectures C3.** Fichier `qualification_v2/test_c3.py`, puis son reset explicite.
   Appliquer `qualification-c3.md`. Archives propres à la C3, pas de sauvegarde S3.
3. **Lectures S3, UART puis Serial/JTAG.** Fichier `test_read_only.py` : M1/M5 puis M6
   avec référence vérifiée ; M4 séparé, en échec attendu. Compléter les contrôles
   partitions/ROM/BOOT à partir des recettes historiques conservées, en les versionnant
   et en vérifiant leurs assertions avant exécution. Ils ne sont pas encore tous
   présents dans la v2 publiée : ne pas annoncer ce lot comme prêt par simple copie.
4. **Interruption lecture et génération.** M8 puis contrôle M1, M8b puis contrôle M1.
   Le M8 S3 actuel demande 10 Mio : ne pas le lancer tel quel sur une C3 de 4 Mio.
   Préparer une version C3 dimensionnée avant d'y reprendre le scénario.
5. **Deux cartes et droits.** Distinguer deux cartes physiques de deux clients sur
   une carte. Grants et gestes navigateur regroupés, sans commande destructive.
6. **Déclencheurs, UART soutenu, SSE et arrière-plan.** Définir d'abord une source
   d'octets connue et un oracle de comparaison. Sinon NON EXÉCUTÉ. Garder le brut
   sensible privé et relever explicitement toute lacune.
7. **HTTP LAN.** Démarrer un service de recette contrôlé ; journaliser les requêtes
   côté service pour prouver réception/redirection sans lire une réponse opaque.
8. **Écritures et coupures autorisées.** Préparation ci-dessous, approbation du
   périmètre concret, essais isolés puis restauration. Relecture et état final.

Pour réduire les gestes, terminer les lots d'un transport avant de changer de
câble. Les gestes manuels ne sont pas une pause/reprise durable du moteur : lancer
un test identifié, attendre son message ready, faire le geste et conserver tout
échec. Ne pas rejouer automatiquement une commande physique.

## Préparer les images et la restauration hors ligne

`bench/tools/prepare_flash_qualification.py` ne contacte aucun banc et n'effectue
aucune écriture flash. Il demande une sauvegarde complète avec son SHA-256 vérifié,
la famille et des **adresses fournies par l'opérateur**. Il ne choisit pas une zone
libre et ne déduit aucune propriété d'une plage à FF ou d'une absence de partition.

Après identification de deux secteurs candidats et d'au moins un secteur témoin,
préparer un dossier privé nouveau (variables ci-dessous à renseigner, aucun offset
universel proposé) :

```sh
umask 077
python bench/tools/prepare_flash_qualification.py \
  --backup "$FULL_BACKUP" --sha256 "$FULL_BACKUP_SHA256" --chip "$TARGET_CHIP" \
  --flash-size "$PHYSICAL_FLASH_BYTES" \
  --image-address "$IMAGE_A" --image-address "$IMAGE_B" \
  --witness-address "$WITNESS" --output "$NEW_PRIVATE_PREPARATION"
```

Le résultat comprend deux motifs déterministes différents des secteurs sauvegardés,
un manifeste ESP Web Tools avec erase=false, les secteurs originaux et leur manifeste
de restauration, les témoins avant essai et `review-plan.json`. Les secteurs doivent
être distincts, alignés et contenus dans la sauvegarde, dont la taille doit égaler
la capacité physique déclarée. Les fichiers sont privés
0600, le dossier 0700 et aucun fichier précédent n'est écrasé.

Le plan porte `approved:false`, `restoration_verified:false` et
`flash_operations_executed:0` : il est **à examiner**, pas à exécuter automatiquement.
Les motifs ne sont pas des firmwares exécutables. Ne jamais les programmer dans
une région utilisée par l'amorçage ou l'application, et ne pas redémarrer sur une
carte dont la restauration n'est pas terminée. Aucun eFuse ni paramètre permanent.

Avant autorisation, confirmer identité/capacité, actualité de la sauvegarde et
propriété de chaque région. Ajouter assez de témoins distribués pour l'objectif
de l'essai : un témoin inchangé ne prouve pas à lui seul toute l'absence d'effacement
global. Une comparaison de toutes les régions non ciblées donne une preuve plus
large ; tenir compte des écritures normales éventuelles du firmware.

Le manifeste résout les fichiers/adresses, mais `manifestPlan` ajoute actuellement
`return_console:true`. **Ne pas utiliser le parcours UI d'installation tel quel
pour ces motifs.** Après résolution du manifeste, l'appel API doit explicitement
remplacer ce champ par `return_console:false` ; le plan de revue le signale par
`manifest_requires_args_override:true`. Ce champ du plan n'est pas un garde moteur.

Après autorisation : charger les artefacts locaux par l'API, vérifier leur SHA,
programmer **sans erase global** et avec `return_console:false`, relire chaque motif
et témoin, puis réécrire les secteurs originaux et contrôler leur égalité. Le grant
local et les consentements restent nécessaires : ce script ne les fournit pas.
Les scénarios de coupure et la restauration via la seule liaison disponible doivent
être examinés avant de commencer. Une sauvegarde n'est pas une restauration vérifiée.

## Règle de bilan et de clôture

Produire une ligne par Q01..Q10, carte, transport et scénario : SHA, recette/hash,
préconditions, attendu/observé, PASS/FAIL/NON EXÉCUTÉ, preuve et limites. Conserver
les assertions et scripts réellement exécutés, y compris les échecs. L'absence de
matériel, de générateur ou de capture n'est pas un PASS.

Le paquet partageable contient sources, hashes, commandes, JUnit, logs filtrés et
preuves automatiques ; dumps/captures et secrets restent privés. La révision de page
non relevée ou le transport seulement déclaré doivent rester explicitement inconnus.

L'issue #2 n'est clôturable que si les exigences sont exécutées et revues, ou si une
exclusion de périmètre est explicitement acceptée par Lionel. La préparation
logicielle présente ne constitue pas une telle exclusion et ne permet pas une
validation « définitive » du matériel.
