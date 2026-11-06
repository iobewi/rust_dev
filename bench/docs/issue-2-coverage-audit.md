# Audit de couverture de l'issue #2 : matrice des écarts

**Statut : audit, brouillon pour revue. Aucun accès matériel, aucun grant, aucune écriture flash, aucun redémarrage serveur.**
Base : `main` au commit `69f4183`. Les dix exigences sont celles de l'**issue #2** (« Qualifier le banc API v1 sur ESP32-S3 et ESP32-C3 réels »), telles que
confrontées au texte de l'issue lors de la revue de la PR #20 (version `cd115b2`). La première version de cet audit avait repris par erreur la
liste de `coverage.md` (qui découpe autrement les sujets) : elle est **remplacée**.

Les dix exigences contractuelles :

| # | Exigence de l'issue #2 |
|---|---|
| 1 | ESP32-S3 avec pont UART : connexion, capture ROM, reset EN, maintien BOOT, retour console |
| 2 | ESP32-S3 en USB Serial/JTAG natif : identité, programmation, relecture, réénumération ou disparition USB (critère OTG), refus de EN/BOOT. USB Serial/JTAG et USB OTG sont deux modes distincts, jamais présumés interchangeables |
| 3 | ESP32-C3 : famille, programmation, partitions, particularités de reset |
| 4 | Programmation multi-images et manifeste réel : SHA-256 des dumps, aucun effacement global implicite |
| 5 | Interruptions USB/réseau, fermeture/rechargement de page, redémarrage serveur : aucun rejeu, blocage maintenu, récupération par l'opérateur |
| 6 | Timeouts, résultats et ACK tardifs ou perdus : pas de retour console automatique avant acceptation par le serveur |
| 7 | Déclencheurs UART : désarmement/réarmement, reset différé, verrouillage, concurrence avec les autres commandes |
| 8 | Capture UART soutenue, onglets en arrière-plan, clients SSE externes, détection explicite des pertes |
| 9 | Grants locaux, révocations, expiration des réservations, essais avec deux cartes de la même famille |
| 10 | HTTP LAN et politiques de sécurité du navigateur conservées |

Sujets pertinents mais **hors des dix exigences** (traités à part, ils ne remplacent aucune exigence) : apparence et interactions de l'interface sur mobile et bureau,
détection effective de `navigator.serial` (voir §3 bis).

## 1. Preuves matérielles disponibles et leur portée

Seules les campagnes exécutées sur `main` (recettes fusionnées, provenance propre, comparateur épinglé) sont réutilisées. Les preuves privées ne sont
pas dans Git ; elles sont référencées par nom de dossier.

| Réf. | Carte / transport | Contenu | Verdicts |
|---|---|---|---|
| **A** `s3-uart-9ad5d1f` + `s3-uart-regression-0e1f890` | S3 N16, pont UART externe | Q1, Q2a–d, Q3 (une exécution + non-régression) | PASS (un FAIL Q2a historique, geste opérateur) |
| **B** `s3-uart-repeat-69f4183` | idem | 3 passages Q1, Q2a–d, Q3 ; comparateur épinglé `2dfc47e2…760760c` | tous PASS vérifiés sauf Q2b tentative 1 = FAIL (geste opérateur) |
| **C** `c3-usb-jtag-qualification-0e1f890` | C3, USB Serial/JTAG natif | C3R-0..3, Q1, Q2a–d, Q3 | PASS |
| **D** `c3-usb-jtag-repeat-69f4183` | idem | 1 passage C3R-0..3, Q1, Q2a–d, Q3 | PASS vérifiés |
| **E** historique `d548dab` / `dafe4b8` (issue #2, commentaire de bilan) | S3 N16, pont UART | identité, partitions, contrôle, IO0, lectures, débranchement pendant une lecture | **antérieur** aux corrections actuelles ; ne qualifie pas `main` |

Ce que Q1/Q2/Q3/C3R démontrent réellement est détaillé dans [qualification-matrix.md](qualification-matrix.md). Rappels qui limitent la réutilisation :
`boot_verified` vaut toujours `false` ; la bannière ROM n'est pas un démarrage applicatif ; le transport est déclaré par l'opérateur et inféré par l'agent,
jamais mesuré ; l'égalité du SHA d'une région vierge n'identifie pas une carte.

## 2. Matrice des écarts par exigence

Légende : ✅ démontré sur matériel (réf.) · 🟡 partiel · ❌ non démontré sur matériel · 🧪 couvert en simulation seulement (le logiciel n'est pas une qualification matérielle).

| # | Exigence (issue #2) | Sous-exigence | État | Preuve existante | Écart / limite |
|---|---|---|---|---|---|
| 1 | S3 + pont UART | ouverture du port, identification, partitions | ✅ | A, B (Q1 : table de partitions, lectures) ; E | |
| 1 | | capture de la ROM au démarrage | ❌ | aucune sur S3 (la bannière `ESP-ROM:` est observée sur C3, réf. D) | à qualifier sur S3 |
| 1 | | reset EN | ❌ | primitives testées en simulation 🧪 ; E (historique, antérieur) | reset EN S3 jamais rejoué sur `main` |
| 1 | | maintien BOOT | ❌ | 🧪 ; E (IO0, antérieur) | idem |
| 1 | | retour console après esptool | 🟡 | S3 : non observé ; C3 : « console ouverte » (C3R-0), preuve de **transport** seulement | à qualifier sur S3 après programmation |
| 2 | S3 en USB Serial/JTAG natif | identité, programmation, relecture | ❌ | aucune | **matériel** : un S3 relié par son USB Serial/JTAG natif ; la S3 disponible est câblée en pont UART, et USB Serial/JTAG et USB OTG ne sont pas interchangeables (à vérifier sur la carte, sans le supposer) |
| 2 | | réénumération ou disparition USB, redécouverte | ❌ | aucune | idem ; critère après réénumération non écrit |
| 2 | | critère OTG (disparition en OTG, redécouverte) | ❌ | aucune | **essai séparé** du Serial/JTAG : le port USB natif d'un S3 peut s'utiliser en Serial/JTAG ou en OTG ; une qualification de l'un ne vaut pas pour l'autre |
| 2 | | refus de EN/BOOT en natif | 🧪 | refus testés en simulation (moteur) | à confirmer sur matériel natif |
| 3 | C3 | famille, flash, partitions | ✅ | C, D (C3R-0/2, Q1) | |
| 3 | | particularités de reset | 🟡 | C, D (C3R-3 : bannière ROM après un reset) | réénumération USB après reset : aucun critère (INDETERMINATE assumé) |
| 4 | multi-images + manifeste réel | programmation multi-images, SHA des dumps | ❌ | Q1 = une seule région de 8 Kio, un motif | recette multi-images à écrire ; régions disjointes à approuver |
| 4 | | absence d'effacement global inféré | 🟡 | A–D : seule la région approuvée est écrite et restaurée ; refus de l'effacement global en simulation 🧪 | aucune comparaison de la flash hors région sur matériel |
| 5 | interruptions | débranchement pendant chaque phase de programmation | ❌ | E : débranchement pendant une **lecture** seulement, antérieur | **injection de fautes** : procédure de récupération préalable + autorisation distincte |
| 5 | | onglet fermé/rechargé | 🟡 | Q3 (reconnexion de la page après redémarrage) ; incident Q3 traité par PR #15 | fermeture pendant une écriture : non qualifiée |
| 5 | | reprise réseau et serveur, aucun rejeu, blocage avant boot | 🟡 | Q3 : redémarrage au **repos**, nouvel epoch, anciennes poignées rejetées ; journal durable en simulation 🧪 | redémarrage pendant une opération : non qualifié |
| 6 | timeouts, résultats et ACK tardifs ou perdus | timeout sans faux succès ; issue inconnue conservée | 🧪 | journal durable et matrice de redémarrage (avant/pendant/après résultat), `PendingOperation`, INDETERMINATE (tests `test_journal`, `test_reliability`) | aucune perte d'ACK ni de résultat provoquée sur matériel |
| 6 | | résultat ou ACK perdu, tardif | ❌ | 🧪 seulement | injection de faute déterministe (coupure de la réponse ou du serveur à une phase donnée) ; autorisation distincte |
| 6 | | pas de retour console automatique avant acceptation par le serveur | 🟡 | C3R-0/1/2 : le retour console est demandé explicitement et rapporté comme preuve de **transport** ; gardes en simulation 🧪 | non qualifié sur S3 ; non qualifié en cas d'ACK perdu |
| 7 | déclencheurs UART | armement `mark`, désarmement par le propriétaire, refus par un autre propriétaire | ✅ | A–D (Q2a–d : expiration naturelle, révocation, `trigger_not_owned`, autre identité) | armement sans stimulus : aucun déclenchement réel |
| 7 | | réarmement | 🧪 | tests moteur/agent (« rearming cancels a pending action ») | à qualifier sur matériel |
| 7 | | reset différé | ❌ | 🧪 | à qualifier : action différée déclenchée par un stimulus contrôlé sur UART externe |
| 7 | | verrouillage et concurrence avec d'autres commandes | 🟡 | 🧪 (agent) ; recette M5 concurrence de `qualification_v2` non rejouée sur `main` | à qualifier sur matériel |
| 8 | capture soutenue | débit UART soutenu | ❌ | 🧪 | **matériel** : source UART soutenue |
| 8 | | onglet en arrière-plan | ❌ | simulation (CI navigateur) | **opérateur** : page masquée longtemps ; libération de l'anti-veille non vérifiée |
| 8 | | client SSE externe, plusieurs Mo, reprise au curseur | ❌ | 🧪 (curseur REST/SSE) | client externe réel avec archivage côté projet |
| 8 | | détection explicite des pertes | 🧪 | tests de lacunes/anneau | aucune lacune provoquée sur matériel ; E : lecture de 10 Mio en échec à ~350 s, **cause non établie** |
| 9 | grants, révocations, réservations | grants et révocations depuis le navigateur réel | ✅ | A–D (Q1, Q2a, Q2b) | |
| 9 | | expiration des réservations | 🟡 | Q2c/Q2d : propriété (nouvelle réservation, autre identité) ; TTL non attendu jusqu'à expiration | recette M5 expiration non rejouée sur `main` |
| 9 | | **deux cartes de la même famille** | ❌ | une seule carte logique branchée à la fois ; S3 et C3 sont de familles différentes | **matériel** : deux S3 ou deux C3 simultanément ; sinon exigence **ouverte** |
| 10 | HTTP LAN et sécurité navigateur | accès réseau local, contenu mixte, redirections refusées, résultat opaque non vérifié | 🧪 | liste d'origines privées, refus de redirection, réponse opaque (tests du moteur et du transfert local) | **réseau/opérateur** : un petit serveur HTTP de test sur une autre machine du LAN et les navigateurs retenus |

### 3 bis. Sujets hors des dix exigences (non contractuels)

| Sujet | État | Preuve | Écart |
|---|---|---|---|
| Interface mobile/bureau, captures | 🟡 | Firefox 159 bureau : Web Serial utilisé en conditions réelles (S3 et C3) | pas de capture, pas de mobile ; Chromium absent du conteneur |
| Détection effective de `navigator.serial` | 🟡 | idem | à documenter par navigateur retenu |

## 3. Synthèse

| # | Verdict d'audit | Reste à faire |
|---|---|---|
| 1 | **Partielle** | capture ROM, reset EN, BOOT, retour console sur S3/UART |
| 2 | **Non démontrée** | S3 en USB Serial/JTAG natif à confirmer (matériel) |
| 3 | **Presque complète** | critère de réénumération USB après reset |
| 4 | **Partielle** | recette multi-images ; vérification hors région |
| 5 | **Non démontrée** | injection de fautes déterministes + procédure de récupération |
| 6 | **Non démontrée sur matériel** | ACK/résultat perdu ou tardif par faute déterministe ; retour console sur S3 |
| 7 | **Partielle** | réarmement, reset différé, concurrence avec stimulus contrôlé |
| 8 | **Non démontrée** | débit soutenu, onglet masqué, client SSE externe, perte provoquée |
| 9 | **Partielle** | expiration de réservation ; **deux cartes de la même famille** |
| 10 | **Non démontrée sur matériel** | cible HTTP sur le LAN, navigateurs retenus |

Aucune exigence n'est à ce stade déclarée validée par justification documentaire : une exigence qu'on ne peut pas vérifier avec le matériel disponible reste ouverte
(en particulier 2, la partie « deux cartes de la même famille » de 9, et 10 sans cible LAN).

## 4. Matériel et interventions nécessaires

| Besoin | Exigences | Détail |
|---|---|---|
| S3 en USB Serial/JTAG natif | 2 (et 1 pour le refus EN/BOOT en natif) | à confirmer sur la carte ; ne pas supposer que USB Serial/JTAG et USB OTG sont interchangeables ; sinon exigence 2 ouverte |
| Deux cartes de la même famille | 9 | deux S3 ou deux C3 connectées ensemble ; sinon cette partie de 9 reste ouverte |
| Opérateur navigateur | 5, 8, 10 | fermer/recharger la page, onglet masqué longtemps, navigateurs retenus |
| Serveur HTTP de test sur une autre machine du LAN | 10 | suffisant ; aucun équipement dédié à acheter |
| Client SSE externe | 8 | script projet archivant plusieurs Mo, reprise au curseur |
| Source UART soutenue | 7, 8 | stimulus contrôlé et débit soutenu |
| Injection de fautes | 5, 6 | coupure USB/serveur à une phase **déterministe** ; sauvegardes vérifiées, procédure de récupération validée **et relue avant les recettes destructives**, autorisation par essai |
| Programmation multi-images | 4 | plusieurs régions disjointes approuvées, manifeste réel, comparaison des SHA |

## 5. Propositions de recettes ciblées (non écrites à ce stade)

Une exécution concluante par scénario suffit, sauf intermittence identifiée (E : échec de lecture longue non expliqué à ~350 s ; redémarrages répétés non expliqués).
Ordre validé par la revue : **R1 (S3/UART), puis R4 (multi-images)**, sous réserve de cet audit corrigé. Pour **R5/R6 (fautes)**, la procédure de récupération est
préparée et examinée **avant** d'écrire toute recette destructive.

| Id | Exigence | Nature | Contenu | Autorisations |
|---|---|---|---|---|
| R1 | 1 | matériel, lecture/reset | S3/UART : ROM, reset EN, BOOT, retour console après chargeur ; critères écrits avant | reset ; geste opérateur éventuel |
| R2 | 2 | matériel | S3 natif : identité, écriture/relecture, réénumération, refus EN/BOOT | région + restauration ; carte à confirmer |
| R3 | 3 | critère | écrire (revue) un critère de réénumération USB après reset, sans le fixer pendant un run | revue |
| R4 | 4 | matériel | multi-images (2 à 3 régions) + manifeste + SHA ; hors-région inchangé | régions, restauration |
| R5 | 5 | matériel, fautes | coupure déterministe par phase (après sauvegarde, pendant effacement, pendant écriture, avant relecture) ; page fermée ; serveur arrêté | procédure de récupération relue ; une autorisation par essai |
| R6 | 6 | matériel, fautes | ACK ou résultat perdu/tardif ; pas de retour console avant acceptation | idem R5 |
| R7 | 7 | matériel | réarmement, reset différé, verrouillage, concurrence avec stimulus contrôlé | grants, reset |
| R8 | 8 | matériel + navigateur | débit soutenu, onglet masqué, perte détectée, plusieurs Mo au client SSE externe | opérateur |
| R9 | 9 | matériel | TTL de réservation ; deux cartes de la même famille | grants ; seconde carte |
| R10 | 10 | réseau | serveur HTTP LAN, contenu mixte, redirection refusée, résultat opaque non vérifié | cible LAN |

Les contrôles purement logiciels (politiques de sécurité, erreurs de protocole, invariants) restent en tests pytest/Node, sans matériel.

## 6. Limites de cet audit

- Les références logicielles (🧪) sont des constats par domaine d'après les fichiers de tests existants ; elles n'ont **pas** été relues cas par cas et doivent
  être confrontées par le réviseur avant de s'y appuyer.
- Les dix exigences ont été confrontées au texte de l'issue #2 par la revue de la PR #20 ; je n'ai pas pu relire l'issue en entier moi-même (résumé seulement).
- Aucun accès matériel : aucun résultat nouveau n'est rapporté ici.
