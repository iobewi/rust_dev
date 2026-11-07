# Issue #2 : finalisation de la qualification du banc ESP32-S3/C3

**Statut : préparation, aucune opération matérielle effectuée par cette PR.** Tout ce qui suit est du code, des critères et des procédures ;
les recettes ajoutées sont testées **en simulation seulement** (un PASS simulé n'est pas un PASS matériel). Chaque opération matérielle reste soumise
à une autorisation explicite, donnée par Lionel pour l'opération concernée.

## 1. État de départ (à `f4be8e1`)

| Source | Contenu |
|---|---|
| PR #3, #14 et campagnes S3/C3 historiques | S3 pont UART : identité, partitions, contrôle, reset EN/IO0, lectures 6 régions et 10 Mio, débranchement pendant une **lecture** (SHA historiques `d548dab`…`dafe4b8`, antérieurs aux corrections) ; Q1, Q2a–d, Q3 ; C3 natif : C3R-0…3, Q1, Q2a–d, Q3 |
| PR #19 | comparateur v1 ; 3 passages S3 + 1 passage C3 sous `69f4183` |
| PR #20–#24 | audit, S3R-1/2/3, R4, critères v2 (`57fa046e…a43e`), rétention de `compare()` |
| **Lot 1 S3 du 7 nov. 2026** (commentaire de l'issue #2) | sur `f4be8e1`, S3 N16 / `external_uart`, critères v2 épinglés : **S3R-3, S3R-1, R4 : PASS vérifiés** (3/3, `--strict` code 0) |

Preuves privées hors Git : `/root/.claude/bench-private/campaigns/s3-lot1-f4be8e1/` (non revérifiées par un tiers).

## 2. Décisions de qualification par exigence

Légende : **PASS** démontrée selon les critères retenus · **PASS WITH LIMITATIONS** démontrée avec une restriction documentée · **BLOCKED** impossible
à qualifier avec ce qui est disponible, avec la décision nécessaire · **FAIL** non conforme. « Aujourd'hui » = avant la campagne de ce document.

| # | Exigence | Aujourd'hui | Ce qui la fait passer | Limite acceptée | Dépendance |
|---|---|---|---|---|---|
| 1 | S3 + pont UART | **PASS WITH LIMITATIONS** (connexion, ROM, reset EN, retour console : S3R-1/3, lot 1) ; maintien BOOT : non démontré | **S3R-2** prête (marqueur `DOWNLOAD(USB/UART0)`, `hold_ms` 500) | retour console = preuve de transport ; câblage du pont supposé (non mesuré) ; un démarrage applicatif n'est jamais prouvé | S3 DevKitC-1 + grant local |
| 2 | S3 USB natif | **BLOCKED** | **NUSB-0/1/2** prêtes (aussi valables pour un C3) | USB OTG **non couvert** (mode distinct, exclu) | un S3 relié par son USB Serial/JTAG natif (Waveshare S3 Zero, à confirmer sur la carte) |
| 3 | C3 | **PASS WITH LIMITATIONS** (C3R-0…3, Q1–Q3, historiques) | NUSB-2 sur le C3 si l'on veut caractériser la réénumération après reset | pas de critère de réénumération avant NUSB-2 ; PASS historiques sous `0e1f890` et `69f4183` | C3 (déjà disponible) |
| 4 | Multi-images | **PASS WITH LIMITATIONS** (R4, lot 1) | — (ne pas rejouer) | une exécution, deux régions ; fenêtres de contrôle partielles ; pas d'atomicité ; aucune coupure | — |
| 5 | Interruptions, rechargement, redémarrage | **PARTIEL** (débranchement en lecture historique ; Q3 au repos) | **R5-RELOAD / R5-UNPLUG / R5-RESTART** prêtes (lecture seule) | **coupure pendant une écriture : exclue** (autorisation, sauvegardes, procédure de récupération à part) | opérateur (onglet, câble), lanceur serveur |
| 6 | Timeouts, ACK/résultats perdus | **PASS WITH LIMITATIONS proposé** après R5 : tests logiciels déterministes + faute naturelle (le résultat ne peut plus être livré) | R5-* | **aucune injection déterministe de perte d'ACK** n'est construite (disproportionné, décision ci-dessous) | — |
| 7 | Déclencheurs UART | **PARTIEL** (Q2 : armement/désarmement/propriété) | **T7** prête (tir unique sur bannière ROM, reset différé, réarmement annule l'action) | verrouillage et concurrence du reset différé : tests moteur seulement | grant local, reset (consentement) |
| 8 | Capture soutenue, SSE, pertes | **BLOCKED** | **U8** prête (client SSE externe, reprise au curseur, pertes recomptées) | l'état « onglet en arrière-plan » est un geste annoncé, pas une condition vérifiée | **une source UART soutenue** (firmware qui journalise en continu, par exemple celui de `rust-ota`) |
| 9 | Grants, expiration, deux cartes | grants/révocations/propriété : **PASS** (Q1, Q2) ; expiration : non démontrée ; deux cartes : **BLOCKED** | **G9** (expiration, sans matériel supplémentaire) ; **M2** (deux cartes) prêtes | M2 jamais simulée en campagne | deux cartes de la même famille connectées ensemble (S3 + S3 Zero) |
| 10 | HTTP LAN et navigateur | **BLOCKED** (politiques testées en logiciel) | **H10** prête + `tools/lan_http_target.py` | ne qualifie que le contrat réel (une origine listée, réponse opaque, pas de redirection) ; aucune origine ni réseau supplémentaire ouvert | une machine du LAN pour la cible ; le navigateur retenu |

Aucune exigence n'est transformée en PASS par absence de test. Les `BLOCKED` ci-dessus ont chacun une dépendance physique isolée ; le reste du travail ne
les attend pas.

## 3. Décisions de conception (compromis assumés)

- **Pas d'infrastructure générale d'injection de fautes.** Les exigences 5 et 6 sont couvertes par des fautes **naturelles en lecture seule** (rechargement
  de l'onglet, câble, signal au lanceur serveur) : l'agent ne peut plus délivrer son résultat, ce qui est la forme réelle d'un résultat ou d'un ACK perdu. Une
  perte déterministe d'ACK demanderait une instrumentation côté agent (désactivée par défaut, preuve persistante anti-rejeu, procédure de restauration
  validée) : disproportionné pour un outil de développement ; **refus explicite**, à rouvrir seulement si une campagne OTA révèle une lacune.
- **Aucune coupure pendant une écriture** : exclue de cette PR et de la campagne. Elle exige sauvegardes vérifiées, chemin de récupération local
  indépendant et autorisation par essai. Le commentaire d'issue le dira.
- **USB OTG exclu** : `NUSB-*` n'accepte qu'une carte dont la **déclaration et l'inférence de l'agent** sont toutes deux `usb_serial_jtag` ; OTG, pont UART et
  inférence inconnue sont BLOCKED, jamais PASS ni FAIL.
- **Critères v3** (`tools/qualification_criteria_v3.json`) : fichier de données qui **étend** la v2 sans la modifier (v1 et v2 inchangées, SHA testés). Le
  comparateur reçoit des **contrôles de données brutes** (fichiers écrits par le client et les recettes, recoupés avec les propres enregistrements du serveur) ;
  ce n'est ni un troisième comparateur ni un nouveau format de preuve.
- **Réutilisé tel quel** : `Campaign`, `Orchestrator`, journal, `ReadbackRetention`, `boot.download`, harnais des recettes, simulateur.

## 4. Ce que cette PR ajoute

Recettes (`bench/recipes/qualification_campaign/`) : `test_native_usb.py` (NUSB-0/1/2), `test_triggers.py` (T7), `test_interruption.py` (R5-RELOAD/UNPLUG/RESTART),
`test_reservation_expiry.py` (G9), `test_two_boards.py` (M2), `test_lan_http.py` (H10), `test_uart_capture.py` (U8). Outils : `tools/sse_capture.py` (client SSE
externe), `tools/lan_http_target.py` (cible LAN de test). Client : `BenchClient.event_stream` ; `Board` enregistre les arguments soumis, les curseurs et les
correspondances UART (depuis la PR #23). Session : sections `faults`, `second_board`, `lan_http`, `capture` et consentements `NUSB-READ`, `NUSB-RESET`, `T7`,
`R5`, `H10`, `U8`, `M2` (modèle `session.example.json`). Simulateur : card native, réénumération, déclencheur, source UART soutenue, interruption pendant
une lecture, seconde carte, agent HTTP (modèle du contrat de la page, **pas** une preuve de la page).

Une conséquence découverte en testant U8 : un flux soutenu dépasse l'anneau d'événements du serveur entre l'ouverture et la fermeture de la fenêtre de preuve de la
campagne, et `Campaign.close()` refuse une fenêtre avec événements perdus. U8 **ferme donc sa fenêtre explicitement** avant la capture ; le témoin est le client externe.

## 5. Campagne matérielle finale (une seule, trois lots) : autorisations à décider

Avant tout : redémarrer le serveur réel sur le SHA de cette PR **après fusion** (le dossier servi et le processus doivent avoir le même build : l'écart a déjà
provoqué un « Refus du protocole » le 7 nov.), recalculer et **épingler** le SHA-256 des critères v3 (`sha256sum bench/tools/qualification_criteria_v3.json`,
puis `--criteria-sha256`), session privée par lot, consentements écrits par Lionel pour ce lot. Arrêt immédiat à tout résultat non PASS, opération non
résolue, quarantaine inattendue, epoch ou génération inattendus ; aucun rejeu ; une exécution conclusive par scénario.

| Lot | Scénarios | Carte / matériel | Autorisations demandées (chacune explicite) | Critère d'arrêt propre au lot |
|---|---|---|---|---|
| **A — transports et démarrage** | S3R-2 ; NUSB-0/1/2 (S3 natif ; C3 optionnel pour la réénumération) | S3 DevKitC-1 puis S3 Zero (ou C3) | grant local court (S3R-2), reset (NUSB-2), entrée en chargeur (lectures), changement de carte et resélection du port | S3R-2 laisse la carte en mode téléchargement : retour par S3R-1 ou EN avant de continuer |
| **B — résilience et contrôle** | G9 ; T7 ; R5-RELOAD, R5-UNPLUG, R5-RESTART | S3 DevKitC-1 | T7 : grant 300 s + resets ; R5 : lecture longue interrompue (onglet, câble, signal au lanceur identifié), récupération locale par l'opérateur | R5 : opération indéterminée = carte bloquée jusqu'à récupération locale ; jamais de rejeu |
| **C — interfaces et exploitation** | U8 ; H10 ; M2 | source UART soutenue ; cible LAN ; deuxième carte | U8 : aucun grant ; H10 : grant 300 s, origine listée dans la page ; M2 : deux onglets, deux cartes | M2/H10/U8 : BLOCKED si la dépendance physique manque, pas FAIL |

Aucune région flash n'est écrite dans ces lots. R4 et les régions `0x400000` / `0x410000` ne sont **pas** réautorisées.

## 6. Conditions de clôture de l'issue #2

L'issue peut être clôturée quand, pour chacune des dix exigences, la décision du tableau §2 est **PASS** ou **PASS WITH LIMITATIONS** (limites écrites dans le
commentaire de clôture) **ou** quand Lionel accepte explicitement qu'une exigence reste **BLOCKED** pour une dépendance physique nommée (avec la raison et la décision).
Elle ne l'est pas tant qu'une exigence est `FAIL`. Sans exécution des lots A à C, les exigences 2, 8, 9 (deux cartes) et 10 restent **BLOCKED**.

## 7. Proposition de mise à jour de l'issue #2 (non publiée)

> **Finalisation (PR « issue 2 final qualification »)** : les recettes NUSB-0/1/2, T7, R5-*, G9, M2, H10 et U8, les critères v3 et leurs tests simulés sont prêts.
> Aucune n'a été exécutée sur matériel. Décisions de conception : pas d'injection déterministe de perte d'ACK, pas de coupure pendant une écriture, USB OTG exclu.
> Tableau des dix exigences et plan de la campagne en trois lots : `bench/docs/issue-2-final-qualification.md`. Dépendances physiques ouvertes : S3 en USB natif
> (exigence 2), source UART soutenue (8), deuxième carte de la même famille (9), cible HTTP sur le LAN (10).
