# Recettes M40 complémentaires — protocole matériel et consentements (issue #40)

**Statut : écrites et qualifiées en SIMULATION uniquement. NON exécutées sur matériel.** Elles complètent `qualification-multi-board.md`
et les recettes M40-0 / M40-11a. Architecture inchangée : pytest → client Python → API REST → serveur → agent navigateur → ESP32 ; le
navigateur garde les ports Web Serial et la supervision, aucune commande de qualification n'est lancée depuis l'interface.

Règles communes : session privée hors Git (`session.m40.example.json` ne donne **aucun** consentement) ; aucune écriture, aucun
effacement, aucun grant, aucun changement du serveur ; arbre Git propre pour chaque exécution ; une seule exécution par essai, jamais
de rejeu après une erreur ou un résultat incertain ; un SHA de serveur différent n'est accepté que si le **code** serveur est identique
(ou déclaré compatible avec justification dans la session) ; preuves JUnit + JSON + SHA-256 sous `evidence_root` (0700).

| Recette | Consentement (session privée) | Option pytest | Cartes commandées | Resets |
|---|---|---|---|---|
| M40-12a | `M40-12A` | — | A et B : `mark` seulement | aucun |
| M40-11c/d | `M40-11C` | — | A : `flash.read` ≤ 4 Kio, `partitions`, `console` ; B observée (GET) | A : entrée bootloader + retour console |
| M40-4 passif | aucun | — | aucune (capture) | aucun |
| M40-4 `reset_both` | `M40-4-RESET` | `--qual-allow-reset` | A et B : `reset` | **A et B** (B : USB Serial/JTAG, réénumération possible) |

## M40-12a — Réservations simultanées
`mark` n'a aucun effet matériel : le moteur de l'agent émet seulement un événement `mark` (pas de loader, pas de reset, pas d'accès
flash), sans grant (catalogue `docs/api.md`). Déroulement : état initial de A et B → réservation de A puis de B par le même client →
un `mark` à libellé unique par carte → événements de chaque carte = exactement son libellé → libération de A (B reste réservée) →
second `mark` sur B (A n'en reçoit rien) → libération de B → aucune réservation, quarantaine, perte ni nouvelle génération.
**Les `seq` ne restent pas constants** : les marks et les événements d'opération sont attendus, comptés et vérifiés contigus.
PASS/FAIL/BLOCKED/INDETERMINATE : voir la docstring de `test_m40_complement.py`. Commande : 3 opérations exactement.

## M40-11c — Lecture à contenu connu (A seulement)
Section `m40.known` : `table_offset` (0x10000 d'après la table historique), trois marqueurs, `marker_timeout_s`. Critères : en-tête
d'image du bootloader (octet 0xE9, segments plausibles, identifiant de puce = S3) lu deux fois ; table de partitions décodée
indépendamment == vue de l'API ; premier 4 Kio de chaque partition applicative lu deux fois, au moins un en-tête valide, les autres
**entièrement effacés** ; un contenu tout à 0xFF n'est jamais une preuve d'adressage. Dumps (4 Kio) et JSON en 0600, privés, jamais publiés.

## M40-11d — Retour applicatif (même cycle loader que 11c)
Curseur UART pris **avant** `console` ; attendre dans l'ordre le marqueur ROM, celui du bootloader de second niveau et le marqueur
**applicatif déclaré** dans la session (sans valeur par défaut ; le gabarit contient un `REPLACE-…` qui BLOQUE la recette). Absent ou
hors d'ordre → FAIL ; rupture de continuité → INDETERMINATE. Ne prouve pas que l'application est correcte, seulement qu'elle a
imprimé sa ligne déclarée après le retour console.

## M40-4 — UART simultané
Section `m40.uart` : fenêtre, seuils (octets, recouvrement, alternances), marqueurs **distinctifs et disjoints** de chaque carte, `stimulus`.
Deux sources indépendantes : chaque flux contient ses propres marqueurs et aucun de l'autre ; fenêtres d'arrivée qui se recouvrent ;
alternance de l'ordre d'arrivée ; `seq` strictement croissant et `source_seq` contigu par carte ; aucune perte flaggée ; générations,
agents, réservations inchangés. `stimulus: none` : capture passive des logs que les firmwares impriment déjà (BLOCKED si une carte
est muette). `stimulus: reset_both` : un `reset` par carte, simultané — **jamais implicite** : consentement `M40-4-RESET` ET
`--qual-allow-reset` ; un changement de génération après le stimulus (réénumération USB) est INDETERMINATE.

## Protocole matériel proposé (ordre de priorité)
1. **M40-12a** (aucun reset). Consentement à demander : commandes `mark` sur A **et B**.
2. **M40-11c/d** (un cycle loader sur A). Prérequis : marqueurs déclarés (ROM `ESP-ROM:esp32s3`, 2nd stage `boot:`, ligne applicative
   de A), table à 0x10000. Consentement : entrée bootloader, lectures ≤ 4 Kio, retour console de A.
3. **M40-4 `reset_both`** seulement après qualification du comportement du reset de B (réénumération). Consentement spécifique B.

Commande type (le token est injecté dans l'environnement du processus, jamais en ligne de commande) :
```
cd bench && python -m pytest recipes/qualification_campaign/test_m40_complement.py::<test> --bench --bench-url http://127.0.0.1:8080 \
  --bench-device esp32-local --qual-session <privé>/session.m40.json --bench-results <privé>/runs \
  --junitxml=<privé>/junit-<id>-vN.xml -rA -p no:cacheprovider      # (+ --qual-allow-reset pour M40-4 reset_both)
```
Critères d'arrêt (aucune relance) : serveur incompatible, génération incohérente, quarantaine, opération préexistante, réservation
impossible, rejeu possible, commande non prévue, observation incomplète, consentement manquant.

## Limites
Vue API seulement : ni la MAC, ni l'objet `SerialPort`, ni la fermeture du port. Les horodatages UART sont ceux du serveur.
Simulation : `SIM_FLASH_PRELOAD`, `SIM_CONSOLE_BOOT`, `SIM_UART_TAG`, `mark` émettent des événements comme l'agent navigateur.
