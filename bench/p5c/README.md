# P5-C : qualification de la chaîne OTA positive sous VerifiedBoot (protocole, non exécuté)

**Statut : préparé, jamais exécuté sur la carte.** Aucune opération matérielle n'a été faite pour l'écrire. Exécution = décision explicite séparée.

Cible : `Written → Activating → PendingVerify → Stable`, avec B″ authentifiée par le bootloader #105 (`a7e884a2…`), et un redémarrage de persistance.

## 1. Revue du code réellement embarqué

A″ et B″ de la carte = `2e62840` + correctif SHA logiciel. Entre `2e62840` et la tête de #101 (`cc3a8a1`), le diff des crates `boot`, `update`, `bootauth/select`, `arch/esp32/firmware/a` et de `ota-system` ne touche que l'adaptateur SHA (`soft.rs`, `Cargo.toml`, `main.rs` pour son installation, `ota.rs` 6 lignes) : **les transitions lues ci-dessous sont celles de la carte.**

### Transitions (code lu)
| Étape | Qui | Code | Écriture |
|---|---|---|---|
| `/ota/activate` | A″ | `ota.rs` Handler → `OtaService::activate` (`service.rs`, `OtaService::activate`) : id, état `Staged`, `admit_activation` (enveloppe re-présentée), re-hachage du slot, `check_binding`, **`slot_bootable`** (`boot_proof::bootable` → `provision::gate` : relit la preuve, RSA-PSS, taille signée = taille annoncée) | aucune jusqu'ici |
| intention | A″ | `activate_staged` (`service.rs:108`) : `ota_meta` **Written → Activating** (1 trame), puis `SystemBoot::activate` | `ota_meta` (+1 trame) |
| entrée de boot | A″ | `boot::activate` (`boot/lib.rs`) : **une** entrée complète `seq 2, New`, dans le secteur qui ne porte pas le dernier `Valid` | `otadata` secteur 1 (erase, corps, mot de validation) |
| réponse | A″ | HTTP 200 `activating`, puis `reset_for_rollback` (200 ms, reset logiciel). Un échec avant → 409 sans reboot | — |
| sélection | bootloader | `plan_boot` : `New` candidat → image_ok(slot 1) (structure, preuve, RSA-PSS ROM, taille canonique #105) → écrit **`PendingVerify`** (seq 2) **avant** de sauter | `otadata` secteur 1 |
| réconciliation | B″ | `on_boot` : `Pending` + slot démarré = `staged.slot` + `Activating` ⇒ **AwaitConfirmation** (`reconcile`) ; sinon `RollbackUnaccounted` | — |
| auto-contrôle | B″ | `pending_check` : `ota.is_healthy()` ET `config.self_check()`, borne 15 s ; chien de garde armé 20 s dès le départ | — |
| confirmation | **B″ elle-même** (`boot_task`, `main.rs:151`) | `confirm_pending` → `SystemBoot::confirm` : `Pending → Valid` (seq 2), puis `watchdog_disable`, puis `finish_validation` | `otadata` secteur 1 ; `ota_meta` : promote (active = B″) puis clear (staged vide) = **2 trames** |
| service | B″ | `ota::serve` (Wi-Fi/HTTPS) : **après** la confirmation | — |

Journal attendu de la confirmation : bootloader `otadata write sector=… seq=0x2 state=0x1`, application `boot: running ota_1 status PendingVerify`, puis `boot: confirmed`. Au démarrage suivant : `status Stable`, `boot: stable`.

### Rollback (code lu)
* **Pas de confirmation / reset pendant `PendingVerify`** : au démarrage suivant, `plan_boot` écrit `Aborted` sur l'entrée Pending et démarre A″ (seq 1 `Valid`). A″ réconcilie `Activating` avec un slot différent ⇒ `ClearStale` : **`ota_meta` est vidé (`Written` perdu)**. Retenter exige donc de restaurer `ota_meta` (sauvegarde S0) ou un nouvel upload.
* **Auto-contrôle faux** : `reject` ⇒ entrée `Invalid`, reset, repli immédiat sur A″. **Auto-contrôle suspendu / panic / blocage** : le chien de garde de 20 s (le gestionnaire de panique boucle) réinitialise la puce, même chemin que sans confirmation.
* **B″ refusée par le bootloader** (preuve, signature, taille, structure) : entrée `Invalid`, A″ démarre, même nettoyage `ota_meta`.
* **Échec du `finish_validation` après `confirm`** : B″ est déjà `Valid` ; `FinishInterruptedActivation` rejoue promote/clear au démarrage suivant (statut `Degraded` ce jour-là).

### Démontré / établi par une campagne / hypothèse
| Propriété | Statut |
|---|---|
| Ordre des vérifications et des écritures, branche d'erreur 409 sans mutation | **code** + P5-A (carte, branche négative) |
| `bootable` négatif, upload authentifié, `Written` persistant | **carte** (P5-A, P5-B) |
| `Activating → PendingVerify → confirmé` par l'application B″ | **carte** M2-C, mais sous le bootloader **non signé** et avec métadonnées préparées à la main (pas via `/ota/activate`) |
| Sélection de B″ authentifiée par #105 (ROM RSA) | **carte** M4-bis, B″ `Valid` seq 2 écrite à la main (pas de `New → Pending`) |
| A″ authentifiée par VerifiedBoot | **carte** M4 (bootloader #101/`5ca06b7c`) ; sous **#105** : décision hôte seulement |
| `New → PendingVerify` puis `Valid` sous VerifiedBoot | **hôte** (pilote ci-dessous) ; **jamais sur carte** |
| `bootable` positif, `/ota/activate` réel, écriture réelle de `New` | **jamais exercés** (hypothèse fondée sur le code) |
| Rollback réel depuis `PendingVerify` | **hôte** ; sur carte M2-B a exercé le chemin `Rollback` (auto-rejet sans `Activating`), pas le chien de garde |

## 2. Compatibilité du bootloader #105

Pilote hôte `plan-driver/` : le vrai `select::plan` de `9ba9533` (RSA-PSS hôte, taille canonique incluse) sur les **octets réels relus de la carte** (A″, B″, preuves) :

```
S1  après W1 (A'' seule)           [seq1 Valid | blank]     -> BOOT slot 0 seq 1, aucune écriture
A1  après /ota/activate            [seq1 Valid | seq2 New]  -> BOOT slot 1 seq 2 ; écrit secteur 1 seq 2 PendingVerify
B1' reset avant confirmation       [seq1 Valid | seq2 Pend] -> BOOT slot 0 seq 1 ; écrit secteur 1 seq 2 Aborted
C1  B″ confirmée                   [seq1 Valid | seq2 Valid]-> BOOT slot 1 seq 2, aucune écriture
D4  B″ New, signature invalide     refused slot 1: BadSignature -> BOOT slot 0 ; écrit secteur 1 seq 2 Invalid
D2' entrée d'activation tronquée   [seq1 Valid | corrompue] -> BOOT slot 0 seq 1 (entrée ignorée)
D10 après rollback                 [seq1 Valid | seq2 Aborted] -> BOOT slot 0 seq 1
```
Reproduire : `git worktree add ../p105 9ba9533`, `sed "s#@P105@#$PWD/../p105#" plan-driver/Cargo.toml.template > plan-driver/Cargo.toml`, `cargo run --offline -- <ota_0 relu> <ota_1 relu> web/p91/p5a-B2-proof-badsig.bin`.

Compatibilités : A″ et B″ (1 050 112 o) ont taille canonique = taille signée (pilote : *authentic*, aucun `BadSize`) ; les formats `otadata` v2 et `ota_meta` OTSF/OTM1 en place sont ceux que lit #105 (même crates) ; la table est inchangée ; le `Written` de P5-B survit à un redémarrage (P5-B) et le bootloader ne lit pas `ota_meta`. La condition d'upload ajoutée par #105 n'intervient pas (aucun upload). **Aucune modification de firmware n'est nécessaire** : rien ne bloque.

### Ordre : bootloader avant ou après `/ota/activate` ?
**Avant (W1, puis A1).** Après l'appel, A″ redémarre en 200 ms et le bootloader en place démarre B″ : il n'existe aucune fenêtre pour remplacer le bootloader entre l'activation et le premier démarrage. Activer d'abord sous le bootloader non signé ferait confirmer B″ sans authentification (ce que M2-C a déjà fait) et ne qualifierait pas `New → Pending` sous VerifiedBoot ; remplacer le bootloader *pendant* `Pending` est impossible (ROM = reset ⇒ `Aborted`). Seul démarrage intermédiaire de la séquence W1-d'abord : **A″ sous #105 (S1)**, état cohérent (authentique, aucune écriture) et de toute façon nécessaire (A″ doit tourner pour servir `/ota/activate`). Son échec est récupérable (§7).

## 3. Séquence

Pré-requis opérateur : page Chrome au premier plan, **une seule page connectée**, `autoreset` off, accès **limité** au départ. **Entre A1 et « boot: confirmed » : ne toucher ni la page, ni le câble, ni le banc** (tout reset de la puce pendant `PendingVerify` provoque le rollback, §7 cas 6).

| Étape | Action | Accès | Preuve |
|---|---|---|---|
| S0 | `identify --stay` ; dump des 6 plages, tag `p5c_s0` ; `check_p5c.py prepare --s0 p5c_s0` ; `check_p5c.py flash --tag p5c_s0 --state s0 --s0 p5c_s0` ; `iap1_pack verify` A″/B″ sur les relectures ; `/health` 200 TLS vérifié (cert A″ …3F) ; file vide, pas d'indéterminé ; manifestes figés | limité | PASS de tous |
| **W1** | `write p91/boot-verified-uart0-sha216-104.bin@0x0 --stay --ttl 60`  — **unique écriture ROM de la campagne** | complet 30 min | `completed` + MD5 chargeur |
| W1-relecture | dump 6 plages tag `p5c_w1` ; `check_p5c.py flash --tag p5c_w1 --state s1 --s0 p5c_s0` | complet | préfixe 34 976 o = image, `0x88A0–0x9000` = FF, `0x9000–0xA000` inchangé, tout le reste = référence ; **révoquer l'accès complet** |
| S1 | `mark P5C-S1` ; `console` (un seul démarrage) ; `check_p5c.py capture --file bench/logs/serial.log --from P5C-S1 --stage s1` ; `/health` 200 | limité | `VERIFIED`, `slots checked`, `plan slot=0 seq=1`, aucune écriture `otadata`, `running ota_0 status Stable` |
| **A1** | `mark P5C-A1` ; **un seul** `activate_client.py` (`p5-b2-20261101`, enveloppe `B2/app.env`) | limité | HTTP **200 `activating`** ; zéro `Signature Check Failed` |
| B1 + C1 | **ne rien faire** ; attendre `OTA ready` (≤ 60 s) ; `check_p5c.py capture … --from P5C-A1 --stage a1b1` | limité | `activating … reboot requested` → `VERIFIED` → `plan slot=1 seq=2` → `otadata write sector=1 seq=2 state=1` → `image ok` → `jump` → `running ota_1 status PendingVerify` → `boot: confirmed` → `OTA ready` ; ni refus, panic, HALT, watchdog |
| C1-relecture | `identify --stay` (reset vers la ROM ; B″ est déjà `Valid`) ; dump tag `p5c_c1` ; `check_p5c.py flash --tag p5c_c1 --state c1 --s0 p5c_s0` ; `iap1_pack verify` | limité | `otadata` = [seq 1 Valid, seq 2 Valid] octet pour octet ; `ota_meta` : séquence S0+3, `staged` vide, `active` = B″ (digest + id) ; A″, B″, preuves, `config` inchangés |
| R1 | `mark P5C-R1` ; `console` (redémarrage de persistance) ; `check_p5c.py capture … --from P5C-R1 --stage r1` ; `/health` 200 TLS vérifié (cert B″ …3E) | limité | `VERIFIED`, `plan slot=1 seq=2`, **aucune** écriture, `running ota_1 status Stable`, `boot: stable` |
| R1-relecture | `identify --stay` ; dump tag `p5c_r1` ; `check_p5c.py flash --tag p5c_r1 --state r1 --ref p5c_c1 --s0 p5c_s0` ; puis `console` final (B″ `Stable`) | limité | `0x10000–0x15000` identique à `p5c_c1` |
| Clôture | nouvelle référence `refs_after_p5c.json` (hachages seuls), rapport dans #101, accès complet révoqué | — | — |

`PendingVerify` n'est pas observable dans la flash (le bootloader saute dans la foulée, B″ confirme en quelques secondes) : il est établi par le **couple** (écriture bootloader `state=1` dans la capture) + (`status PendingVerify` de l'application, qui n'existe que si l'état lu est `Pending` et `Activating` concorde) + (état final `Valid`). `Stable` n'est déclaré qu'après **confirmation** (`boot: confirmed`, relecture `otadata` seq 2 `Valid`) **et** redémarrage de persistance (`boot: stable`).

### Écritures flash
| Qui | Quoi | Nombre |
|---|---|---|
| ROM (banc, accès complet) | bootloader `0x0` (W1) | **1** |
| Application A″ | `ota_meta` trame Activating ; `otadata` secteur 1 (New) | 2 |
| Bootloader #105 | `otadata` secteur 1 (PendingVerify) | 1 |
| Application B″ | `otadata` secteur 1 (Valid) ; `ota_meta` promote, clear | 3 |

## 4. Références et plages

Départ : `bench/w0/refs_after_p5b.json` (commit `840a213`) ; empreintes complètes des artefacts : `refs.json`. `otadata` S0 `1170d1af5a2bef83547dcd22f3faf993f5255c79eae16318a73478a13af1a0bc`, `ota_meta` S0 `86f719feb4ca0479e93b508a17121e6586cbe68c88644f31660da9fcfc0b9991`. Bootloader #105 : 34 976 o, `a7e884a2d0057865565e51e7665ebaa497882f3a7f4f4cb3129878ef318667d1`, plage `0x0–0x88A0` (l'effacement s'arrête à `0x9000`). Attendu après PASS : bootloader = W1 ; `0x10000–0x15000` = (table, `otadata` [seq1 Valid, seq2 Valid], `ota_meta` séquence 5) ; le reste inchangé. **Aucune de ces plages ne sert à restaurer automatiquement après PASS.**

## 5. Critères

* **PASS** (tout à la fois) : W1 `completed` + MD5 + relecture SHA-256 ; S1 conforme ; A1 200 `activating` sans échec de signature ; capture `a1b1` conforme ; relecture C1 conforme ; R1 conforme (capture, `/health` 200 TLS vérifié, relecture identique) ; B″ `Stable` sous VerifiedBoot, A″ intacte et authentique (secteur 0 `Valid` seq 1).
* **FAIL récupérable** : état non conforme dont toutes les plages sont relues et identifiables (refus, rollback, `Aborted`/`Invalid`, A″ revenue `Stable`, `HALT` bootloader avec ROM accessible).
* **INDETERMINATE** : tout verdict `indeterminate` du banc pendant W1, page rechargée ou port perdu pendant une écriture, capture perdue entre A1 et la confirmation, relecture incomplète. Aucun reset, aucune console, aucun nouveau démarrage avant relecture ; ne jamais convertir en PASS.
* Arrêt de la campagne : au premier écart. Aucun second `/ota/activate` automatique (le client refuse via son marqueur).

## 6. Risques (aucune panne n'est provoquée)

| # | Interruption | État flash probable | Comportement attendu | Preuves | Récupération | Arrêt |
|---|---|---|---|---|---|---|
| 1 | Coupure après W1, avant A1 | bootloader #105, `otadata` S0 | A″ démarre sous VerifiedBoot, aucune écriture | capture `s1` | rien à restaurer ; poursuivre sur nouvelle décision | si S1 non conforme |
| 2 | Coupure pendant l'écriture `ota_meta` (Activating) | une trame valide (ancienne `Written` ou nouvelle `Activating`) ; `otadata` S0 | A″ démarre ; si `Activating` : slot ≠ ⇒ `ClearStale` | relecture `0x13000–0x15000` | restaurer `ota_meta` (S0) par ROM puis nouvelle décision | oui |
| 3 | Coupure après `Activating`/`New`, avant le premier démarrage | `otadata` [1 Valid, 2 New] | au démarrage : B″ vérifiée, `Pending`, nominal | capture B1 | nominal ; sinon cas 4–6 | oui si refus |
| 4 | B″ rejetée au démarrage | `otadata` secteur 1 `Invalid` ; `ota_meta` vidé par A″ | repli sur A″ `Stable` | `boot: slot 1 refused, reason N` | ROM : `otadata` (secteur 1 vierge) + `ota_meta` depuis S0 ; ne pas rejouer sans décision | oui |
| 5 | B″ démarre sans confirmer (auto-contrôle faux/suspendu, panic) | `Pending` puis `Aborted`/`Invalid` | reset (≤ 20 s) puis A″ ; `ota_meta` vidé | `status Rollback` ou absence de `confirmed`, `rst` watchdog | idem 4 | oui |
| 6 | Reset pendant `PendingVerify` | `Pending` | `Aborted` au démarrage suivant, A″ | `otadata write … state=4` | idem 4 | oui |
| 7 | Perte réseau après activation | confirmation faite avant le Wi-Fi ⇒ flash conforme | B″ `Valid` | UART `confirmed` ; `/health` KO | relire (lecture seule) ; si flash conforme, ne pas conclure PASS tant que `/health` n'est pas 200 TLS vérifié (une relance Wi-Fi par console est un redémarrage de plus : décision) | verdict « incomplet » |
| 8 | Web Serial interrompu pendant une **relecture** | aucune écriture | lecture refaite | trace banc | refaire la relecture (autorisé, lecture seule) | non |
| 9 | Timeout du banc pendant W1 | `0x0–0x9000` inconnu | `indeterminate` : pas de console, pas de reset | relecture `0x0–0xA000` | classer : image W1 / non signé de référence / partiel ; réécrire le même fichier = nouvelle décision ; **jamais** de démarrage avant | oui |
| 10 | Métadonnées v2 partiellement écrites | entrée sans mot de validation ⇒ `corrupt` ignorée ; deux entrées inutilisables ⇒ `HALT UnknownMetadata` | A″ démarre si l'autre entrée est acceptée (pilote D2′), jamais de réensemencement | capture, relecture `otadata` | ROM : restaurer `otadata` (S0) | oui |

## 7. Restauration (ROM, indépendante de l'application)

Matériel conservé : bootloader non signé `p91/p3d-restore-0x0-0xa000.bin` (40 960 o, `bd7c4e20…`) ; bootloader #105 (`a7e884a2…`) ; relectures S0 (`otadata`, `ota_meta`, A″, B″, preuves, `config`) ; `m2-B2-proof.bin` ; images figées (`MANIFEST` 17/17) ; configuration EKV = plage `config` de S0 (`8b911b67…`).
Chaque restauration = écriture ROM (accès complet), MD5, relecture SHA-256, jamais de démarrage entre deux.
* **R-A (état après W1/S1 ou repli)** : `0x0–0xA000` ← non signé ; `0x11000–0x15000` ← S0 si `otadata`/`ota_meta` ont changé.
* **R-B (retenter P5-C après repli)** : `0x11000–0x13000` ← S0 (`otadata`), `0x13000–0x15000` ← S0 (`ota_meta`), bootloader inchangé.
* Après PASS : pas de restauration automatique ; état final cible = B″ `Stable` sous VerifiedBoot, A″ conservée en `Valid` seq 1.

## 8. Limites connues avant exécution
* Le rollback réel (chien de garde, `Aborted`) n'est pas exercé : la capacité de repli est établie par les données (secteur 0 `Valid`, A″ authentique) et le pilote hôte, pas par un rollback sur carte (une campagne distincte, nécessitant une image qui échoue son auto-contrôle).
* A″ sous #105 : première fois sur carte en S1.
* Les images A″/B″ n'ont pas la condition d'upload de #105 (sans effet ici).
* `/health` de B″ présente un autre certificat (…3E) : le contrôle TLS se fait contre la même CA.
