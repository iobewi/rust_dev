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
| `q2.*`, `q3.*` | Motif inerte, durée du déclencheur ; pilotage du lanceur (désactivé par défaut) : `launcher_pid_file`, `state_dir`, `launcher_log` (journal d'événements du lanceur, preuve d'ordre) |

## Matrice des scénarios

Verdicts : PASS / FAIL / SKIP / BLOCKED / INDETERMINATE. Un prérequis absent (carte, interface, geste,
second token, autorisation de redémarrage) donne **BLOCKED** (pytest `skip` préfixé `BLOCKED:`), jamais PASS.
Un `PendingOperation`, une perte de continuité **ou une soumission dont l'issue est inconnue** (réponse POST perdue, `Board.uncertain`/`pending`, `PendingOperation` jusque dans un groupe d'exceptions de clôture) donnent INDETERMINATE, quelle que soit l'autre issue du test ; l'erreur initiale est conservée et relancée, rien n'est rejoué.

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
| Q2a expiration naturelle | grant le plus court (60 s demandé, ≥ 10 s observé), puis rien | grant observé à 0 par expiration ; réarmement → `403 grant_required` ; désarmement `{"action":"disarm"}` par la réservation active réussi ; grant toujours 0 |
| Q2b révocation locale | grant de 300 s demandé (**≥ 150 s observé** : un grant de 300 s est déjà inférieur à 300 s à la première lecture), révoquer sur demande | mêmes assertions ; la révocation est distinguée de l'expiration (grant > 10 s restant avant le passage à 0), sinon BLOCKED |
| Q2c nouvelle réservation | grant de 90 s demandé (≥ 45 s observé), révocation finale | grant **valide** ; désarmement par une nouvelle réservation du même propriétaire → `403 trigger_not_owned` (donc pas un refus de grant). **Conclusion limitée à ce refus** : la libération de la réservation armante révoque le bail et l'agent annule son déclencheur ; ce cas ne montre rien sur un déclencheur encore armé |
| Q2d autre identité | idem Q2c + `BENCH_CLIENT_TOKEN_B` | désarmement par une identité distincte → `403 trigger_not_owned` ; BLOCKED sans second token |

Réservation 600 s, déclencheur ≤ 120 s ; si le déclencheur expire avant le contrôle, BLOCKED (gestes trop lents).

### Q3 — Redémarrage coordonné au repos (`test_q3_coordinated_restart.py`, flux dans `q3_flow.py`)

- **Prérequis** : `--qual-allow-restart`, `q3.allow_restart`, `launcher_pid_file`, `state_dir`, `launcher_log` ; carte au repos : aucune opération indéterminée ou non réconciliée, agent sans blocage (*hold*), grant 0. Un blocage ou une opération historique à réconcilier **n'est pas masqué** pour obtenir PASS.
- **Un seul serveur établi avant tout effet** (aucune réservation, aucun signal avant) : (1) l'URL est **loopback** (une URL distante ne désigne jamais un processus local) ; (2) le lanceur exécute `run_server.py`, a exactement un enfant `bench_server.py` et cet enfant **possède le socket d'écoute** du port de l'URL ; (3) ce même enfant **tient ouverts** `server.lock` et `operations.sqlite3` du `state_dir` configuré. Le fichier PID n'est qu'un indice. L'identité du lanceur est revérifiée juste avant `SIGHUP`. Sinon : BLOCKED, rien n'est signalé.
- **Ordre des effets** : identification → réservation (suivie par `Campaign`) → surveillance → fin de fenêtre de preuve → `SIGHUP`. Un échec **avant** le signal laisse `Campaign.close()` libérer la réservation. Après le signal, la poignée est **abandonnée explicitement** (`abandoned-handles.jsonl`) : plus jamais utilisée ni libérée, aucune reprise.
- **Preuve d'ordre arrêt → démarrage** : le journal d'événements du lanceur (`launcher_log`, lu à partir de l'offset d'avant le signal) doit montrer `Server PID <ancien> exited` **avant** tout `child started` d'un autre PID ; un `replacement refused` est un échec. Un échantillonnage toutes les 10 ms des enfants du lanceur corrobore (aucun instant à deux serveurs) mais n'est pas continu : il ne prouve pas seul l'absence de chevauchement. Un lanceur défectueux qui démarre le remplaçant avant la fin de l'ancien est détecté (test logiciel).
- **Autres assertions** : `/health` ok, `sqlite-full`, **epoch différent** ; mêmes inodes de base et de verrou et taille non décroissante ; ancien `Board` rejeté (`ContinuityError`) ; agent reconnecté, grant 0, aucune réservation, pas de quarantaine ; nouvelle fenêtre (`open_window`).
- **Échecs** : délai d'arrêt dépassé → INDETERMINATE (inspection seule : ni verrou, ni base supprimés, aucun signal supplémentaire). L'agent peut exiger un rechargement de la page (geste annoncé, attente bornée).
- **Hors périmètre** : coupure pendant une écriture (lot distinct avec autorisation et récupération dédiées).

### Extension C3 (non exécutable tant que la carte C3 n'est pas décrite)

Aucune valeur C3 n'est inventée. Il suffit d'une **nouvelle session privée** (`chip: esp32c3`, capacité, transport, offset de table, région approuvée et consentement propres) ; Q1/Q2/Q3 sont paramétrés par cette session. Interface ou carte absente : BLOCKED/SKIP justifié. Un verdict par couple carte/transport.

## Provenance d'un run

Chaque run écrit `run.json` (`declared_environment` : SHA Git de la recette, propreté des sources `bench/`, empreinte du
fichier de session — jamais son contenu) et `provenance.json` + `support/` : copie et SHA-256 de **tous** les fichiers de
support exécutés (`session.py`, `harness.py`, `restart.py`, `q3_flow.py`, `conftest.py`, modèle de session). Un arbre `bench/` modifié ou non
suivi donne BLOCKED (sauf `--qual-allow-dirty`, réservé aux simulations et consigné dans le run). Les versions serveur / agent / page
observées sont dans `before.json` (empreintes rapportées par le serveur ; l'agent et le transport restent `agent_reported`).

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

- `python -m pytest python/tests recipes -q` : 116 réussis, 15 ignorés (6 scénarios matériels + 9 existants). JavaScript non relancé en local (non modifié ; la CI le fait).
- Collecte sans opt-in : 6 ignorés, **aucune connexion** à l'URL configurée (écouteur espion) ; opt-in sans session : BLOCKED, pas PASS.
- Verdict : réponse POST perdue (vrai `Board.command`) et incertitude visible seulement à la clôture → INDETERMINATE, erreur initiale préservée.
- Q2 : les quatre cas rejoués avec un **opérateur simulé** (le simulateur RAM émet grant puis révocation à dates fixes, l'expiration naturelle utilise l'horloge réelle du grant) ; PASS simulés.
- Q3 : identification contre un vrai lanceur isolé ; refus sans signal pour URL étrangère, socket non possédé, autre `state_dir` (fichiers stables mais non ouverts par le serveur), fichier PID étranger ; ordre arrêt → démarrage vérifié par le journal du lanceur ; un lanceur défectueux (remplaçant lancé avant la fin de l'ancien) est détecté ; échec avant signal → réservation libérée ; serveur non établi → aucune réservation ; échec après signal → poignée abandonnée, jamais libérée (mutation vérifiée : le test échoue sans l'abandon).
- Provenance : SHA Git, copies et empreintes des supports ; arbre modifié bloqué.
- Q1 : rejoué contre le simulateur RAM jusqu'à la comparaison finale lors de la première version de la PR (finit BLOCKED, le simulateur ne permettant pas la révocation).
- **Non simulé** : tout le matériel ; la reconnexion réelle de l'agent navigateur (Q3) ; le redémarrage du vrai serveur avec agent.
- Outillage de simulation ajouté : `--script` (gestes opérateur datés) dans `python/tests/sim_server.py` et `tools/sim_agent.py` (simulateur uniquement).

## Arbitrages (revue du SHA `699a1d0`)

Acceptés : Q1 à une seule région pour ce lot ; aucune restauration automatique après échec (référence conservée, état attendu consigné) ;
Q2c/Q2d limités au refus de la nouvelle réservation ; Q3 grant 0 / pas de quarantaine au repos sous réserve d'absence d'opération
indéterminée ou de blocage. La PR #13 (méthode) reste séparée.

## Emplacement privé (hors Git)

Les recettes locales historiques et leurs preuves (Q0, erreur de collecte v1, Q1 v2, `ref-a`/`ref-b`) sont dans
`/root/bench-recipes` et `/root/bench-evidence` (overlay, **non persistants**) et copiés avec empreintes (47 fichiers) dans
`/root/.claude/bench-private/2026-11-06-preserved/` (volume persistant `/dev/sdd`). Rien de cela n'est dans Git.
