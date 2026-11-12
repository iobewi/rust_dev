# Qualification générique ESP32-P4 / ESP32-C6 (G1)

**Statut : recettes écrites et qualifiées en SIMULATION uniquement. NON exécutées sur matériel.** Aucune écriture, aucun effacement, aucune réinitialisation n'a été faite sur les cartes pour ce lot. Un PASS simulé n'est jamais un PASS matériel.

Ce lot est **générique** : il qualifie Device Lab sur ces deux MCU. Il ne contient aucune assertion de firmware ni aucune recette SDIO.

## 1. Cartes visées

| Carte logique | MCU | Flash | Pont | Entrée dans le loader ROM |
|---|---|---|---|---|
| `esp32-local` | ESP32-P4 | 32 Mo | CH343 (1a86:5593) | Séquence DTR/RTS de l'agent : **câblage inféré** des identifiants USB, jamais mesuré |
| `esp32-zero` | ESP32-C6 | 8 Mo | CP210x (10c4:ea60), connecteur H7 | **Manuelle** : GPIO9 à GND puis reset. DTR/RTS non connus pour atteindre EN/GPIO9 |

Les deux sont des cartes logiques indépendantes du même agent navigateur (liste « Cartes actives »). Le banc ne connaît pas l'appariement physique : une session de recette décrit **une** carte, on exécute donc une session par carte.

## 2. Écart serveur actif / checkout

Le serveur actif tournait en `408cf5c3a`, le checkout était en `4ae45d0fa`. Le seul commit d'écart est le merge de la PR #50, **sans différence d'arbre**. Les empreintes observées étaient identiques (`server_sha256` `02c44fd3…`, `assets_sha256` `66a0543e…`). Le serveur a ensuite été rechargé sur `main` (PR #51, correctif d'affichage de l'interface) : seul `assets_sha256` change. Les recettes enregistrent ces empreintes dans `before.json` de chaque run.

## 3. Capacités et état de qualification

| Capacité | Constat |
|---|---|
| Identification `esp32p4` / `esp32c6` | **Observée** : le banc les annonce (famille issue d'esptool-js 0.7.0, sans liste figée côté serveur) |
| `partitions`, `flash.read`, `flash.write`, `flash.erase`, retour console | **Non qualifiés** sur ces puces (les recettes génériques ne couvraient que S3/C3) |
| `reset`, `io0`, `boot.download` | Annoncés pour les deux cartes d'après les identifiants USB. **Câblage non mesuré** ; ne pas les utiliser sur le C6 |
| Lecture d'une flash de 32 Mo | Non chronométrée : l'agent lit par blocs de 1 Mio au plus |

## 4. Recettes

Fichiers dans `bench/recipes/qualification_campaign/`. Les recettes S3/C3 existantes ne sont pas modifiées.

| ID | Fichier | Effet matériel | Consentement (session) | Options pytest |
|---|---|---|---|---|
| PC-0 identité, PC-1 lectures répétables, PC-2 table de partitions | `test_pc_read_only.py` | Entrée dans le loader, redémarrage possible. Aucune écriture | `PC-READ` | `--bench` |
| PC-3 reset explicite et bannière ROM | `test_pc_reset.py` | Un reset | `PC-RESET` | `--bench --qual-allow-reset` |
| PC-4 cycle d'images | `test_pc_cycle.py` | **Écritures**, avec sauvegarde et restauration | `PC-CYCLE` + régions approuvées + images | `--bench --bench-allow-destructive` + grant local |

Aucune région ni image n'existe par défaut. Un modèle de session n'accorde aucun consentement (`session.p4.example.json`, `session.c6.example.json`).

### Session : ajouts
- `chip` accepte `esp32p4` et `esp32c6`.
- `loader.entry` : `auto` ou `manual`. En `manual`, `loader.download_marker` est **obligatoire et sans valeur par défaut** (à fixer d'après la documentation ROM avant un run). `loader.marker_timeout_s` 5 à 300 s.
- `images` : au plus 16 entrées `{label, path, sha256, address}`. Le fichier est privé, épinglé par SHA-256, l'adresse est alignée sur 4 Kio et doit démarrer dans une région approuvée. **Une image ne crée jamais de permission d'écriture.**

### Fenêtre de loader manuelle (C6)
Avant la première commande de loader d'un run, la recette annonce le geste, puis attend la **bannière ROM déclarée** sur l'UART après un curseur pris *avant* le geste. Sans bannière : BLOCKED, **aucune commande n'a été soumise**. Si la première commande échoue avec `serial_timeout`, `serial_partial_packet` ou `serial_invalid_packet`, le résultat est BLOCKED (le geste est à refaire), pas FAIL. Une nouvelle soumission après un geste est une opération explicite, jamais un retry automatique.

### Plan d'images (vérifié avant toute commande)
Fichier présent, lisible, de 1 octet à 16 Mio, SHA-256 identique à la session ; plage d'effacement (arrondie à 4 Kio) incluse dans **une seule** région approuvée ; pas de chevauchement entre images, même au niveau secteur ; fin dans la flash. Le serveur et l'agent revérifient famille de puce, plages, chevauchements et condensat.

## 5. Stratégie de sauvegarde et de restauration

Pour chaque région approuvée de la session (une carte) :

1. **Deux lectures séquentielles** (`ref-N-a.bin`, `ref-N-b.bin`) par blocs de 1 Mio au plus, fsyncées, privées. Les deux SHA-256 doivent être égaux, sinon aucune mutation.
2. Une image déjà identique au contenu actuel est BLOCKED : rien ne serait démontré.
3. Grant local demandé à l'opérateur, durée vérifiée avant (une écriture exige un grant et une réservation d'au moins `2 × timeout + 90 s` à chaque appel).
4. Écriture de chaque image (`flash.write`, une image par commande) suivie de la **relecture indépendante** par `Orchestrator.program`.
5. **Restauration** de chaque région depuis sa sauvegarde, puis **comparaison indépendante** égale à la référence.
6. Révocation locale du grant, libération de la réservation, retour console, `Campaign.close()`.

La recette ne restaure **jamais** d'elle-même après un échec, et ne rejoue aucune mutation : les références restent dans le dossier de preuves pour une restauration explicite de l'opérateur. Un verdict final PASS exige la restauration et la comparaison égales.

### Choix des régions par carte
- **P4 (32 Mo)** : approuver au minimum la plage couverte par les images (amorce, table, application). La **flash entière** est une option que l'opérateur peut décider, au prix d'une lecture longue (non chronométrée).
- **C6 (8 Mo)** : même principe. Chaque fenêtre d'opérations de loader exige le geste manuel ; prévoir un geste avant la sauvegarde et un autre après si le chip est sorti du mode téléchargement.

## 6. Stockage persistant des preuves

`/root/bench-evidence` est sur le système de fichiers superposé du conteneur et **disparaît à sa reconstruction**. Seuls les volumes déclarés survivent.

- `bench/recipes/qualification_campaign/evidence.py` décide de la persistance d'après la table des montages (système `overlay`, `tmpfs` et assimilés refusés, liens symboliques résolus).
- `PC-CYCLE` est **BLOCKED** si `evidence_root` n'est pas persistant. L'option `--qual-allow-ephemeral-evidence` existe pour la simulation et le développement ; elle est enregistrée dans `evidence-storage.json`.
- `bench/tools/prepare_evidence_root.py --root <dossier> [--check]` vérifie, puis crée avec le mode 0700 ; il ne supprime ni ne déplace rien.
- Dossier préparé dès maintenant dans le volume d'état déjà persistant : `$BENCH_STATE_DIR/qualification-evidence`.
- Un volume dédié `iobewi-qualification-evidence`, monté sur `/root/qualification-evidence`, est ajouté au devcontainer (effectif à la prochaine reconstruction). Le dossier historique `/root/bench-evidence` n'est ni déplacé ni masqué : **préserver ses preuves avant toute reconstruction**.

## 7. Risques

| Risque | Conséquence | Mitigation |
|---|---|---|
| Écrire sur l'amorce ou la table | Puce non amorçable jusqu'à une nouvelle entrée dans le loader | Références vérifiées ; C6 : geste manuel toujours possible ; P4 : séquence de l'agent inférée, donc la garder sous surveillance |
| Interruption en cours d'écriture | État indéterminé, quarantaine | Aucun rejeu ; restauration explicite depuis les références |
| Chip bloqué en mode téléchargement après un run (C6) | L'application ne démarre pas | Relâcher GPIO9 puis reset, geste de l'opérateur |
| Preuves perdues | Plus de sauvegarde | Racine persistante obligatoire |
| `reset` annoncé mais sans effet sur le C6 | PC-3 FAIL (constat de câblage) | Ne pas conclure à un défaut du firmware |
| Esptool-js 0.7.0 sur P4/C6 : écriture non qualifiée | Comportement inconnu | Qualification progressive : lecture, puis écriture sur zone de travail |
| Interface rechargée ou serveur redémarré | Agents déconnectés, cookies invalidés | Reconnecter les ports ; ne pas redémarrer pendant une campagne |

## 8. Procédure de qualification matérielle proposée (non exécutée)

Une session privée par carte, un dossier de preuves neuf par run, des recettes exécutées depuis un commit relu et un arbre propre.

1. **Préflight (aucune commande)** : `/health`, `identity()`, `devices()` ; cartes connectées, non réservées, non en quarantaine ; racine de preuves persistante (`prepare_evidence_root.py --check`).
2. **P4, lecture seule** : `PC-READ` (PC-0, PC-1, PC-2).
3. **C6, lecture seule** : geste manuel d'entrée, puis `PC-READ`.
4. **Reset (optionnel)** : `PC-RESET` sur le P4 ; sur le C6, constat de câblage attendu.
5. **Écriture sur zone de travail** : d'abord `PC-CYCLE` avec une image de test inerte sur une zone sans effet sur l'amorçage, P4 puis C6.
6. **Images firmware réelles** : seulement après revue des résultats précédents et accord explicite, avec les régions de la disposition validée.

Arrêt au premier ciblage erroné, état incertain, quarantaine ou rejeu possible. Aucune relance automatique.

## 9. Autorisations opérateur à obtenir avant la première écriture

1. Accord explicite sur chaque **scénario** inscrit dans la session privée (`PC-READ`, `PC-RESET`, `PC-CYCLE`), avec ses effets.
2. **Régions approuvées** par carte (adresses, tailles) et **images** (fichiers, SHA-256, adresses).
3. Décision sur la **sauvegarde de la flash entière** du P4 (durée) et du C6.
4. Racine de preuves **persistante** confirmée, avant la première sauvegarde.
5. **Grant local** dans la page, durée suffisante pour tout le cycle ; révocation locale à la fin.
6. Geste **manuel** d'entrée dans le loader du C6 (GPIO9 à GND, reset) et bannière ROM déclarée.
7. Consentement au `reset` du P4 par le banc, si PC-3 est exécutée.
8. Option `--bench-allow-destructive` : elle ne vaut **pas** consentement matériel.

## 10. Contrôles logiciels

`bench/python/tests/test_pc_recipes.py` : session, plan d'images, persistance des preuves, fenêtre de loader (parties pures), et recettes exécutées contre le simulateur en RAM déclaré `esp32p4` / `esp32c6`. Le simulateur ne prouve rien sur les cartes réelles, ni sur le CH343, le CP210x, Web Serial ou la ROM.
