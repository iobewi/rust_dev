# Qualification du rollback automatique sous VerifiedBoot #105 — protocole (non exécuté)

**Statut : préparé, aucune opération sur la carte.** L'exécution exige une autorisation distincte. Branche : `bench/rollback-preparation`. Les images (qui embarquent les paramètres Wi-Fi du banc) ne sont jamais dans Git : seules leurs empreintes le sont.

## 1. Revue des transitions réelles (code de `9ba9533`, identique à celui de la carte pour ces chemins)

| Étape | Qui / où | Écriture, dans l'ordre |
|---|---|---|
| Choix de la cible | `boot::update_target` : slot opposé à la plus haute séquence `Valid`/`Pending` (seq 2 → slot 1 ⇒ cible `ota_0`) ; `SystemBoot::activate` refuse si une entrée `Pending` existe | aucune |
| Upload de D | `Handler::upload` (voir `bench/canonical/README.md` §1) | preuve de `ota_0` effacée → image effacée par blocs de 64 Kio puis écrite → preuve écrite → `ota_meta` `Written` |
| `/ota/activate` | `OtaService::activate` : `bootable` (relit la preuve, RSA-PSS) → `ota_meta` **Activating** → `boot::activate` : **une** entrée `seq 3 New` dans le secteur **qui ne porte pas le dernier `Valid`** (secteur 0, qui portait l'entrée d'A″ seq 1) ; l'entrée de B″ (secteur 1) n'est pas touchée | `ota_meta`, `otadata` secteur 0 ; réponse 200 `activating`, reset logiciel 200 ms plus tard |
| Premier démarrage de D | bootloader #105 : `plan_boot` → candidat `New` → `image_ok` (structure, preuve RSA-PSS ROM, longueur canonique) → écrit **`PendingVerify` avant de sauter** | `otadata` secteur 0 (seq 3 `PendingVerify`) |
| D sans confirmation | `on_boot` : `Pending` + slot démarré = `staged.slot` + `Activating` ⇒ `AwaitConfirmation`, chien de garde nourri (20 s, depuis le démarrage) ; `pending_check` = `select(self_check, Timer 15 s)` ; `self_check` suspendu (feature `hang-self-check`) ⇒ **timeout ⇒ `PendingCheckAction::Reset` ⇒ `reset_for_rollback` : 200 ms puis `software_reset`** | aucune |
| Reset | **le timeout seul ne fait pas de rollback** : il n'y a rollback que parce que l'application fait elle-même le reset ; sans reset l'entrée resterait `Pending` indéfiniment (jusqu'au chien de garde à ≈ 20 s après son dernier nourrissage, filet de sécurité matériel) | — |
| Traitement du `Pending` au démarrage suivant | `plan_boot` étape 1 : **tout `Pending` devient `Aborted`** (écrit avant toute décision), puis candidats `Valid` par séquence décroissante : B″ seq 2, `image_ok(1)` (évaluation paresseuse : D n'est pas examinée) | `otadata` secteur 0 (seq 3 `Aborted`) ; **aucune écriture dans le secteur 1** |
| B″ réconcilie | `on_boot` : plus de `Pending`, une entrée `Valid` ⇒ `Confirmed` ; `staged` = `Activating` pour `ota_0` ≠ slot démarré `ota_1` ⇒ **`ClearStale`** ⇒ `ota_meta` vidé ; `Stable`, `boot: stable` | `ota_meta` (+1 trame) |
| Aucune image authentique | `Halt::NoUsableEntry` (toutes les entrées rejetées) ; `Halt::UnknownMetadata` (métadonnées illisibles) ; récupération par le chargeur ROM | — |

**Synchronisation `otadata` / `ota_meta`** : il n'y en a pas de directe. `otadata` appartient au bootloader et à l'adaptateur ; `ota_meta` à l'application, qui le réconcilie à chaque démarrage avec ce que le bootloader a décidé. Après un rollback, c'est B″ qui supprime la transaction `Activating` de D ; D n'est jamais promue (`active_*` reste celui de B″).

Précédent matériel : la campagne #68 a qualifié `hang-self-check` et `reject-self-check` (`PendingVerify` → reset à 15,2–15,3 s → image précédente `Stable`) **sous le bootloader non signé et avec des métadonnées EKV** ; rien de tel n'a été exercé sous VerifiedBoot #105, ni avec le flux OTA signé et les preuves IAP1. C'est la nouveauté de cette campagne.

## 2. Application D
* **Code** : `ota-system` de `9ba9533` (#105), construit avec la feature **déjà existante** `hang-self-check` du firmware (`--fault hang-self-check`, `prepare.py`) : l'auto-contrôle ne se termine jamais. Un seul ajout, de trace, dans la branche `hang-self-check` (`d-hang-trace.patch`, une ligne) : `rollback test image: self-check hangs on purpose, this image never confirms`. Aucune modification du bootloader, de #101 ni de #105 (l'arbre est une exportation à part).
* **Propriétés** : image ESP32 valide (taille canonique = taille signée), `signed-rsa` + boot `verified`, preuve IAP1 produite par la clé du banc : **cryptographiquement valide** ; elle démarre en `PendingVerify`, n'appelle jamais `confirm`.
* **Reset** : par le chemin existant du timeout (§1), logiciel, à ≈ 15,3 s après la ligne `PendingVerify` (valeur de #68 : 15,2–15,3 s). Pas de corruption de mémoire ni de flash.
* Construction reproductible : `build_d.sh` (secrets et paramètres réseau par l'environnement, jamais dans Git).

| Artefact (gelé hors dépôt) | Octets | SHA-256 complet |
|---|---|---|
| `D.bin` | 1 051 232 | `c602e6d5a05d50229b207713202141dee81597516db195f3b63ced7dc43d46f6` |
| `D.env` | 614 | `a62bc5bb4cbb56a8c5292f3ecda8b8b4ba1373e8519b28a8afc1ba19cd8dbdc1` |
| `D.iap1` | 4096 | `3c9eb8f2a6ae9b3e22a3e68a433b6745749dc9e8e016b4bf0115070978c9fe2b` |
| `D.elf` (symboles) | — | `f808c0be2c52bfbf2d637e21561f963ec9dc23ada95f181f4ce70558c7a07bca` |
| `B2.bin` / `B2.iap1` (repli, référence) | 1 050 112 / 4096 | `1a36e280eabd4610677c0ae14fcfe01b1824c0655c0f887db08dc0d3c138a5f2` / `d1d5843caa241281ff82398041540cb2567e7a032a67525d468a0b604d35d7ad` |

`check_rollback.py prepare --frozen DIR` : manifeste 8/8, preuve de D *authentic* sur 1 051 232 octets exactement, chaîne de trace présente dans D et absente de C et de B″.

## 3. Topologie A/B et perte temporaire d'A″ (modèle hôte, vrais validateurs et vrais octets)
`rollback-model/` rejoue tout le scénario avec le vrai `boot::activate`, le vrai `select::plan` de #105 (RSA-PSS, longueur canonique) et le vrai `boot_action` ; les octets de l'entrée de B″ sont comparés après **chaque** étape :
```
P5-C state          otadata [seq1 Valid | seq2 Valid]
PASS activate writes ONE entry, sector 0, seq 3 New (the sector that does not hold the last Valid)
PASS B'' entry (sector 1) byte for byte unchanged by the activation
PASS bootloader #105 authenticates D and selects slot 0 seq 3, no refusal  /  planned write: sector 0 seq 3 PendingVerify
PASS application D: Pending + Activating + same slot => AwaitConfirmation
PASS after the reset the bootloader authenticates B'' and selects slot 1 seq 2 (D is not examined)
PASS planned write: sector 0 seq 3 Aborted (the only write)   /  B'' entry byte for byte unchanged by the rollback
PASS application B'': staged Activating(ota_0) while running ota_1 => ClearStale
PASS next boot: B'' again, no write, D never started again
branches : reset too early (still Pending) => Aborted + B'' ; Aborted write torn => entry ignored, B'' boots ;
           D without proof => ProofAbsent, entry Invalid, B'' boots ; B'' without proof after rollback => Halt(NoUsableEntry)
```
Garanties demandées avant toute écriture :
1. **B″ reste le repli `Valid` et authentique** : `otadata` secteur 1 seq 2 `Valid` jamais modifié ; sa preuve et son image (`ota_1`) ne sont pas dans la zone écrite par l'upload (`ota_0`).
2. **Le bootloader peut sélectionner B″ si D est `Aborted`** : modèle ci-dessus, avec la vraie vérification RSA-PSS de B″.
3. **L'activation de D n'altère pas l'entrée de B″** : `boot::activate` écrit dans le secteur ne portant pas le dernier `Valid`.
4. **D n'écrase aucune preuve ni métadonnée nécessaire à B″** : l'upload touche `ota_0` `[0x20000, 0x130000)`, sa preuve `0x1CF000` et `ota_meta` (que B″ nettoie lui-même).
5. **Récupération ROM disponible** : indépendante de l'application et du bootloader (DTR/RTS du banc), exercée à chaque campagne.
**Contrepartie assumée** : dès H1, **A″ est détruite** et B″ est la **seule** application authentique, jusqu'à la restauration finale. Si B″ échouait pendant cette fenêtre, il n'y aurait plus de candidat (`Halt::NoUsableEntry`) : récupération par le chargeur ROM depuis les sauvegardes (§7). Aucune autre séquence ne préserve A″ : l'OTA n'écrit que le slot inactif, et le slot actif est B″.

## 4. Séquence matérielle (un point d'arrêt après chaque étape ; aucun accès complet avant la restauration)
Tags `p7_*`. Départ = référence `bench/p5c/refs_after_p5c.json` (tag `p5c_r1`). Vérifications avec `check_canonical.py` (`diff`, `accepted-state`, `plan-restore`) et `check_rollback.py`.

| Étape | Action | Régions pouvant changer | Contrôles (arrêt à l'écart) |
|---|---|---|---|
| **S0** | relecture `p7_s0`, `/health` 200 TLS vérifié (cert B″), A″ et B″ *authentic*, file vide, autoreset off, une page, `prepare` (manifeste, preuve de D), `rollback-model` PASS, `plan-restore p7_s0→p5c_r1` vide | aucune | tout égal à `refs_after_p5c` |
| **H1** | upload de D (`D.bin` + `D.env`, id `p7-d`) → `ota_0`, depuis B″ | `ota_0` `[0x20000,0x130000)`, preuve `0x1CF000`, `ota_meta` (+1 trame) | 200 `staged`, `writing`+`publishing` ; relecture `p7_h1` ; `accepted-state --before p7_s0 --after p7_h1 --image D.bin --proof D.iap1 --slot ota_0 --id p7-d` ; `diff … --allow ota0_image,ota0_proof,ota_meta` ; **`otadata` secteur 1, `ota_1` et sa preuve identiques**. A″ est perdue |
| **H2→H4** (continu, **ne rien toucher**) | `mark` ; **un** `/ota/activate` (`p7-d`, `D.env`) ; attendre `OTA ready` de B″ (≈ 30 s) | `ota_meta` (+2 trames), `otadata` secteur 0 (New → Pending → Aborted) | 200 `activating` ; `check_rollback.py capture --stage rollback` : `activating` → `VERIFIED` → `plan slot=0 seq=3` → `otadata write sector=0 seq=3 state=1` → `running ota_0 status PendingVerify` → trace de D → **pas de `confirmed`** → `rst:0x3` (logiciel) **14,5–16,5 s** après `PendingVerify` → `VERIFIED` → `plan slot=1 seq=2` → `otadata write sector=0 seq=3 state=4` (Aborted) → `running ota_1 status Stable` → `boot: stable` → `OTA ready` ; D ne redémarre pas ; ni refus, panic, HALT ni reset de chien de garde |
| **H5** | `/health` 200 TLS vérifié ; relecture `p7_h5` (entrée en ROM : le rollback est terminé) | aucune | `check_rollback.py state --before p5c_r1 --tag p7_h5 --image D.bin --proof D.iap1` : `ota_0` = D (preuve intacte), `otadata` [seq 3 `Aborted` \| seq 2 `Valid`] octet pour octet, `ota_meta` séquence S0+3, `staged` vide, actif = B″ ; `diff p5c_r1→p7_h5 --allow ota0_image,ota0_proof,otadata,ota_meta` (bootloader, table, `config`, `ota_1`, preuve de B″ égaux) ; `iap1_pack verify` B″ *authentic* |
| **H6** | un redémarrage de persistance (`console` depuis la ROM) | aucune | `capture --stage b-stable` : B″ `Stable`, aucune écriture `otadata`, D non démarrée ; `/health` 200 |
| **R** | restauration (§7), accès complet 30 min | voir §7 | six plages = `refs_after_p5c` ; B″ `Stable` ; accès révoqué |

Le reset est **applicatif** : aucun reset du banc ne doit survenir entre H2 et `OTA ready` ; une entrée en ROM à ce moment interromprait le scénario (un reset pendant `PendingVerify` donne le même `Aborted`, mais hors du chemin à qualifier).

## 5. Écritures et régions (à chaque étape)
| Qui | Écriture | Étape |
|---|---|---|
| Application B″ | preuve `ota_0` (effacement), `ota_0` (blocs de 64 Kio), preuve, `ota_meta` `Written` | H1 |
| Application B″ | `ota_meta` `Activating` ; `otadata` secteur 0 `seq 3 New` | H2 |
| Bootloader | `otadata` secteur 0 `PendingVerify` | H3 |
| Bootloader | `otadata` secteur 0 `Aborted` | H4 |
| Application B″ | `ota_meta` vidé (ClearStale) | H4 |
| Chargeur ROM (banc) | 4 plages de restauration | R |
Jamais écrits : bootloader, table, `config`, `ota_1`, preuve de B″, `otadata` secteur 1.

## 6. Risques
* **Perte de la redondance** (§3) entre H1 et R : B″ seule ; récupération ROM.
* **Chien de garde / reset** : le chemin nominal est logiciel (15,3 s, filet matériel à ≈ 20 s). Un `rst:0x7/0x8` signalerait un autre chemin : écart à arbitrer, pas de répétition.
* **D ne démarre pas** (refus, ex. `ProofAbsent`/`BadSize`) : entrée `Invalid` (pas `Aborted`), B″ démarre ; ce n'est pas le scénario visé ⇒ FAIL récupérable.
* **Un reset du banc pendant H2→H4** invalide la démonstration.
* Les images contiennent des paramètres sensibles : conservées hors Git.

## 7. Restauration exacte de P5-C (ROM, différentielle, idempotente)
`check_canonical.py plan-restore --current p7_x --reference p5c_r1 --out web/p91` : seuls les secteurs qui diffèrent de la référence, dans `ota_0`, `ota_1`, `otadata`, `ota_meta`, sont écrits ; tout écart ailleurs (bootloader, table, `config`) arrête le plan. À l'état attendu après le rollback (simulé sur les octets de D) : **4 plages, 1 105 920 o** — `ota_0` `[0x20000, 0x12A000)` (A″, 1 089 536 o), preuve d'A″ `0x1CF000` (`7d376f51bda8a69f0da2901ee01f75d4e9bcbc75adeac7050c75a3f5b23e1761`), `otadata` secteur 0 `0x11000` (seq 1 `Valid`), `ota_meta` `0x13000` (8 Kio, séquence 5). Empreintes de zone attendues après : `ota_0` `9677f1c836c5a53e2dcc90b2f1c154303139a3271c38fa1b7c5ead3929a858dc`, `0x10000–0x15000` `5dea577cd5bb548ebba56095ad27448af932e6653ba3c894ac8616ae1e0fffa0`. Ordre : `ota_0`, sa preuve, `otadata`, `ota_meta` ; chaque écriture : MD5 chargeur + relecture SHA-256 ; **aucun démarrage avant relecture complète**. Critère final : `diff p5c_r1→p7_r` sans écart, `check_p5c.py flash --state r1`, A″ et B″ *authentic*, puis un démarrage `Stable` (`check_p5c.py capture --stage r1`) et `/health` 200 TLS vérifié, accès révoqué.

| Cas | État probable | Conduite |
|---|---|---|
| D ne démarre pas | `otadata` secteur 0 `Invalid` ou `New` ; B″ `Stable` ; `ota_meta` nettoyé par B″ | capture ; ne pas rejouer ; `plan-restore` |
| D redémarre trop tôt (panic, reset externe) | `Pending` → `Aborted`, même résultat | documenter l'écart de chemin et de délai ; `plan-restore` |
| B″ ne démarre pas après le rollback | `HALT` `NoUsableEntry` possible, ROM accessible | **aucune réécriture sans relecture** ; restaurer `ota_1` (+ preuve) puis `otadata` depuis `p5c_r1` |
| Rollback partiel (coupure après l'écriture de `Aborted`, entrée tronquée…) | entrée ignorée comme corrompue : B″ démarre (modèle hôte) | relecture, `plan-restore` |
| Coupure pendant la restauration | secteurs mixtes | pas de démarrage ; nouvelle relecture ; nouveau plan (plus court) ; verdict `indeterminate` du banc : aucune console, aucun reset |

## 8. Critères
**PASS** (tout à la fois) : D (image authentique, preuve valide) acceptée par l'OTA et **effectivement démarrée** (trace de D) ; `PendingVerify` démontré par le bootloader et l'application ; **absence réelle de confirmation** (aucun `boot: confirmed`, `otadata` jamais `Valid` pour D) ; **reset observé** (logiciel, 14,5–16,5 s) ; **transition vers `Aborted`** (journal du bootloader et relecture `otadata` seq 3 `Aborted`) ; B″ authentifiée par VerifiedBoot et **`Stable`** ; métadonnées cohérentes (`ota_meta` vidé, B″ active) ; relecture indépendante des régions critiques ; persistance (H6) ; **restauration exacte de P5-C** (six plages) ; HTTPS vérifié ; accès complet révoqué. Un redémarrage réussi ne suffit pas.
**FAIL récupérable** : comportement non conforme (D refusée, reset par chien de garde, B″ non `Stable`…) avec un état flash identifié : `plan-restore`.
**INDETERMINATE** : état matériel ou métadonnées inconnus, `indeterminate` du banc, capture perdue entre l'activation et `OTA ready`, relecture incomplète : relecture seule, aucune action, jamais converti en PASS.

## 9. Tests hôte (cette branche)
Node 57/57 ; Python 78/78 (dont 10 pour le vérificateur de rollback) ; `rollback-model` PASS (modèle ci-dessus) ; `check_rollback.py prepare` PASS ; plan de restauration simulé sur les octets de D : 4 plages.

## 10. Limites
Le chemin watchdog matériel (boucle bloquant l'exécuteur) n'est pas qualifié ici ; un seul scénario de non-confirmation (timeout coopératif) ; ESP32-S3 uniquement ; modèle hôte réplique la règle `image_outcome` (3 lignes) de l'adaptateur ESP, qui n'est pas appelable hors cible.
