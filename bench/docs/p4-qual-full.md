# P4-QUAL-FULL (PC-11) — qualification matérielle complète ESP32-P4 ECO7 (flash 32 Mio)

**Statut : recette écrite et qualifiée en SIMULATION uniquement. NON exécutée sur matériel.** Un PASS simulé n'est jamais un PASS matériel. Aucun consentement n'est écrit dans ce dépôt : il vient du message explicite de l'opérateur (carte, scénario, SHA exact, effets).

Recette : `bench/recipes/qualification_campaign/test_pc_qual_full.py` ; support pur : `qual_full.py` ; section de session `qual_full` (`expected_assets_sha256` obligatoire, `segment_size` 1 Mio, `grant_seconds` 3600–14400 défaut 14400, `revoke_wait_s` 600).

## Périmètre
Stub **officiel** v1.2.2 uniquement, **115200 baud** à chaque opération (`uart_baud` dans chaque lecture et écriture ; l'identité du stub et la vitesse sont dans les preuves). Chaque opération est une session de loader (`return_console` vrai). Ni effacement de puce, ni `flash.erase`, ni eFuse, ni C6, ni GPIO54, ni variante expérimentale.

## Phases
- **A** — 6 lectures ciblées (début, table 0x8000, 0x800000, 0xFFF000, 0x1000000, dernier secteur) de 4096 o, **deux fois** chacune, égalité exigée, JEDEC 0x1940C8, condensat MD5 du stub vérifié par bloc.
- **B** — deux images complètes indépendantes BACKUP-A / BACKUP-B (33 554 432 o, segments 1 Mio), copie privée fsyncée (0600) avant suppression de l'artefact serveur, `verify_backup`, assemblage.
- **C** — tailles, couverture, SHA-256 par segment et global, comparaison octet à octet A/B, relecture depuis le disque, inventaire (comptes et offsets, jamais le contenu), criticité d'amorçage des secteurs d'écriture (premiers 64 Kio, partitions app/otadata de la table lue à 0x8000). Critique ⇒ BLOCKED avant toute écriture.
- **D** — secteurs D1 0x00800000, D2 0x01000000, D3 0x01FFF000 : initial == BACKUP-A, voisins lus avant/après. Séquence par secteur **pattern-A → blanc (0xFF) → pattern-B (complément) → restauration depuis BACKUP-A**, chaque écriture suivie d'une relecture indépendante. Le périphérique n'a pas de commande d'effacement de secteur : l'écriture 4 Kio efface exactement son secteur, et le stub saute l'effacement d'un secteur déjà vierge ; cet ordre garantit un effacement réel d'un secteur non vierge, lu ensuite à 0xFF. Vérification finale : secteurs et voisins égaux à l'état initial.

## Arrêt
Premier problème ⇒ arrêt, aucun rejeu, aucune restauration automatique (les fichiers BACKUP-A servent à une restauration explicite). Verdicts : PASS / FAIL / BLOCKED / INDETERMINATE par phase (`phase-verdicts.json`) et global. Sans grant local observé, ou si le grant ne couvre pas la campagne projetée à partir du premier segment : BLOCKED avant toute écriture.

## Grant
L'opérateur arme UN grant local au début (≥ `grant_seconds`−300 s observé), le révoque à la fin ; la recette n'en crée ni n'en révoque aucun. Grant encore actif après `revoke_wait_s` : incident opérateur, et `Campaign.close()` le signale.

## Durée estimée à 115200 baud
≈ 50–60 min par image de 32 Mio, ≈ 2–3 h au total.

## Contrôles logiciels
`bench/python/tests/test_pc_qual_full.py` (14 tests : motifs, session, campagne complète PASS carte inchangée, relecture corrompue, écriture en échec, sauvegardes différentes, 6 cas BLOCKED, sans grant) et `bench/tests/stub-variant.test.mjs` (`flash.write` avec `uart_baud`).

## Garde contre le rebouclage d'adresse à 16 Mio
Avant toute écriture, chaque secteur D est comparé au secteur qu'un rebouclage d'adresse atteindrait (`alias` : D2 → secteur 0, l'amorce). Contenu identique et alias critique ou non vierge : BLOCKED. Après la première écriture de chaque secteur, l'alias est relu et doit rester égal à BACKUP-A ; les alias sont aussi relus à la fin.
