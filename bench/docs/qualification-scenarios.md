# Scénarios de qualification matérielle du banc (à relire avant exécution)

**Statut : essais matériels non exécutés.** Cette PR propose des recettes versionnées ; aucune n'a
été jouée sur une carte depuis ce commit. Les contrôles automatiques sont des *simulations*
(`python/tests/test_qualification_recipes.py`) : un PASS simulé n'est pas un PASS matériel.
Méthode générale : [qualification-workflow.md](qualification-workflow.md) (PR #13).

Emplacement : `bench/recipes/qualification_campaign/` (recettes génériques du banc ; aucun scénario
métier ni OTA). Ces recettes complètent, sans les remplacer, `recipes/qualification_v2` (lecture seule).

## Provenance de Q1 et PASS historique

Le Q1 S3/UART rapporté PASS (1 cycle, 106,7 s) a été exécuté par la **recette locale `s3-uart-q1-v2`**
(SHA-256 `97f5d68c3511c261…`), après une v1 en erreur d'import (avant tout contact matériel). Ce PASS reste
attribué à ce fichier et à ses preuves privées ; **il n'est pas le PASS de `test_q1_campaign_cycle.py`**.
Les originaux sont conservés hors Git (voir « Emplacement privé »).

Différences de la version Git par rapport à la v2 locale : paramètres lus dans une session privée au lieu
de constantes ; région vérifiée disjointe de la table et des partitions lues à l'offset configuré ;
refus côté client contrôlés avant toute soumission (consentement par opération, hors manifeste) ;
verdicts classifiés (`recorded`) avec clôture toujours tentée sans masquer l'erreur initiale ;
attentes opérateur bornées ; état de restauration écrit *avant* la mutation ; aucune région par défaut.

## Paramètres (session privée, hors Git)

Modèle : `session.example.json` (inutilisable tel quel : `flash_size` invalide, aucune région, aucun consentement).
La copie réelle contient les consentements et reste sur un stockage persistant privé.

| Champ | Rôle |
|---|---|
| `board_id`, `chip`, `flash_size`, `transport` | Doivent correspondre à la carte observée (`CampaignManifest.check_device`) ; le transport est celui *rapporté par l'agent* (inféré du VID/PID) |
| `partition_table_offset` | Offset de la table (donnée de session, pas une constante : `0x8000` n'est pas supposé) |
| `approved_regions` | Secteurs de 4 Kio explicitement approuvés. Vide par défaut. `[0x400000, 0x2000]` n'est qu'une donnée de la session S3 rapportée |
| `consent.scenarios` | Scénarios destructifs consentis (`Q1`) ; `--bench-allow-destructive` n'est **pas** un consentement |
| `evidence_root` | Racine absolue privée ; un sous-dossier neuf par run (`Q1-<horodatage>`) |
| `q2.*`, `q3.*` | Motif inerte, durée du déclencheur ; pilotage du lanceur (désactivé par défaut) |

## Matrice des scénarios

Verdicts : PASS / FAIL / SKIP / BLOCKED / INDETERMINATE. Un prérequis absent (carte, interface, geste,
second token, autorisation de redémarrage) donne **BLOCKED** (pytest `skip` préfixé `BLOCKED:`), jamais PASS.
Un `PendingOperation` ou une perte de continuité donne INDETERMINATE ; rien n'est rejoué.

### Q1 — Cycle complet `Campaign` (`test_q1_campaign_cycle.py`)

- **Objectif** : prouver sauvegarde → upload → programmation → relecture indépendante → restauration → comparaison → clôture avec les vraies abstractions.
- **Prérequis** : session avec une région approuvée et `Q1` consenti ; `--bench --bench-allow-destructive` ; scopes read/control/program ; carte connectée, non réservée, hors quarantaine ; opérateur présent pour grant et révocation.
- **Effets matériels** : lectures (entrée loader possible, reset possible), écriture/effacement de la seule région approuvée, relectures.
- **Assertions** : refus client sans consentement et hors manifeste, `submissions` inchangé ; région disjointe de la table et des partitions ; deux sauvegardes de SHA-256 identiques, motif ≠ référence ; programmation + relecture exacte ; `restore` puis `compare` égal ; grant révoqué ; `Campaign.close()` réussi.
- **Délais** : grant minimal `2×(2·T+90)+60` s (T = `operation_timeout_s`, 120 s → 720 s) ; réservation 1800 s renouvelée explicitement après le grant ; attentes opérateur ≤ `operator_timeout_s`.
- **Échecs/récupération** : aucune restauration ni nouvelle tentative automatique. Après échec, l'état attendu est consigné (« motif possiblement présent, restauration requise depuis ref-a ») ; la référence est conservée ; la reprise est une décision de l'opérateur.
- **Limites** : un cycle ≠ endurance ; une seule région ; la stabilité de la région n'est établie que par deux lectures.

### Q2 — Désarmement après perte du grant (`test_q2_trigger_disarm.py`)

Déclencheur `mark` à motif inerte ; **aucun stimulus** : on ne conclut rien sur le déclenchement (scénario distinct à écrire).
Chaque cas demande son propre grant local ; ce client n'en envoie jamais.

| Cas | Geste opérateur | Assertions |
|---|---|---|
| Q2a expiration naturelle | grant le plus court (60 s), puis rien | grant observé à 0 par expiration ; réarmement → `403 grant_required` ; désarmement `{"action":"disarm"}` par la réservation active réussi ; grant toujours 0 |
| Q2b révocation locale | grant ≥ 300 s, révoquer sur demande | mêmes assertions ; la révocation est distinguée de l'expiration (grant > 10 s restant avant le passage à 0), sinon BLOCKED |
| Q2c nouvelle réservation | grant ≥ 90 s, révocation finale | grant **valide** ; désarmement par une nouvelle réservation du même propriétaire → `403 trigger_not_owned` (donc pas un refus de grant) |
| Q2d autre identité | idem Q2c + `BENCH_CLIENT_TOKEN_B` | désarmement par une identité distincte → `403 trigger_not_owned` ; BLOCKED sans second token |

Réservation 600 s, déclencheur ≤ 120 s ; si le déclencheur expire avant le contrôle, BLOCKED (gestes trop lents).

### Q3 — Redémarrage coordonné au repos (`test_q3_coordinated_restart.py`)

- **Prérequis** : `--qual-allow-restart`, `q3.allow_restart`, `q3.launcher_pid_file`, `q3.state_dir` ; carte au repos (aucune opération, grant 0).
- **Identification** : le fichier PID n'est qu'un indice. Le lanceur doit exécuter `run_server.py`, avoir exactement un enfant `bench_server.py`, et cet enfant doit posséder le socket d'écoute du port de `--bench-url` (lecture de `/proc`). L'identité est revérifiée juste avant `SIGHUP`.
- **Assertions** : ancien enfant terminé *avant* tout remplaçant ; remplaçant sous le même lanceur ; `/health` ok, `sqlite-full`, **epoch différent** ; mêmes inodes de base et de verrou, taille non décroissante ; ancien `Board` rejeté (`ContinuityError`) ; agent reconnecté, grant 0, aucune réservation, pas de quarantaine ; nouvelle fenêtre (`interrupt_window` puis `open_window`).
- **Échecs** : délai d'arrêt dépassé → INDETERMINATE (inspection seule : ni verrou, ni base supprimés, aucun signal supplémentaire). L'agent peut exiger un rechargement de la page (geste annoncé, attente bornée).
- **Hors périmètre** : coupure pendant une écriture (lot distinct avec autorisation et récupération dédiées).

### Extension C3 (non exécutable tant que la carte C3 n'est pas décrite)

Aucune valeur C3 n'est inventée. Il suffit d'une **nouvelle session privée** (`chip: esp32c3`, capacité, transport, offset de table, région approuvée et consentement propres) ; Q1/Q2/Q3 sont paramétrés par cette session. Interface ou carte absente : BLOCKED/SKIP justifié. Un verdict par couple carte/transport.

## Gestes opérateur (récapitulatif)

Q1 : 1 grant (1800 s) + 1 révocation. Q2 : 4 grants courts à moyens + révocations (Q2b, Q2c, Q2d). Q3 : éventuel rechargement de la page. Aucun n'est un consentement à des gestes distants.

## Commandes

Collecte et contrôles sans matériel (CI incluse) — aucune carte, aucun grant, aucune connexion :

```sh
cd bench
python -m pytest python/tests recipes -q          # scénarios matériels : SKIP sans --bench
python -m pytest recipes/qualification_campaign --collect-only -q
```

Rejeu matériel — **uniquement** après revue du commit, accord de Lionel pour la campagne et consentements
applicables ; le token est lu par le processus, jamais passé en argument :

```sh
export BENCH_URL=… BENCH_DEVICE=… BENCH_QUAL_SESSION=/chemin/prive/session.json   # BENCH_CLIENT_TOKEN dans l'environnement du processus
python -m pytest recipes/qualification_campaign/test_q1_campaign_cycle.py \
  --bench --bench-allow-destructive --bench-results /chemin/prive/preuves -v -s
# Q2 : test_q2_trigger_disarm.py --bench          # Q3 : test_q3_coordinated_restart.py --bench --qual-allow-restart
```

Chaque run écrit dans un dossier neuf ; une recette corrigée change de version/commit et repasse en revue.

## Résultats logiciels de cette PR (simulation)

- `python -m pytest python/tests recipes -q` : 99 réussis, 15 ignorés (6 scénarios matériels + 9 existants).
- Collecte sans opt-in : 6 ignorés, **aucune connexion** à l'URL configurée (écouteur espion) ; opt-in sans session : BLOCKED, pas PASS.
- Identification du lanceur, SIGHUP, fin de l'ancien enfant, remplaçant, nouvel epoch, mêmes inodes base/verrou : vérifiés contre un **vrai lanceur isolé** (répertoire d'état jetable, port libre).
- Q1 rejoué contre le simulateur RAM jusqu'à la comparaison finale ; finit BLOCKED comme attendu (le simulateur ne permet pas la révocation du grant).
- **Non simulé** : flux opérateur de Q2 (le simulateur impose un grant initial, incompatible avec `Campaign.start` non destructif) — ses codes de refus (`grant_required`, `trigger_not_owned`) sont couverts par les tests du broker ; reconnexion réelle de l'agent (Q3) ; tout le matériel.

## Points à arbitrer par la revue

1. Q2c/Q2d : `first.close()` libère la réservation qui a armé le déclencheur ; l'effet exact sur le déclencheur armé (désarmement implicite ou expiration à 30 s) n'est pas affirmé, seul le refus `trigger_not_owned` l'est.
2. Q3 : assertion « pas de quarantaine après redémarrage au repos » et « grant 0 » — à confirmer comme contrat.
3. Q1 : une seule région ; plusieurs régions disjointes sont-elles requises ?
4. Q1 après échec mi-parcours : la politique « aucune restauration automatique » est choisie ; la valider.
5. Dépendance à la PR #13 (méthode) : ce document la référence mais ne la modifie pas.

## Emplacement privé (hors Git)

Les recettes locales historiques et leurs preuves (Q0, erreur de collecte v1, Q1 v2, `ref-a`/`ref-b`) sont dans
`/root/bench-recipes` et `/root/bench-evidence` (overlay, **non persistants**) et copiés avec empreintes (47 fichiers) dans
`/root/.claude/bench-private/2026-11-06-preserved/` (volume persistant `/dev/sdd`). Rien de cela n'est dans Git.
