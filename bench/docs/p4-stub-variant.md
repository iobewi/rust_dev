# Variante de stub expérimentale P4 : intégration, preuves, retour arrière, recette proposée

**Statut : intégration logicielle et simulation. NON déployée, NON chargée sur matériel, aucune recette exécutée.** Le comportement par défaut de Device Lab est inchangé : le stub officiel (celui d'esptool-js 0.7.0, esp-flasher-stub v1.2.2) reste le seul chargé tant qu'une commande ne demande pas explicitement la variante.

La variante est `p4-no-clock-boost` : le stub v1.2.2 reconstruit depuis les sources épinglées, sans la bascule DCDC / 200 MHz pour le transport UART du P4 ([préparation et comparaison des binaires](../experiments/p4-stub-no-clock-boost/README.md)). Le chemin de lecture ECO7 et la séquence d'attache sont ceux du v1.2.2.

## 1. Comment elle est sélectionnée

Seules deux commandes de lecture, `flash.read` et `flash.md5`, acceptent `stub_variant` (nom) et `stub_sha256` (empreinte épinglée), toujours ensemble. La variante vaut pour la durée de la session de chargeur ; `return_console` (vrai par défaut) la termine avec un reset de la carte.

| Contrainte du mandat | Où elle est tenue |
|---|---|
| 1. Le stub officiel reste le défaut | Aucun argument, aucune lecture du fichier de la variante, `runStub` d'esptool-js inchangé, vitesse inchangée (460800). Test Node et test de recette |
| 2. Sélection explicite, limitée au P4 et à une session autorisée | Nom + SHA-256 dans la commande ; appareil `esp32p4` (serveur) ; puce détectée `ESP32-P4` de révision ≥ 300 (agent, **avant** tout envoi) ; consentement `P4-STUB-VARIANT` dans la session privée de la recette ; `allow_stub_variant` par opération et scope `program` (serveur) ; refus sur l'interface manuelle et pour un appareil en quarantaine |
| 3. Binaire reconstruit, SHA-256 contrôlé | `bench/web/stub-esp32p4-noboost.json`, issu de `build.sh` ; trois contrôles indépendants : la commande porte l'empreinte épinglée par le serveur, l'agent compare à la sienne (`stub-variants.js`), puis hache le fichier lu (`911c8617…6f24`) et le champ `text` (`cc2865bb…1928`) ; un test garde les trois copies de l'empreinte et celle du patch d'accord |
| 4. Protections conservées | Réservation, identité JEDEC stricte à chaque commande (rang compté), arrêt sur erreur sans relance, preuves durables, consentement : la variante passe par `openLoader` et `execute` comme le stub officiel |
| 5. 115200 bauds | La session de la variante se connecte à 115200 (vitesse de la ROM, donc sans changement de vitesse) |
| 6. Pas de changement de lecture ni d'ECO7 | Le code du stub est celui du binaire vérifié ; l'agent n'ajoute que le téléversement, qui reproduit la séquence de `runStub` d'esptool-js 0.7.0 (`memBegin`/`memBlock` du texte puis des données, `memFinish`, attente de `OHAI`) |
| 7. Retour au stub officiel sans toucher à la flash | Voir §4 |

Les erreurs de sélection sont refusées **avant** que le port soit ouvert : nom inconnu, empreinte fausse, fichier altéré ou illisible, appareil d'une autre famille. Après `main()` d'esptool-js (détection de la puce, séquence d'alimentation flash ECO7) et avant le moindre octet téléversé : puce détectée autre que le P4, silicium v1.x, stub déjà actif. Un stub qui ne répond pas `OHAI` ferme la session (`stub_variant_start_failed`).

## 2. Aucun chemin d'écriture ou d'effacement

Cinq couches, chacune testée :
1. **Protocole** : `stub_variant` et `stub_sha256` n'existent que dans le schéma de `flash.read` et `flash.md5`. Avec `flash.write`, `flash.erase`, `partitions`, `identify`, `console`, `uart.write`, `mark` ou `reset`, la validation refuse.
2. **Serveur** : opt-in par opération, scope `program`, appareil non en quarantaine, interface manuelle refusée.
3. **Client Python** : `Board.command` refuse la variante sans `allow_stub_variant=True`, et pour toute autre commande que ces deux lectures.
4. **Agent** : garde en tête d'`execute` (toute commande portant ces arguments, hors lectures, est refusée) et liste `allowed_commands` du registre.
5. **Recette** : n'émet que `flash.md5` et `flash.read` (un test vérifie l'ensemble des commandes soumises).

Une session de variante ouverte refuse toute commande officielle (`loader_mode_mismatch`), et inversement ; une session ouverte avec le chargeur ROM de même.

## 3. Preuves

Chaque résultat de `flash.md5` et `flash.read` contient `stub_identity` : `official` (stub d'esptool-js, non vérifiable côté agent : `verified: false`) ou, pour la variante, `verified: true`, `file_sha256`, `text_sha256`, `uart_baud`, le commit du stub (`6f8dc174…`), celui de la bibliothèque (`d61983fa…`) et l'empreinte du patch. `ram_load_ops` (déjà en place) montre que le téléversement a eu lieu. La recette enregistre ces champs par opération et vérifie l'identité attendue de chaque série.

## 4. Retour arrière (aucune intervention sur la flash)

La variante n'existe que dans la RAM de la puce, pour une session de chargeur.
- **Fin normale** : `return_console` vrai (défaut) : la carte est réinitialisée ; la commande suivante sans `stub_variant` charge le stub officiel.
- **Session laissée ouverte, échec ou `OHAI` absent** : la commande `console` (ou un reset) ramène la carte à l'application ; aucune opération n'est rejouée.
- **Retrait de la fonction** : revenir au commit déployé précédent (`4707802cd…`) et envoyer SIGHUP au lanceur identifié, puis recharger la page : la variante n'existe plus dans la page servie.
- **Aucune étape** ne lit, n'écrit ni n'efface la flash, et aucun eFuse n'est touché.

## 5. Tests (simulation et doubles)

- Node `tests/stub-variant.test.mjs` (20) : vraies fonctions de l'agent, esptool-js et port simulés. Défaut officiel ; chargement avec l'empreinte épinglée, ordre et tailles du téléversement, vitesse, identité ; refus d'une empreinte fausse, d'un fichier altéré, inconnu, d'une autre puce ou révision, d'un stub déjà actif, d'un `OHAI` absent, de toute commande non lecture, du mode ROM ; sessions de modes différents refusées dans les deux sens ; réutilisation de la session de la variante ; retour au stub officiel.
- Python `python/tests/test_stub_variant.py` (59) : trois registres d'empreintes d'accord, validation du protocole (puces, commandes, quarantaine), session, comparaison pure, opt-in et scope côté serveur, client, recette contre le simulateur.
- Ils ne prouvent rien sur la vraie puce, le stub, esptool-js ni la flash.

## 6. Recette comparative `P4-STUB-VARIANT` (PC-10), à débit UART identique, non exécutée

**Pourquoi une nouvelle version.** Le premier run de la variante (STUBVAR1) a donné un MD5 égal à celui de la ROM, mais il a changé deux choses à la fois : la transition DCDC / 200 MHz et le débit (460800 pour le stub officiel, 115200 pour la variante). La comparaison se fait donc **à 115200 bauds pour les deux stubs**.

**Moyen.** Un argument `uart_baud` (seule valeur permise : `115200`, celle de la ROM) sur `flash.read` et `flash.md5`, pour le stub officiel comme pour la variante, jamais avec le chargeur ROM ni sur une autre commande. Sans l'argument, le comportement est inchangé (stub officiel à 460800). Le débit fait partie de l'identité de la session : une session ouverte à un débit n'est jamais réutilisée pour un autre. Le stub officiel reste le même binaire ; l'identité rapportée (`stub_identity.uart_baud`) donne le débit réellement utilisé.

**Séries.** Identiques, deux fois (officiel d'abord, variante ensuite, le même jour) : `flash.md5`, `flash.read` (avec le condensat final du stub), `flash.md5`, `flash.read`, `flash.md5` sur `0x2000`/4096 octets, une session de chargeur par opération : dix opérations, aucune écriture, aucun retry, arrêt au premier échec. Le MD5 de la ROM (`stub_variant.reference_rom_md5`) est la vérité de terrain.

**Analyse.** Séries analysées comme PC-7, plus une comparaison octet par octet : toute lecture dont le MD5 égale celui de la ROM fournit les octets de référence ; chaque lecture est comparée position par position (positions, bits, position dans le morceau de 16 octets, jamais le contenu) pour voir si les écarts du stub officiel, concentrés sur les octets 1 à 3 de leur morceau, disparaissent avec la variante. Sans lecture égale au MD5 de la ROM, rien n'est dit sur la justesse.

- **PASS** : les deux séries complètes, identités et débits conformes, série de la variante cohérente et égale au MD5 de la ROM ; la série officielle est reportée (SV-A si elle est instable) sans être exigée.
- **FAIL** : opération en échec, identité ou débit inattendu, condensat manquant, série de la variante incohérente (SV-C) ou différente du MD5 de la ROM (SV-E).
- **Traçabilité** : l'état final (`after.json`) est enregistré après la libération de la réservation ; tous les fichiers de preuve de cette recette sont privés (0600, dossiers 0700), vérifié par un test.
- Limite : un run, une plage de 4 Kio, des digests transportés par la même liaison ; aucune cause n'est établie.

## 7. Avant tout essai (rien de cela n'est fait)

Revue du SHA par le réviseur indépendant, nouvelle reconstruction de la variante par `build.sh` pour contrôler les empreintes, déploiement contrôlé (code servi modifié : redémarrage, rechargement de la page, reconnexion du seul P4), session privée écrite par l'opérateur avec son consentement explicite (carte, scénario, commit, effets, empreinte de la variante). Empreintes attendues du code servi par ce commit : voir le message de livraison.

Hors périmètre : le C6, GPIO54, PC-BACKUP, toute utilisation de la variante en production.
