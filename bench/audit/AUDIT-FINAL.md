> Archive historique de la campagne VerifiedBoot sur `bench/audit-final` (9938b64). Ce document ne décrit pas l’API v1 du banc générique. Les recettes référencées restent dans les branches sources ; voir `../docs/architecture.md`.

# Audit final VerifiedBoot #91 / #101 / #104 / #105 — avant fusion

Date : 2026-11-02. Périmètre : ESP32-S3 (QFN56 v0.2, 16 Mio). Aucune opération matérielle pour cet audit ; la carte est restée à l'état P5-C (`bench/p5c/refs_after_p5c.json`), sauvegardes intactes.

## 1. État réel des PR et des issues

| | #101 | #105 |
|---|---|---|
| Statut | ouverte, **brouillon**, `MERGEABLE`/`CLEAN` | ouverte, **brouillon**, `MERGEABLE`/`CLEAN` |
| Base | `main` (`529d453`, 0 commit de retard : aucun rebase nécessaire) | `feat/verified-boot-91` (empilée) |
| HEAD | `cc3a8a139f397ab5e8a7eaaaba21bc49f7d67c18` | `9ba953362e52103c488bb298b218843464a6b5b9` |
| Contenu | 29 commits, 84 fichiers, +7 379 / −90 | 1 commit, 9 fichiers, +299 / −10 |
| CI | **59/59 SUCCESS** | **59/59 SUCCESS** |
| Revue | aucune (`reviewDecision` vide) | aucune |
| Issues | « Refs » #91 | « Fixes #104 » |

* `main` n'est pas protégée (404), le ruleset « preserve main » est désactivé ; la convention du dépôt est le **squash** (`feat(...) (#issue) (#PR)`), `deleteBranchOnMerge` désactivé.
* #91 et #104 sont ouvertes. #100 (publish/Activating) et #92 (anti-rollback, clés, coupures) restent ouvertes et distinctes.
* Parent de #105 = `d659865` = tête de #101 moins un commit de documentation (+6 lignes, `cc3a8a1`).
* CI : `format`, `discover`, tests hôte + Clippy par crate (OTA et générique), compilation ciblée **ESP32-S3** (toolchain épinglée 1.98.1.0) et **ESP32-C3** (`riscv32imc`), matrice console, second étage vérifié (liaison et gabarit de taille) pour S3 et C3. Le planifié nocturne teste aussi d'autres versions de toolchain.

### Code proposé et code qualifié sur la carte
| Élément qualifié | Source | Identique au code proposé ? |
|---|---|---|
| Bootloader `a7e884a2…667d1` (34 976 o) | `9ba9533` (reconstruit deux fois à l'identique) | **oui** = tête de #105 |
| Applications C et D | `9ba9533` (+ une ligne de trace et la feature de test `hang-self-check` pour D) | **oui** (code de #105) ; D diffère par la feature de test |
| Applications A″ et B″ | `2e62840` + correctif SHA, **sans** la console de #102/#103 ni #105 | non : plus anciennes que le code proposé ; C et D couvrent le code proposé |
| Docs, CI, outils `.github` | — | non qualifiables matériellement |

Aucune divergence de code non qualifiée dans #105. Dans #101, la seule divergence avec le code qualifié est le commit `cc3a8a1` (documentation). Reste **non exercé sur carte** (voir §3) : ESP32-C3, profil ECDSA P-256 de l'OTA, backends SHA `hal`/`soft` du second étage, `diag-panic`, supervisor/`ota-supervised`.

## 2. Revue des modifications
* **#101 (VerifiedBoot)** : `iobewi-firmware-bootauth` (nouveau, +2 768) : preuve IAP1 (dernier secteur de 4 Kio du slot, mot de validation écrit en dernier), profils scellés `UnsignedBoot`/`VerifiedBoot`, `select::plan` (enveloppe de `plan_boot`, flash erreur ≠ refus), `provision` (`invalidate` / `publish` / `gate`), `writer` ; `iobewi-firmware-auth` (RSA-PSS 3072, contrôle de cible, lecture du manifeste en place) ; `layout` (secteur de preuve exclu de la capacité utile sur tous les chemins) ; `iobewi-esp-boot` (SHA-256 et RSA-PSS de la ROM) ; bootloader (feature `verified-boot`, clés épinglées à la compilation, profil par défaut non signé) ; `ota-system` (écriture de la preuve à l'upload, `bootable` à l'activation) ; géométrie large (table à `0x10000`) ; CI et documentation.
* **#105** : `Image::canonical_size`, `Structure::Bootable { canonical_size }`, refus `BadSize` (raison `0x6e`) dans `select::plan` ; le bootloader transmet la longueur ; l'upload exige `canonical_size == taille reçue` (6 lignes dans `ota.rs`) ; 7 tests + 2 balayages exhaustifs ; documentation.
* **Hygiène** : aucune clé privée, jeton, SSID ni IP dans les deux diffs. **Une donnée du banc est publiée** : l'adresse MAC de la carte, deux occurrences en documentation (`docs/firmware/verified-boot-migration.md`, `verified-boot-qualification.md`) : à retirer.
* **Obsolescence** : `docs/firmware/verified-boot.md` affirme encore « *Rien n'a été installé sur une carte* » et donne 34 864 o pour le second étage vérifié S3 (qualifié : 34 976 o) ; la description de #101 dit « *Ne pas fusionner* », mesure 34 256 o et liste comme « non fait » des éléments aujourd'hui faits (écriture de la preuve à l'activation, vérification en contexte bootloader, durées au boot). Les rapports matériels ne sont que dans des commentaires de PR, pas dans `docs/`.

## 3. Correspondance exigences / preuves

H = hôte, M = matériel (ESP32-S3). Rapports : commentaires de #101 sauf mention.

| Propriété | Preuve hôte | Preuve matérielle | Limites |
|---|---|---|---|
| Une image non authentifiée n'est pas exécutée dans le profil vérifié | tests `bootauth` (slots falsifiés, mauvaise clé, preuve absente/tronquée/corrompue) | P3-F, M4 (A″ authentifiée, nominal) ; P4-A `BadSignature` (6545924074) ; P4-C `DigestMismatch` + repli (6549892382) | S3 seulement ; la ROM revérifie 2 fois un refus (latence) |
| Repli authentifié sur le slot précédent | `boot_selection`, `ota_sequence`, modèle de rollback | P4-B (6546295590), M4-bis (6561924341, #104), rollback (6575199025) | repli vers `ota_1` démontré ; le sens `ota_1 → ota_0` ne l'est pas |
| Signature RSA-PSS, épinglage de clé (SHA-256 de la structure complète) | `auth`/`bootauth` (rsa_algorithm.rs, 267 lignes) | idem ci-dessus (ROM RSA exercée) | clé du banc ; pas de rotation |
| Longueur signée = longueur canonique au démarrage (`BadSize`) | `canonical_size.rs` (7) + `esp_tests` (12, 2 balayages) | M4-bis : préfixe signé 1 Mio refusé `0x6e`, repli B″, restauration | — |
| Longueur canonique à l'upload | `bench/canonical/differential.py` (vrais validateurs, versions figées) | #105, 6574371681 (PR #105) : N1 → 409 sans `publishing`, relecture ROM = octets envoyés, preuve vierge, aucune mutation hors `ota_1` | attribution **par élimination** (un seul `if`, aucune trace) ; pas de build instrumenté |
| Upload signé, rejet d'un upload invalide | `auth_service`, `ota` hôte | P5-B0 (6555494464), P5-B (6562124090) | image uploadée identique à B″ |
| Publication IAP1 : la preuve suit l'image | `proof_write`, `provision` | P5-B, P5-C, #105 (preuve relue = octets attendus) | — |
| Refus d'activation sur preuve invalide (`bootable`) | `auth_service` | P5-A (6572373649) : 409, métadonnées inchangées | le 409 est générique : cause établie par la trace RSA ROM et le verdict hôte |
| Activation → `PendingVerify` → confirmation → persistance | `boot/tests` (coupure à chaque commande flash), `transaction` | P5-C (6573341394) ; M2-C non signé (6545206328) | une exécution |
| Rollback automatique (`PendingVerify` → `Aborted` → repli `Stable`) | modèle hôte du scénario complet | rollback (6575199025) : reset logiciel 15,7 s, `Aborted`, B″ `Stable`, restauration exacte ; #68 (non signé) | chemin watchdog matériel, `reject-self-check` sous VB et HALT non exercés |
| Atomicité des métadonnées (`otadata` mot de validation écrit en dernier, `ota_meta` deux copies CRC) | tests de coupure à chaque commande flash, entrées tronquées | transitions observées, états relus octet pour octet | **aucune coupure d'alimentation sur matériel** : ne pas déclarer « power-loss safe » (#92) |
| Non-régression du boot non signé | profil par défaut non signé conservé | référence S0 de toutes les campagnes avant M4 | — |
| Frontière `common` / `arch` | aucune dépendance ESP dans `common/firmware/*` (Cargo.toml et sources vérifiés) | — | `common/firmware/image/src/esp.rs` est un analyseur du *format* d'image ESP (sans HAL) |
| ESP32-C3 | CI : compilation, liaison, gabarit (27 296 o vérifié) | **aucune** | RSA/SHA ROM du C3 jamais exécutés ; ne rien attribuer à partir du S3 |
| Racine matérielle (Secure Boot V2, eFuses, chiffrement flash) | — | — | **non implémentée**, documentée ; aucun eFuse touché |
| Anti-rollback persistant, rotation/révocation | — | — | #92 |

## 4. Audit de sécurité

### Points vérifiés (conformes)
* `VerifiedBoot::admit` : preuve lue, classée (absente/tronquée/corrompue), algorithme, forme de la clé, **empreinte de la clé épinglée** (modulus, exposant, valeurs de Montgomery) *avant* la signature ; signature ; cible (produit, MCU, variante, version minimale) **après** signature ; taille ≤ capacité utile ; SHA-256 sur exactement la taille signée ; `Authentic` seulement si tout passe. Profils scellés (`Sealed`), `NoPins`/`ZeroPin` refusés. Une erreur de lecture flash n'est jamais enregistrée comme refus (`Verdict::Io`, 3 tentatives puis arrêt sans écriture).
* #105 : la longueur signée doit égaler la longueur canonique calculée à partir de l'image elle-même, en arithmétique vérifiée ; `fail-closed`.
* Ordre d'upload : autorisation (signature) avant tout effacement ; `invalidate` avant le premier octet ; preuve écrite puis relue par la même vérification que le boot ; `ota_meta` publié en dernier. À l'activation : re-hachage du slot puis `bootable` avant toute écriture persistante.
* Transitions `otadata` : `activate` écrit une seule entrée dans le secteur qui ne porte pas le dernier `Valid` (le repli reste sélectionnable à toute interruption) ; tout `Pending` devient `Aborted` avant la décision ; entrées tronquées ignorées, jamais réensemencées.

### Risques classés
**Bloquants (code) : aucun.**

**À corriger avant la fusion (documentaires, sans effet sur la carte)**
1. Description de #101 obsolète (« Ne pas fusionner », mesures, « Non fait ») ; `verified-boot.md` obsolète (« Rien n'a été installé sur une carte », 34 864 o).
2. Adresse MAC de la carte dans deux documents.
3. Les rapports de qualification ne sont référencés que par des commentaires de PR : ajouter une matrice de qualification dans `docs/` (S3 qualifié / C3 non qualifié).
4. PR en brouillon, sans revue.

**Non bloquants, à corriger (suivi)**
5. **Destruction du repli pendant un upload finalement refusé ou interrompu** : `invalidate` efface la preuve du slot inactif *avant* le premier octet, puis l'image est effacée par blocs de 64 Kio ; un refus tardif (digest, structure, longueur canonique) laisse l'appareil **sans repli authentifiable** jusqu'à une restauration ROM ou un nouvel upload réussi. Aucun mécanisme n'avertit l'opérateur. C'est inhérent à une OTA à deux slots, démontré sur la carte (N1) ; atténuations possibles : signaler l'absence de repli, contrôler avant `invalidate` ce qui l'est (longueurs, enveloppe), documenter. Si l'application en cours échoue ensuite, le bootloader s'arrête (`NoUsableEntry`).
6. **Panic de l'application après confirmation** : le gestionnaire boucle (`spin_loop`) alors que le chien de garde est désarmé à la confirmation : un panic en régime établi **fige l'appareil** jusqu'à un cycle d'alimentation (disponibilité à distance). Antérieur à #101 (même gestionnaire dans A″/B″).
7. **Refus d'upload non diagnostiqué** : trois causes distinctes (digest, structure, longueur canonique) dans un seul `if`, sans journal ; attribution et exploitation difficiles.
8. **Surface « non exercée » sur carte** : chemin du chien de garde matériel, `reject-self-check` sous VerifiedBoot, absence d'image authentique (`HALT`), sens de repli `ota_1 → ota_0`, profil ECDSA P-256 de l'OTA, coupures d'alimentation.
9. Marge de taille : second étage S3 vérifié 34 976 o dans une région de 65 536 (limite CI 61 440) : large ; C3 27 296 o dans 32 768 (historique) : marge ≈ 5,4 Kio, à surveiller.

**Limites documentaires (déjà connues, à conserver visibles)**
* Pas de racine matérielle : un attaquant qui réécrit le bootloader (flash externe, JTAG/USB-JTAG actifs, pas de chiffrement flash) contourne tout ; les clés sont épinglées dans le bootloader. Une fenêtre de vérification puis chargement (deux lectures de la flash) n'est exploitable que par un attaquant ayant déjà un accès physique à la flash.
* Rejeu d'une ancienne image valablement signée (anti-rollback persistant : #92) ; `min_security_version` à 0 sur la carte.
* Pas de rotation ni révocation dynamique des clés ; gestion de la clé de signature de production non définie (clé du banc utilisée) ; jeton OTA et certificat compilés dans l'image.
* Les résultats sont **ESP32-S3 uniquement** ; RP2350/Teensy hors périmètre.

## 5. Portabilité
* Cœur : `auth`, `boot`, `bootauth`, `image`, `layout`, `slots`, `update` sont `no_std`, sans dépendance `esp-*` ni ESP-IDF (aucune cible ni cfg ESP dans `common/firmware`). Les adaptateurs (`arch/esp32/*`, `examples/*`) portent les adresses ROM, la géométrie et le chargement.
* Compilation : S3 et C3 construites en CI (applications `signed` et `signed-rsa`, second étage vérifié avec liaison et gabarit, backends SHA `rom`/`hal`/`soft`, diagnostic) — **sans exécution** sur C3.
* Interfaces communes : `BootPolicy` scellée, `SlotRead`, `Sha256Stream` ; `Structure::Bootable { canonical_size }` est un invariant indépendant de la cible (calculé par l'adaptateur).
* **C3 : non qualifié matériellement.** Les adresses de fonctions ROM RSA-PSS/SHA du C3 n'ont jamais été exécutées : la fusion ne doit pas être présentée comme une qualification C3.

## 6. Plan de fusion
1. **Avant** : (a) autoriser les modifications de documentation sur `feat/verified-boot-91` (§4 points 1–3) — commits documentaires seulement, vérification que `git diff 9ba9533 HEAD` ne touche aucun fichier de code ; (b) créer les issues de suivi (§7) ; (c) geler `main` (aucune autre fusion) ou, si `main` avance, fusionner `main` dans la branche et relancer la CI puis revérifier l'arbre de code ; (d) pose d'une étiquette sur `9ba9533` (`verified-boot-s3-qualified`) pour garder la traçabilité des campagnes (les squash changent les empreintes).
2. **Fusionner #105 dans `feat/verified-boot-91`** avec un **commit de fusion** (pas un squash : `9ba9533` reste atteignable). CI de #101 attendue verte (59/59). Vérifier : code identique à `9ba9533` (`git diff 9ba9533 <tête> -- . ':!docs' ':!.github'` vide).
3. Mettre à jour description et documents de #101 ; sortir #101 du brouillon ; revue.
4. **Squash de #101 dans `main`** (convention du dépôt) avec message `feat(boot): verified boot (#91) (#101)` listant « Fixes #104 », « Refs #91 » et les rapports. Le mot-clé de #105 (« Fixes #104 ») ne se déclenche pas (fusion hors branche par défaut) : le placer dans le message du squash ou fermer #104 à la main.
5. **Après chaque fusion** : CI `Firmware OTA CI` et `Rust MCU CI` (pull_request puis push sur `main`) ; le lendemain, la CI planifiée (autres toolchains). Après le squash : `git diff 9ba9533 <squash> -- ':!docs' ':!.github'` doit être vide (arbre de code qualifié = arbre fusionné).
6. **Issues** : fermer #104 (critères satisfaits : préfixe insuffisant refusé avant `jump` — M4-bis —, images complètes admises — P5-C —, aucun fail-open) ; #91 : critères d'acceptation satisfaits pour le profil logiciel ESP32-S3 ; le périmètre restant (profil matériel, RP2350/Teensy, renouvellement des clés) passe dans les issues dédiées avant fermeture, ou #91 reste ouverte comme parente de ces suivis (décision du propriétaire) ; commenter #92 (coupures matériel, anti-rollback, rotation) avec les éléments ci-dessus ; #100 inchangée.
7. **Alternative écartée** : squash de #101 d'abord, puis réécriture de #105 (`rebase --onto`) : l'empreinte qualifiée serait perdue et #104 ne serait fermée qu'au second merge.

## 7. Issues de suivi proposées
1. Qualification matérielle d'ESP32-C3 : second étage vérifié, ROM RSA-PSS/SHA, chaîne OTA.
2. Profil à racine matérielle (Secure Boot V2, chiffrement flash, politique eFuse) pour S3 ; contrainte second étage ≤ 28 672 o ; durcissement JTAG.
3. Disponibilité : panic après confirmation (chien de garde désarmé) ⇒ reset contrôlé.
4. OTA : état du repli après upload refusé/interrompu (signalement, atténuations) et diagnostic de la cause du refus à la relecture.
5. Qualification matérielle des chemins restants : chien de garde matériel, `reject-self-check` sous VerifiedBoot, absence d'image authentique, repli `ota_1 → ota_0`, profil ECDSA.
6. Capacités de vérification au boot de RP2350 et Teensy par adaptateur (reste du périmètre de #91).
7. (Dans #92) coupures d'alimentation sur matériel, anti-rollback, rotation et custody de la clé de production.

## 8. Décision

**GO conditionnel pour la fusion** (ordre : #105 dans la branche de #101, puis squash de #101 dans `main`) :
* aucun défaut de sécurité bloquant ; le code proposé (#105) **est** celui qualifié sur S3 (bootloader, applications C et D) ; CI 59/59 sur les deux PR ; #101 sans retard sur `main`.
* conditions : (1) corriger la documentation obsolète et retirer la MAC ; (2) créer les issues de suivi, notamment C3 et la racine matérielle ; (3) revue humaine et sortie du brouillon ; (4) `main` gelé ou CI relancée après tout mouvement ; (5) étiquette de traçabilité.
**NO-GO** si la fusion doit valoir qualification ESP32-C3, protection contre un attaquant physique, anti-rollback ou « power-loss safe » : ces garanties ne sont pas fournies.

