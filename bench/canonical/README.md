# Qualification du contrôle de longueur canonique côté application (#104 / #105) — protocole final

**Statut : une exécution autorisée (mandat « qualification matérielle finale »).** Cette version ajoute le témoin positif (§4, étape **P**) ; elle remplace les précédentes (§9).

## 1. Revue de `Handler::upload` (arbre `9ba9533`, `examples/firmware/ota-system/src/ota.rs`)

`params.total` = `Content-Length`. Effets **flash** de chaque étape, d'après le code (`provision.rs`, `shared_flash.rs`, `service.rs`, `otm1.rs`) :

| # | Étape | Écriture flash | Si elle échoue |
|---|---|---|---|
| 1 | `reserve` (slot inactif, aucune confirmation en attente), `valid_upload` | aucune | 409 |
| 2 | `service.admit` : enveloppe IAM1, RSA-PSS, taille et digest signés = `total` / `X-Image-SHA256` | aucune | 409 |
| 3 | `service.begin` | **si un enregistrement `ota_meta` supersédable existe** (`Written`), il est **vidé tout de suite** (une trame) ; avec `Activating`, refus `Conflict` sans écriture ; sans enregistrement : aucune | 409 |
| 4 | `ArtifactWriteSession::begin` | aucune | 409 |
| 5 | **`boot_proof::invalidate`** | **effacement du secteur de preuve du slot inactif** (`0x37F000` pour `ota_1`, `0x1CF000` pour `ota_0`) : *avant* le premier octet d'image | 409 |
| 6 | journal `ota: writing <id> (<total> bytes) to ota_N`, puis par blocs de 2 Kio : **effacement à l'avancement, par blocs de 64 Kio depuis le début du slot** (jusqu'à `⌈reçu/64 Kio⌉ × 64 Kio`), puis programmation | image du slot inactif | 409 |
| 7 | `session.finish` (digest du flux) | — | 409 |
| 8 | relecture de la flash : SHA-256 des `total` octets **et** `esp::validate(.., total, Verify::Full)` **et** (#105) `canonical_size == total` — **un seul `if`, aucune trace ne distingue les trois causes** | — | 409 |
| 9 | `boot_proof::publish` : effacement, programmation et validation de la preuve, `gate` ; journal `flash digest and image verified; publishing` | secteur de preuve | 409 |
| 10 | `service.publish` | `ota_meta` : une trame (`Written`) | 409 |

Conséquence de la ligne 5 : dès le début d'un upload, **le slot inactif n'a plus de preuve**. Sous VerifiedBoot le bootloader le refuse, même si son entrée `otadata` reste `Valid` (rien ne la modifie).

### Régions modifiables selon le cas
| Cas | Image du slot inactif | Secteur de preuve | `ota_meta` | `otadata`, bootloader, table, `config`, slot actif |
|---|---|---|---|---|
| **Image canonique acceptée** (ex. C → `ota_0`, 1 051 280 o) | `[0x20000, 0x130000)` (17 blocs) effacés puis écrits | effacé puis réécrit (*authentic*) | +1 trame `Written` (+1 si un `Written` préexistait) | inchangés |
| **N1 refusée** (1 050 128 o → `ota_1`) | `[0x1D0000, 0x2E0000)` effacés ; `[0x1D0000, 0x2D0610)` réécrits avec les octets reçus | **effacé, jamais réécrit** (vierge) | inchangé s'il n'y a pas d'enregistrement ; **vidé si un `Written` existait** | inchangés |
| **N2 refusée** (1 Mio → `ota_1`) — hôte seulement | `[0x1D0000, 0x2D0000)` effacés et réécrits ; la queue de l'ancienne image (`0x2D0000…`) n'est pas touchée | effacé, jamais réécrit | idem | inchangés |
| **Upload interrompu après effacement** (octet *k*) | `[début, ⌈k/64 Kio⌉ × 64 Kio)` effacés, le début programmé jusqu'au dernier secteur durable, **le reste de l'ancienne image est intact** | effacé (vierge) | inchangé (aucune publication) | inchangés |

**Un refus applicatif ne préserve pas les octets de l'ancien slot** : ils sont effacés avant d'être remplacés par ceux du flux. Dans le cas de N1 (B″ + 16 octets), le contenu final est *par coïncidence* presque celui de B″ (seul le secteur `0x2D0000`, qui reçoit les 16 octets nuls, et le secteur de preuve diffèrent). Aucun mécanisme de l'application ne restaure l'ancien slot.

### Slot de repli `Valid` dans le slot inactif
* Une fois l'étape 5 passée, le repli n'est plus authentifiable. L'application en cours reste `Valid` et démarre ; si elle échoue ensuite, le bootloader n'a plus de candidat : l'entrée du repli est marquée `Invalid`, puis `Halt::NoUsableEntry` (récupération par le chargeur ROM).
* Tant que le slot actif démarre, `plan_boot` n'examine pas l'autre slot (évaluation paresseuse) : aucune écriture `otadata` n'est faite à cause du repli abîmé.
* **Garanties réelles de récupération après refus** : aucune automatique ; uniquement la restauration par le chargeur ROM depuis une sauvegarde relue (§6).

## 2. Démonstration différentielle hôte (reproductible)

`differential.py` rejoue, avec les vrais validateurs d'une exportation propre de `9ba9533` (`esp::validate` en `Verify::Full`, `provision::gate` avec RSA-PSS et digest réels, `select::plan` du bootloader #105), la décision d'upload et ses suites sur les mêmes octets. Il refuse de tourner si une version, un blob, une entrée ou la sortie diffère des empreintes figées (`differential.pins.json`, `differential.expected.txt`).

```
case                                  total canonical validate   old rule  new rule  pub. gate  boot (#105 select::plan)
canonical B'' image                 1050112   1050112 ok         accepted  accepted  accepts    boots the upload
N1 B'' + 16 trailing bytes          1050128   1050112 ok         accepted  REFUSED   accepts    slot 0; slot 1: BadSize
N2 first 1 MiB of B''               1048576         - SegmentOut REFUSED   REFUSED   accepts    slot 0; slot 1: BadSize
```
Lecture : pour N1, `validate` réussit, la preuve est *authentic* (signature et digest valides sur les octets signés, porte de publication ouverte), l'ancienne règle accepte et publierait, la nouvelle règle refuse, et le bootloader #105 refuserait de toute façon (`BadSize`, raison `0x6e`). N2 est refusée par `validate` avec ou sans #105 : il **ne distingue pas** l'ancien comportement du nouveau (la colonne « pub. gate » de N2 n'est jamais atteinte).
Versions : rustc 1.99.0 (2026-09-28), cargo 1.99.0. Sources : `2e62840` (base d'A″/B″), `cc3a8a1` (tête de #101), `9ba9533` (#105) ; `ota.rs` blobs `6389bf35…`, `29dfb064…`, `9b5a9944…` ; `esp.rs` `e61bae3a…` ; le diff `cc3a8a1→9ba9533` de `ota.rs` est exactement 6 lignes (empreinte figée). Le source de build de C porte les blobs `ota.rs` `9b5a9944b350dc3a2461801edc3932a3c648617f` et `esp.rs` `e61bae3a2ce65e65dce31dc1f24407df0b9e0426`.

**Attribution sans instrumentation : limite explicite.** Le contrôle canonique et les contrôles de digest/structure sont dans un même `if` et ne journalisent rien. Sur la carte, le refus de N1 ne peut donc pas être *observé* comme dû à la comparaison canonique. Il est établi par élimination, sur les octets réellement écrits : (1) la signature a été acceptée (journal `writing`, aucun `Signature Check Failed`) ; (2) la relecture ROM d'`ota_1` est identique à `N1.bin` octet pour octet, donc le digest de la flash est celui annoncé ; (3) l'hôte donne `validate = ok` sur ces mêmes octets ; (4) il ne reste que la comparaison canonique. Ce qui n'est pas démontré : qu'un échec transitoire non lié (écriture) n'ait pas produit le même 409 — écarté par (2). Une démonstration directe exigerait un build instrumenté (journal de la cause), donc un code différent de #105 : **non proposé par défaut, disponible sur décision**.

## 3. Artefacts (gelés hors dépôt : les images contiennent les paramètres Wi-Fi du banc, rien de tout cela n'est publié)

| Artefact | Octets | SHA-256 complet |
|---|---|---|
| `C105/app.bin` (construite depuis `9ba9533`, certificat …3F) | 1 051 280 | `bb9cc15fe2bdfdbd1a3647c35640ce0d0bb1a41ada22875260c6ace5756e734a` |
| `C105/app.env` | 614 | `3d71f7bb870965d630c362ff64c60be174979cae8866b9898cc5c71a9b2d5b9e` |
| `C105/app.iap1` | 4096 | `a3404617ce10100e3650d355884511c8c2837ce658baf75987fcf339162e3286` |
| `N1.bin` (B″ + 16 octets nuls ; canonique 1 050 112 < signé 1 050 128) | 1 050 128 | `a72596600ff99708d23fd1e3d0fdb8f0a5275b48a45d924bf26769c32f5d7b7c` |
| `N1.env` / `N1.iap1` | 614 / 4096 | `b7ffd33142dbc833f263cdaa4cc1d1c936161843add1c66a955a6f198154c68a` / `27133b6f33de95bb95d363b44e06a4c62a7b5a5490f7ebeaa836acca276b2d27` |
| `N2.bin`, `N2.env`, `N2.iap1` (hôte seulement) | 1 048 576 / 614 / 4096 | `42acd3de…450f`, `0b9ba832d22aec935e1cb6d3cdcf1c8f460600cbf54c153bc183669d522225ef`, `40f21508663626b363676792c8ea0d9d5749cb8307279065e38eec46866f919b` |

`check_canonical.py prepare --frozen <dossier>` revérifie le manifeste (10/10) et que chaque preuve est *authentic* sur la longueur exacte.
**C n'est pas « B″ plus la ligne canonique »** : son source (`9ba9533`) diffère de celui d'A″/B″ (`2e62840` + correctif SHA) aussi par l'estampille et l'impression de panique de la console (`main.rs`, feature `console-uart0` par défaut), d'où 1 168 octets de plus. La logique de démarrage et de confirmation (`boot_task`) est identique. *Correction* de la préparation P5-C : les 18 lignes de `main.rs` entre `2e62840` et `cc3a8a1` sont ces ajouts de console, pas l'installation de l'adaptateur SHA.

## 4. Séquence matérielle réduite (aucune écriture ROM sur le chemin nominal ; accès complet seulement pour R)

Un point d'arrêt après **chaque** étape : vérifier, noter, attendre. Relectures = six plages, tags `p6_*`, entrée en ROM (reset) **jamais** entre l'activation de C et `boot: confirmed`. Référence de départ : `refs_after_p5c.json` (tag `p5c_r1`).

| Étape | Action | Régions pouvant changer | Contrôles (arrêt si écart) |
|---|---|---|---|
| **S0** | `identify`, relecture `p6_s0`, `/health` 200 TLS vérifié (cert …3E), file vide, autoreset off, une seule page ; `check_canonical.py prepare`, `differential.py`, `diff p5c_r1→p6_s0 --allow ''` ; sauvegardes : dumps `p5c_r1` + `p6-restore-*` + `plan-restore` à blanc (plan vide) ; `ota_meta` : `staged` vide (sinon l'étape 3 de l'upload l'effacerait) | aucune | tout égal à `refs_after_p5c` |
| **H1** | upload de C (`app.bin` + `app.env`, id `p6-c105`) → `ota_0`, depuis B″ | `ota_0` `[0x20000,0x130000)`, preuve `0x1CF000`, `ota_meta` (+1 trame) | 200 `staged`, `writing`+`publishing` (`capture --stage accepted`) ; `diff p6_s0→p6_h1 --allow ota0_image,ota0_proof,ota_meta` ; `ota_0[:1051280]` = C ; preuve = `a3404617…` *authentic* ; `ota_meta` `Written` C (1 051 280 o). **A″ est perdue à partir d'ici** |
| **H2** | `mark` ; **un** `/ota/activate` (`p6-c105`, `app.env`) ; ne rien toucher | `otadata` secteur 0 (`0x11000`, qui portait l'entrée seq 1 d'A″), `ota_meta` | 200 `activating` ; `capture --stage activate-c` : `VERIFIED` → `plan slot=0 seq=3` → `otadata write sector=0 seq=3 state=1` → `running ota_0 status PendingVerify` → `boot: confirmed` → `OTA ready` ; zéro refus/panic/watchdog |
| **H3** | `/health` 200 TLS vérifié (cert …3F) ; un redémarrage (persistance) ; relecture `p6_h3` | aucune | `capture --stage reboot-c` (`Stable`, aucune écriture) ; `otadata` [seq 3 Valid, seq 2 Valid] ; `ota_meta` séquence 9 (`staged` vide, actif = C) ; `ota_1` = B″ inchangée (`46a5d06c…`) ; `diff p6_h1→p6_h3 --allow otadata,ota_meta` |
| **N1** | upload de N1 (`N1.bin` + `N1.env`, id `p6-n1`) → `ota_1`, depuis C | `ota_1` `[0x1D0000,0x2E0000)` et sa preuve `0x37F000` | **409** ; `capture --stage refused` (`writing`, **pas** de `publishing`, pas de redémarrage) ; `/health` 200 ; relecture `p6_n1` ; `refused-state --before p6_h3 --after p6_n1 --image N1.bin` ; `diff p6_h3→p6_n1 --allow ota1_image,ota1_proof` |
| **P** (témoin positif, depuis C) | upload de **B″ canonique** (`B2.bin` + `B2.env`, id `p6-bwit`) → `ota_1` ; dernière opération OTA de la campagne ; ni activation ni redémarrage | `ota_1` `[0x1D0000,0x2E0000)` (effacé puis réécrit : la queue nulle de N1 disparaît), preuve `0x37F000`, `ota_meta` (+1 trame `Written`) | 200 `staged`, `writing`+`publishing` (`capture --stage accepted`) ; relecture `p6_w` ; `accepted-state --before p6_n1 --after p6_w --image B2.bin --proof B2.iap1 --slot ota_1 --id p6-bwit` ; `diff p5c_r1→p6_w --allow ota0_image,ota0_proof,otadata,ota_meta` (donc `ota_1` **égale à la référence** `46a5d06c…` et sa preuve `d1d5843c…`) ; `iap1_pack verify` sur la relecture : *authentic* ; `ota_meta` : `Written` pour B″ (`ota_1`, 1 050 112 o, `sha256:1a36e280…`), séquence 10 ; `/health` 200 (cert …3F) ; C toujours `Stable`, aucun redémarrage |
| **R** | restauration (§6), accès complet 30 min | voir §6 | état = `refs_after_p5c` |

## 5. Risque de perte du slot de repli, étape par étape
* **H1** : A″ (repli de B″) est détruite ; B″ reste `Valid` seq 2 et démarre. Coupure en cours : B″ démarre, `ota_0` sans preuve est refusé ; `plan-restore` rétablit A″.
* **H2** : repli = B″ (`otadata` seq 2 `Valid` conservée par `activate`). C ne confirme pas ⇒ rollback sur B″ (§6, cas 2).
* **H3 → N1** : après N1, **C est seule** (B″ détruite). C `Valid` démarre ; si elle échoue, `HALT` et récupération ROM. Fenêtre = de N1 à la fin de R. Elle est minimale dans la séquence (R suit N1 immédiatement, aucun autre essai).
* **C n'a jamais tourné sur la carte** : l'activation de C est le point de plus haut risque (premier rollback réel éventuel hors protocole).

## 6. Restauration déterministe (chargeur ROM, accès complet)

**Principe : restauration pilotée par la différence, idempotente.** Après tout incident : relecture des six plages (tag `p6_x`), puis `check_canonical.py plan-restore --current p6_x --reference p5c_r1 --out web/p91` : seuls les secteurs de 4 Kio qui diffèrent de la référence P5-C, dans les zones `ota_0` (image, preuve), `ota_1` (image, preuve), `otadata`, `ota_meta`, sont réécrits, depuis les relectures `p5c_r1` (empreintes complètes affichées pour chaque fichier). **Une différence dans le bootloader, la table, l'espace `0xA000–0x10000` ou `config` arrête le plan** : anomalie à présenter, pas à restaurer. Le plan est relancé à partir d'une nouvelle relecture après toute interruption : il ne peut que rétrécir. Aucune région conforme n'est écrite.

Plan attendu à la fin de la séquence (étape P comprise), simulé sur les octets de la carte : **`ota_1` n'y figure plus** (le témoin positif l'a rétablie à l'identique) ; restent `otadata` secteur 0 (`0x11000`), `ota_meta` (`0x13000`, 8 Kio, qui porte le `Written` de B″), `ota_0` en deux plages de 450 560 et 634 880 o et la preuve `0x1CF000`, soit 5 plages, 1 101 824 o. Si le témoin positif n'est pas exécuté ou échoue, le plan ajoute le secteur `0x2D0000` et la preuve `0x37F000` d'`ota_1`.
| Plage | Contenu de référence | Empreinte de référence |
|---|---|---|
| `0x37F000`, 4096 | preuve de B″ | `d1d5843caa241281ff82398041540cb2567e7a032a67525d468a0b604d35d7ad` |
| `0x1CF000`, 4096 | preuve d'A″ | `7d376f51bda8a69f0da2901ee01f75d4e9bcbc75adeac7050c75a3f5b23e1761` |
| `ota_0` (secteurs qui diffèrent) | A″ | zone entière `ota_0` `9677f1c836c5a53e2dcc90b2f1c154303139a3271c38fa1b7c5ead3929a858dc` |
| `ota_1` (secteur `0x2D0000`) | B″ | zone entière `ota_1` `46a5d06c2e6db70eb572c97fced63a8d05a32db8d3e2d498cff66b4f9a770a6e` |
| `0x11000–0x13000`, `0x13000–0x15000` | `otadata` [seq 1 Valid, seq 2 Valid], `ota_meta` séquence 5 | `0x10000–0x15000` `5dea577cd5bb548ebba56095ad27448af932e6653ba3c894ac8616ae1e0fffa0` |

**Ordre d'écriture** (à chaque instant, une application authentique de plus haute séquence reste démarrable) : 1) `ota_1` s'il figure au plan (image, preuve), 2) `ota_0` (image, puis preuve), 3) `otadata`, 4) `ota_meta`. Aucun démarrage avant relecture complète.

**Critère final** : relecture des six plages ⇒ `diff p5c_r1→p6_r --allow ''` PASS et `check_p5c.py flash --tag p6_r --state r1 --ref p5c_c1 --s0 p5c_s0` PASS ; `iap1_pack verify` *authentic* pour A″ et B″ sur les relectures ; `ota_meta` staged vide, actif = B″ ; puis **un** démarrage : `VERIFIED`, `plan slot=1 seq=2`, aucune écriture `otadata`, `running ota_1 status Stable`, `/health` 200 TLS vérifié (cert …3E) ; accès complet révoqué.

### Cas
| # | Situation | État probable | Conduite |
|---|---|---|---|
| 1 | échec pendant l'installation de C (H1) | B″ actif ; `ota_0` partiel, preuve vierge ; `ota_meta` inchangé ou `Written` | relecture ; `plan-restore` ; aucune réessai automatique |
| 2 | rollback spontané de C | `otadata` secteur 0 = seq 3 `Aborted` ; B″ `Stable` ; `ota_meta` vidé par B″ ; `ota_0` = C | documenter (capture), cause d'abord ; `plan-restore` (ota_0, preuve, otadata, ota_meta) ; ne pas rejouer |
| 3 | C `Stable`, B″ partiellement effacée par N1 (cas nominal ou N1 interrompue) | `ota_1` partiel/bouché, preuve vierge | `plan-restore` (ota_1 d'abord) ; C reste le seul candidat jusque-là |
| 4 | `Written` ou `Activating` résiduel | `ota_meta` ≠ référence | inclus dans le plan (`ota_meta`) ; ne jamais appeler `/ota/activate` pour « finir » |
| 5 | coupure pendant la restauration | secteurs mixtes | **pas de démarrage** ; nouvelle relecture ; nouveau plan (plus court) ; verdict `indeterminate` du banc : aucune console, aucun reset |

## 7. Critères

**PASS du contrôle #105** (tout à la fois) :
1. C démarrée et **confirmée** sous VerifiedBoot (capture `activate-c` conforme, relecture `otadata`/`ota_meta` conforme), puis stable après redémarrage avec `/health` 200 TLS vérifié.
2. Témoin positif **sur la carte** : après N1, C reçoit B″ canonique : 200 `staged`, `publishing`, `ota_1` = B″ exacte (égale à la référence), preuve *authentic* publiée, `ota_meta` `Written` pour B″, aucune activation, aucun redémarrage, C toujours `Stable` ; plus, sur l'hôte, la table du §2 (canonique acceptée par les deux règles). Ce témoin n'a de valeur que si N1 a été refusée dans les mêmes conditions : il est exécuté **seulement si N1 satisfait ses critères**.
3. N1 : signature RSA-PSS et digest valides (hôte *authentic* ; carte : `writing`, aucun `Signature Check Failed`), reçue **intégralement** (relecture ROM d'`ota_1` = `N1.bin` octet pour octet).
4. HTTP 409, **jamais** `publishing`, aucun redémarrage ; le 409 seul ne suffit pas.
5. Aucune publication : proof sector `0x37F000` vierge, `ota_meta` octet pour octet identique à `p6_h3` ; aucune région hors `ota_1` (image et preuve) modifiée pendant N1.
6. Attribution établie par la chaîne d'éliminations du §2 ; la limite d'observation directe est consignée.

**PASS de la campagne complète** : le contrôle #105 qualifié ; restauration vérifiée (critère final §6) ; empreintes de `refs_after_p5c.json` retrouvées pour les six plages ; B″ `Stable` sous VerifiedBoot #105 avec HTTPS vérifié ; A″ authentique ; accès complet révoqué.

**FAIL récupérable** : N1 acceptée (le contrôle n'est pas actif ou C n'est pas celle qu'on croit : arrêt et diagnostic), C non confirmée (rollback), état non conforme dont toutes les plages sont identifiées : `plan-restore`.
**INDETERMINATE** : `indeterminate` du banc, connexion HTTPS coupée sans réponse sur un upload (une coupure due au redémarrage de l'activation n'est pas un échec : on tranche d'après `ota_meta`, `otadata` et le journal), capture perdue, relecture incomplète, écart dans une zone non restaurable. Aucune action, relecture seule, jamais converti en PASS.

## 8. Variantes non retenues (décision possible)
* **T0 sur carte** (N1 envoyé depuis B″ : accepté par l'ancien code) : retiré ; remplacé par la démonstration hôte et l'élimination du §2.
* **N2 sur carte** : retiré ; hôte seulement.
* **Témoin positif par l'application C** : *retenu* dans la version finale (étape P).
* **Build instrumenté** (journal de la cause du refus) : attribution directe, mais code ≠ #105.

## 9. Différences avec le premier protocole
T0 et N2 retirés de la carte ; témoin positif ajouté après N1 (étape P, dernière opération OTA) ; séquence S0 → H1 → H2 → H3 → N1 → R ; état final imposé = P5-C exact ; restauration **différentielle et idempotente** au lieu de fichiers de slot complets ; contrôle explicite des mutations (`diff`, `refused-state`) ; tableau des régions modifiables ; correction de l'affirmation sur `main.rs` ; démonstration hôte étendue à la porte de publication et au `BadSize` du bootloader, avec versions et entrées figées ; limite d'attribution déclarée.
