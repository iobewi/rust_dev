# Phase 1 : contrôle de longueur canonique côté application (#104 / #105) — préparation, non exécutée

**Statut : préparé, aucune écriture matérielle.** L'exécution exige une autorisation explicite. Tests hôte terminés ; protocole carte ci-dessous.

## 1. Le chemin réel du contrôle (lu dans `9ba9533`, `examples/firmware/ota-system/src/ota.rs`, `Handler::upload`)

Ordre exact, `params.total` = `Content-Length` reçu :

| # | Étape | Effet d'un échec |
|---|---|---|
| 1 | `reserve` (slot inactif, aucune confirmation en attente), `valid_upload` | refus avant tout effacement |
| 2 | `service.admit` : enveloppe IAM1, **RSA-PSS**, taille et digest signés = `total` et `X-Image-SHA256` | refus avant tout effacement (P5-B0) |
| 3 | `service.begin`, `ArtifactWriteSession::begin` | idem |
| 4 | `boot_proof::invalidate` : la preuve de l'ancienne image du slot part **avant** le premier octet | à partir d'ici le slot est inutilisable sous VerifiedBoot |
| 5 | journal `ota: writing <id> (<total> bytes) to ota_N` ; écriture ; `session.finish` (digest du flux) | `false` → HTTP 409 |
| 6 | relecture de la flash : SHA-256 des `total` octets **et** `esp::validate(.., total, Verify::Full)` **et**, nouveau en #105, `canonical_size(partition.offset) == Some(total)` | `false` → 409, **un seul `if` : aucun journal ne distingue ces trois causes** |
| 7 | `boot_proof::publish` (écrit la preuve, la fait juger par `gate`) puis journal `flash digest and image verified; publishing` | — |
| 8 | `service.publish` : `ota_meta` → `Written` | — |

`canonical_size` (`image/src/esp.rs`) = fin de l'image telle que **son format** la définit (en-tête, segments, bourrage, somme de contrôle, condensé final), depuis `part_offset`. C'est la même fonction que le bootloader #105 compare à la longueur signée (`BadSize`, raison `0x6e`).

### Conséquence importante pour le test négatif
La validation d'étape 6 est **bornée à `total`**. Donc :
* **canonical < total** (octets en trop après l'image, tous signés) : `validate` réussit ; avant #105 l'upload est **accepté et publié** (la preuve signe `total`, `gate` accepte) ; sous #105 il est **refusé par la nouvelle comparaison**. C'est le seul cas qui exerce la ligne ajoutée.
* **canonical > total** (image tronquée, signée sur sa longueur) : `validate` échoue (`Truncated` / `SegmentOutsidePartition`) **avant** la comparaison, **avec ou sans #105**. Le cas « canonique > signé » au sens dangereux (octets chargés mais non signés) n'est atteignable qu'au démarrage : il est qualifié par M4-bis (`BadSize` `0x6e`, carte).

Table hôte (vrai `esp::validate` sur les octets de B″, `upload-check/`) :
```
case                                        total  canonical validate              old rule  new rule
canonical image                           1050112    1050112 ok                    accepted  accepted
N1 canonical + 1 trailing bytes           1050113    1050112 ok                    accepted  REFUSED
N1 canonical + 16 trailing bytes          1050128    1050112 ok                    accepted  REFUSED
N1 canonical + 4096 trailing bytes        1054208    1050112 ok                    accepted  REFUSED
N2 canonical - 1 bytes cut                1050111          - Truncated             REFUSED   REFUSED
N2 canonical - 32 bytes cut               1050080          - Truncated             REFUSED   REFUSED
N2 canonical - 4096 bytes cut             1046016          - SegmentOutsidePartiti REFUSED   REFUSED
```
Et au démarrage, si l'image N1 avait été publiée par une application sans le contrôle, le vrai `select::plan` de #105 sur ces octets donne `refused slot 1: Proof(BadSize)`, entrée `Invalid`, repli sur l'autre slot (pilote `../p5c/plan-driver`).

Tests de #105 (arbre `9ba9533`, `--features host-sign`) : bootauth 9+7+7+7+19+1, image 12 (2 ignorés) : verts. Aucune garantie nouvelle n'est donc réclamée à l'hôte au-delà de ces tables.

## 2. Artefacts (gelés, hors dépôt car les images contiennent les paramètres Wi-Fi du banc)

| Artefact | Octets | SHA-256 | Rôle |
|---|---|---|---|
| `C105/app.bin` | 1 051 280 | `bb9cc15fe2bdfdbd1a3647c35640ce0d0bb1a41ada22875260c6ace5756e734a` | application construite depuis `9ba9533` (#105, certificat …3F), `signed-rsa` + boot `verified`, canonique = taille |
| `C105/app.env`, `app.iap1` | 614 / 4096 | `3d71f7bb870965d630c362ff64c60be174979cae8866b9898cc5c71a9b2d5b9e`, `a3404617ce10100e3650d355884511c8c2837ce658baf75987fcf339162e3286` | enveloppe et preuve : *authentic*, taille signée 1 051 280 |
| `N1.bin` | 1 050 128 | `a72596600ff99708d23fd1e3d0fdb8f0a5275b48a45d924bf26769c32f5d7b7c` | image B″ + 16 octets nuls ; **signature RSA-PSS valide, digest correct sur les octets signés** ; canonique 1 050 112 < signé 1 050 128 |
| `N1.env`, `N1.iap1` | 614 / 4096 | `b7ffd33142dbc833f263cdaa4cc1d1c936161843add1c66a955a6f198154c68a`, `27133b6f33de95bb95d363b44e06a4c62a7b5a5490f7ebeaa836acca276b2d27` | *authentic*, taille signée 1 050 128 |
| `N2.bin` | 1 048 576 | `42acd3de7941808b44f5671a6b5b3f9c6a02160e01aa2dafdd825647fb9b450f` | 1 Mio de l'image B″ ; signé sur sa longueur ; canonique 1 050 112 > signé |
| `N2.env`, `N2.iap1` | 614 / 4096 | `0b9ba832d22aec935e1cb6d3cdcf1c8f460600cbf54c153bc183669d522225ef`, `40f21508663626b363676792c8ea0d9d5749cb8307279065e38eec46866f919b` | *authentic*, taille signée 1 048 576 |

`check_canonical.py prepare --frozen <dossier>` revérifie : manifeste complet 10/10, preuves *authentic* sur la taille exacte, N1 = B″ + 16 zéros, N2 = préfixe de 1 Mio. Les artefacts de référence A″/B″ (`MANIFEST` 17/17) ne sont pas touchés. L'application C n'est **pas** B″ modifiée : elle est reconstruite depuis `9ba9533`, 1 168 octets plus grosse.

Matériel de restauration (hôte, empreintes complètes) : `p91/p6-restore-A-ota0-0x20000.bin` (`9677f1c836c5a53e2dcc90b2f1c154303139a3271c38fa1b7c5ead3929a858dc`), `p91/p6-restore-B-ota1-0x1d0000.bin` (`46a5d06c2e6db70eb572c97fced63a8d05a32db8d3e2d498cff66b4f9a770a6e`), `p91/p6-restore-state-P5C-0x11000.bin` (`otadata` + `ota_meta` de P5-C, 16 384 o, `69d93c579a2a0e3549ebbd093f6df82599db055357f8bb5b3c46360a09a90ae5`) : octets relus de la carte à P5-C.

## 3. Comment exercer #105 sur la carte
Le contrôle s'exécute dans l'application **en cours d'exécution**. A″ et B″ ne l'ont pas : il faut donc faire tourner C. La voie réelle : téléverser C dans le slot inactif (`ota_0`) depuis B″, l'activer (chemin P5-C), puis téléverser N1 et N2 **depuis C**. Aucun écrit direct de métadonnées, **aucune écriture ROM sur le chemin nominal** (accès complet inutile).

Preuve que le refus vient du contrôle canonique et non d'autre chose, sans modifier le code testé (aucune trace ne distingue les causes à l'étape 6) : un **témoin différentiel sur carte**, N1 téléversé d'abord depuis **B″** (ancien code) est **accepté** (`200 staged`, `publishing`) : signature, digest et structure de N1 sont donc bons sur ce chemin ; le même N1 téléversé depuis **C** est refusé. Combiné au `BadSize` hôte.

## 4. Risques du remplacement de l'application
* L'OTA n'écrit jamais le slot en cours d'exécution. **Le slot inactif contient la solution de repli** : T0/H1 écrasent A″ (`ota_0`), N1/N2 écrasent B″ (`ota_1`). Pendant ces fenêtres il n'y a plus de repli authentifiable ; l'application en cours (B″, puis C) reste `Valid` et démarre.
* Coupure pendant un upload : le slot inactif est sans preuve (étape 4) ⇒ refusé au boot ; l'application `Valid` démarre ; `ota_meta` n'est pas publié. Restauration ROM du slot depuis les sauvegardes.
* **C est une application jamais exécutée sur la carte.** Si elle ne confirme pas, le premier rollback réel de la carte se produit hors protocole (retour sur B″, `Valid` seq 2, `ota_meta` vidé par B″). C'est la raison pour laquelle l'activation de C est un point d'arrêt.
* T0 publie une preuve *authentic* sur N1 dans `ota_0` : **ne jamais l'activer** (sous #105 il serait refusé `BadSize`, sous l'ancien bootloader il démarrerait).
* Les images contiennent SSID et mot de passe Wi-Fi : elles restent locales, jamais publiées.

## 5. Protocole matériel (autorisation distincte requise)

Tous les contrôles par `check_canonical.py` ; relectures = six plages, tags `p6_*` ; accès **limité** sauf branche de restauration. État de départ = référence `bench/p5c/refs_after_p5c.json`. Chaque upload : `activate_client`-équivalent HTTPS (TLS vérifié) avec jeton en variable d'environnement.

| Étape | Action (une seule à chaque fois) | Attendu | Contrôles |
|---|---|---|---|
| S0 | relecture, `/health` 200 (cert …3E), file vide | B″ `Stable` seq 2, A″ seq 1 | `diff p5c_r1 → p6_s0` aucune zone ne change |
| **T0** (témoin, depuis B″) | upload `N1` + `N1.env`, id `p6-n1-t0` → `ota_0` | **200 `staged`**, `writing` puis `publishing` | `capture --stage accepted` ; relecture `p6_t0` : zones changées ⊆ {`ota0_image`, `ota0_proof`, `ota_meta`} ; `ota_meta` = `Written`, `ota_0`, 1 050 128 ; ne pas activer |
| H1 | upload `C105/app.bin` + `app.env`, id `p6-c105` → `ota_0` | **200 `staged`** | idem ; `ota_0[0:1051280]` = C105, preuve `a3404617…` *authentic* |
| H2 | `mark`, **un** `/ota/activate` (`p6-c105`), ne rien toucher | 200 `activating` ; `plan slot=0 seq=3` ; écriture `state=1` ; `running ota_0 status PendingVerify` ; `boot: confirmed` | `capture --stage activate-c` ; relecture : `otadata` [seq 3 Valid, seq 2 Valid], `ota_meta` active = C, `staged` vide ; `/health` 200 (cert …3F) |
| H2b | un redémarrage | `Stable`, aucune écriture | `capture --stage reboot-c` |
| **N1** (depuis C) | upload `N1` + `N1.env`, id `p6-n1-c` → `ota_1` | **HTTP 409** `upload refused or verification failed` | `capture --stage refused` ; relecture `p6_n1` : zones changées ⊆ {`ota1_image`, `ota1_proof`} ; `iap1_pack verify` sur la relecture : **pas** *authentic* ; `/health` 200 |
| **N2** (depuis C) | upload `N2` + `N2.env`, id `p6-n2-c` → `ota_1` | **409** | idem (cause : `validate`, indépendante de #105) |
| Témoin positif (depuis C) | upload `B2/app.bin` + `B2/app.env`, id `p6-b-restore` → `ota_1` | **200 `staged`**, `publishing` | `capture --stage accepted` ; `ota_1` = B″ (`46a5d06c2e6db70eb572c97fced63a8d05a32db8d3e2d498cff66b4f9a770a6e`), preuve `d1d5843c…` *authentic* ; `ota_meta` `Written` pour B″ ; zones ⊆ {`ota1_image`, `ota1_proof`, `ota_meta`} |
| Retour à la référence P5-C (décision) | accès complet : `p6-restore-A-ota0-0x20000.bin@0x20000`, `p6-restore-state-P5C-0x11000.bin@0x11000`, relecture, un démarrage | B″ `Stable` seq 2, A″ seq 1 : six plages = `refs_after_p5c.json` | `check_p5c.py flash --state r1`-équivalent ; ou **conserver C** comme application courante (décision) |

Les relectures entrent en ROM (reset) : elles ne se font jamais entre l'activation de C et `boot: confirmed`.

## 6. Critères

* **PASS** (tout à la fois) : T0 accepté (le témoin prouve que N1 est recevable sans #105) ; N1 refusé par C en 409, **jamais** `publishing`, avec signature acceptée (`writing` présent, aucun `Signature Check Failed`) ; N2 refusé ; aucune mutation hors `ota_1` (image et preuve) pour les deux refus : `bootloader`, table, `otadata`, `ota_meta`, `ota_0` (C et sa preuve), `config` identiques octet pour octet ; la preuve de `ota_1` n'est plus *authentic* après le refus (aucun état publiable) ; le témoin positif est publié et `ota_1` = B″ ; A″ jamais activée ; activation de C conforme (§5 H2) ; `/health` 200 TLS vérifié à chaque étape.
* **FAIL récupérable** : refus attendu absent (N1 accepté par C ⇒ le contrôle n'est pas actif), état non conforme mais toutes les plages identifiées : restauration par les fichiers `p6-restore-*` (ROM, accès complet, relecture avant tout démarrage).
* **INDETERMINATE** : verdict `indeterminate` du banc, connexion HTTPS interrompue sans réponse pendant un upload, capture perdue : aucune opération, relecture seule, jamais converti en PASS. Une connexion coupée par un redémarrage n'est pas un échec par elle-même (leçon P5-C) : on tranche d'après les métadonnées et le journal.
* Arrêt au premier écart ; aucun second essai automatique ; aucun reset de diagnostic.

## 7. Restauration (ROM, indépendante de l'application)
* **R1** (échec avant l'activation de C, B″ en cours) : `ota_0` ← `p6-restore-A-ota0-0x20000.bin`, `0x11000–0x15000` ← `p6-restore-state-P5C-0x11000.bin` (efface le `Written` éventuel).
* **R2** (échec après l'activation de C, `ota_1` abîmé) : `ota_1` ← `p6-restore-B-ota1-0x1d0000.bin` (image et preuve).
* **R3** (retour exact à P5-C) : R1 + R2 + `state`, six plages = `refs_after_p5c.json`.
* Rollback inattendu de C : B″ `Valid` démarre ; établir l'état par relecture seule ; ne pas rejouer.

## 8. Ce que ce protocole ne démontre pas
* Le nom `BadSize` n'est jamais émis par l'application (elle répond 409) : l'attribution repose sur le témoin différentiel (T0) et le pilote hôte.
* Le cas « canonique > signé » côté upload est refusé par `validate`, pas par #105 ; côté démarrage il est qualifié (M4-bis).
* Une seule exécution, sur ESP32-S3 ; ESP32-C3 non testé sur carte.
