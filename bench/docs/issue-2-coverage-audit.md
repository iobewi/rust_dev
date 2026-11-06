# Audit de couverture de l'issue #2 : matrice des écarts

**Statut : audit, brouillon pour revue. Aucun accès matériel, aucun grant, aucune écriture flash, aucun redémarrage serveur.**
Base : `main` au commit `69f4183`. Les dix exigences sont celles de la liste « Recette matérielle restant à réaliser » de
[coverage.md](coverage.md) (reprise par l'issue #2, « Qualifier le banc API v1 sur ESP32-S3 et ESP32-C3 réels »). Le texte exact de l'issue
n'a pu être relu que par un résumé : la reformulation de chaque exigence ci-dessous suit `coverage.md`, à confronter au texte de l'issue par le réviseur.

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

| # | Exigence (coverage.md) | Sous-exigence | État | Preuve existante | Écart / limite |
|---|---|---|---|---|---|
| 1 | S3 + pont UART | ouverture du port, identification, partitions | ✅ | A, B (Q1 : table de partitions, lectures) ; E | |
| 1 | | capture de la ROM au démarrage | ❌ | aucune sur S3 (la bannière `ESP-ROM:` est observée sur C3, réf. D) | à qualifier sur S3 |
| 1 | | reset EN | ❌ | primitives testées en simulation 🧪 ; E (historique, antérieur) | reset EN S3 jamais rejoué sur `main` |
| 1 | | maintien BOOT | ❌ | 🧪 ; E (IO0, antérieur) | idem |
| 1 | | retour console après esptool | 🟡 | S3 : non observé ; C3 : « console ouverte » (C3R-0), preuve de **transport** seulement | à qualifier sur S3 après programmation |
| 2 | S3 + USB Serial/JTAG natif | identification, programmation, relecture | ❌ | aucune | **matériel** : un S3 relié par son port USB natif (la S3 disponible est câblée en pont UART) |
| 2 | | disparition en OTG et redécouverte | ❌ | aucune | idem ; critère après réénumération non écrit |
| 2 | | refus de EN/BOOT en natif | 🧪 | refus testés en simulation (moteur) | à confirmer sur matériel natif |
| 3 | C3 | famille, flash, partitions | ✅ | C, D (C3R-0/2, Q1) | |
| 3 | | particularités de reset | 🟡 | C, D (C3R-3 : bannière ROM après un reset) | réénumération USB après reset : aucun critère (INDETERMINATE assumé) |
| 4 | multi-images + manifeste réel | programmation multi-images, SHA des dumps | ❌ | Q1 = une seule région de 8 Kio, un motif | recette multi-images à écrire ; régions disjointes à approuver |
| 4 | | absence d'effacement global inféré | 🟡 | A–D : seule la région approuvée est écrite et restaurée ; refus de l'effacement global en simulation 🧪 | aucune comparaison de la flash hors région sur matériel |
| 5 | interruptions | débranchement pendant chaque phase de programmation | ❌ | E : débranchement pendant une **lecture** seulement, antérieur | **injection de fautes** : procédure de récupération préalable + autorisation distincte |
| 5 | | onglet fermé/rechargé | 🟡 | Q3 (reconnexion de la page après redémarrage) ; incident Q3 traité par PR #15 | fermeture pendant une écriture : non qualifiée |
| 5 | | reprise réseau et serveur, aucun rejeu, blocage avant boot | 🟡 | Q3 : redémarrage au **repos**, nouvel epoch, anciennes poignées rejetées ; journal durable en simulation 🧪 | redémarrage pendant une opération : non qualifié |
| 6 | longue durée / débit UART | arrière-plan long, débit soutenu | ❌ | simulation 🧪 | **matériel + navigateur** (onglet masqué) |
| 6 | | détection des lacunes ; pas de faux succès sur timeout | 🧪 | tests de pertes/anneau et d'issue inconnue (INDETERMINATE) | aucune lacune provoquée sur matériel ; E : lecture de 10 Mio en échec à ~350 s, **cause non établie** |
| 7 | capture de plusieurs Mo par client SSE externe | archivage côté projet, reprise au curseur | ❌ | 🧪 (curseur REST/SSE) | à qualifier avec un client externe réel |
| 8 | autorisations | grants et révocations depuis le navigateur réel | ✅ | A–D (Q1, Q2a révocation/expiration naturelle, Q2b révocation locale) | |
| 8 | | expiration des réservations | 🟡 | Q2c/Q2d : nouvelle réservation / autre identité (propriété) ; TTL de réservation non attendu jusqu'à expiration | recette M5-expiration (`qualification_v2`) non rejouée sur `main` |
| 8 | | déclencheurs pendant une opération longue | ❌ | Q2 : armement `mark` seul, sans stimulus ni opération longue | à qualifier avec un stimulus contrôlé |
| 9 | HTTP LAN | accès local, contenu mixte, redirections refusées, résultat opaque | 🧪 | liste d'origines privées, refus de redirection et réponse opaque testés en simulation | **matériel/réseau** : un appareil HTTP sur le LAN et les navigateurs retenus ; opérateur |
| 10 | UI mobile/bureau, détection Web Serial | `navigator.serial` effectif | 🟡 | Firefox 159 desktop : Web Serial utilisé en conditions réelles (S3 et C3) ; pas de capture | rendu mobile et captures : non faits ; Chromium absent du conteneur |

## 3. Synthèse

| # | Verdict d'audit | Reste à faire |
|---|---|---|
| 1 | **Partielle** | capture ROM, reset EN, BOOT, retour console sur S3/UART |
| 2 | **Non démontrée** | matériel S3 en USB natif manquant (à confirmer par l'opérateur) |
| 3 | **Presque complète** | décider du critère de réénumération USB après reset |
| 4 | **Partielle** | recette multi-images ; vérification hors région |
| 5 | **Non démontrée** | injection de fautes déterministes + procédure de récupération |
| 6 | **Non démontrée** | débit soutenu, onglet masqué, lacune provoquée |
| 7 | **Non démontrée** | client SSE externe, plusieurs Mo, reprise au curseur |
| 8 | **Partielle** | expiration de réservation ; déclencheur pendant opération longue |
| 9 | **Non démontrée** | cible LAN réelle + navigateurs |
| 10 | **Partielle** | captures desktop/mobile ; verdict sur `navigator.serial` |

Aucune exigence n'est à ce stade déclarée validée par justification documentaire : une exigence qu'on ne peut pas vérifier avec le matériel disponible reste ouverte
(en particulier 2, et 9 sans cible LAN).

## 4. Matériel et interventions nécessaires

| Besoin | Exigences | Détail |
|---|---|---|
| S3 en USB natif (ou carte équivalente) | 2, 1 (refus EN/BOOT) | à confirmer : sinon exigence 2 ouverte |
| Opérateur navigateur | 5, 6, 8, 10 | fermer/recharger la page, onglet masqué, captures desktop et mobile |
| Cible HTTP sur le LAN | 9 | un appareil ou serveur de test, navigateurs retenus |
| Client SSE externe | 7 | script projet archivant plusieurs Mo, reprise au curseur |
| Injection de fautes | 5 | débranchement ou arrêt du serveur à une phase **déterministe** d'une écriture ; sauvegardes vérifiées, procédure de récupération validée, autorisation par essai |
| Programmation multi-images | 4 | plusieurs régions disjointes approuvées, manifeste réel, comparaison des SHA |

## 5. Propositions de recettes ciblées (non écrites à ce stade)

Une exécution concluante par scénario suffit, sauf intermittence identifiée (E : échec de lecture longue non expliqué à ~350 s ; redémarrages répétés non expliqués).

| Id | Exigence | Nature | Contenu | Autorisations |
|---|---|---|---|---|
| R1-S3 | 1 | matériel, lecture/reset | ROM, reset EN, BOOT, console après chargeur ; critères écrits avant | reset ; geste opérateur éventuel |
| R2 | 2 | matériel | identité, écriture/relecture, réénumération, refus EN/BOOT en natif | région + restauration |
| R3b | 3 | critère | décider d'un critère de réénumération USB, sans le fixer pendant un run | revue |
| R4 | 4 | matériel | multi-images (2 à 3 régions) + manifeste + SHA ; hors-région inchangé | régions, restauration |
| R5 | 5 | matériel, fautes | coupure déterministe par phase (après sauvegarde, pendant effacement, pendant écriture, avant relecture) avec récupération | une autorisation par essai |
| R6/R7 | 6, 7 | matériel + navigateur | débit soutenu, onglet masqué, lacune détectée, plusieurs Mo au client SSE externe | opérateur |
| R8 | 8 | matériel | TTL de réservation, déclencheur avec stimulus pendant lecture longue | grants |
| R9 | 9 | réseau | cible LAN, contenu mixte, redirection refusée, résultat opaque non vérifié | cible LAN |
| R10 | 10 | opérateur | captures desktop/mobile, `navigator.serial` | opérateur |

Les contrôles purement logiciels (politiques de sécurité, erreurs de protocole, invariants) restent en tests pytest/Node, sans matériel.

## 6. Limites de cet audit

- Les références logicielles (🧪) sont des constats par domaine d'après les fichiers de tests existants ; elles n'ont **pas** été relues cas par cas et doivent
  être confrontées par le réviseur avant de s'y appuyer.
- Le texte exact de l'issue #2 n'a été lu que par résumé ; les dix exigences suivent `coverage.md`.
- Aucun accès matériel : aucun résultat nouveau n'est rapporté ici.
