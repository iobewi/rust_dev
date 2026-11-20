# Recette matérielle — où se situent les ~20,9 s de la lecture flash USB OTG du P4

**NON EXÉCUTÉE.** Cette recette n'a pas été lancée et ne doit pas l'être sans l'accord explicite de l'opérateur pour la carte, le scénario et la version exacte du binaire.
Elle ne modifie jamais la flash NOR et n'écrit aucun réglage persistant de la carte.

## Question et références
Lecture de 1 Mio à `0x8000`, paquets de 4 Kio, USB OTG CDC : environ 23,1 s avec le lecteur standard A, environ 2,175 s avec le lecteur D (MD5 calculé par le stub, sans transfert).
L'écart d'environ 20,9 s doit être réparti entre : lecture flash, calcul MD5, encodage SLIP et émission, `esp_rom_cdc_acm_fifo_fill()`, et attente des ACK.

## Périmètre
| Élément | Valeur |
| --- | --- |
| Carte | ESP32-P4 en révision ≥ 3.00 (celle du banc est v3.2) ; le chargeur refuse toute autre |
| Région | adresse `0x8000`, 1 048 576 octets, paquets de 4 096 octets |
| Transport | USB OTG CDC (pas USB Serial/JTAG ni UART) |
| Mode | lecture seule ; RAM uniquement pour le stub diagnostique |
| Binaire | image instrumentée construite par `build_stub.py` à partir de `esp-flasher-stub v1.2.2` + `esp-stub-lib d61983fa…` ; SHA-256 du JSON **fournis par l'opérateur** |

## Prérequis, tous vérifiés avant de commencer
1. **Accord écrit de l'opérateur** nommant la carte (`esp32-local`), le scénario (cette recette), et le SHA-256 exact du JSON et de l'ELF instrumentés.
2. Le dossier de preuves du build (`build-manifest.json`, `static-qualification.json`) indique `qualified_statically: true` ; ses SHA-256 correspondent à ceux de l'accord.
3. Tous les tests de `QUALIFICATION.md` section 7 passent sur le commit utilisé ; le commit a été relu.
4. Le mode diagnostic est **intégré** à IDLab (commande `diagnostic.p4_usb_trace`, `QUALIFICATION.md` section 6) **dans la version réellement servie** : ce n'est pas le cas du serveur actuellement déployé ; son déploiement exige l'accord explicite de l'opérateur.
5. Réservation de la carte par le client, grant local donné par l'opérateur, aucune autre session ouverte, carte non en quarantaine, aucune opération en attente.
6. Un **nouveau** dossier de preuves, vide, par exécution ; les anciens ne sont jamais réutilisés ni modifiés.

## Séquence (une seule fois, sans relance automatique)
Chaque étape est précédée d'une vérification de l'état (réservation, grant, session) ; un écart arrête la recette.

1. **A1 — lecteur standard.** `flash.read`, `reader: reference`, région ci-dessus. Relever durée, MD5 et SHA-256 des données.
2. **D — MD5 seul.** `flash.read`, `reader: md5-only`, même région. Relever durée et MD5 (aucune donnée transférée).
3. **T — lecteur instrumenté**, par la commande `diagnostic.p4_usb_trace`. Gestes **de l'opérateur**, dans cet ordre : BOOT + reset sur le P4 ; **retirer la carte de la page, puis la rajouter avec l'option avancée « ROM seule (diagnostic P4) »** (une connexion normale recharge le stub standard et rend T impossible) ; armer le grant local. IDLab ne fait aucun reset. Le consentement dédié est donné pour la carte, ce SHA-256 et cette région, à usage unique.
   Relever durée, MD5, SHA-256 des données, le rapport IDT1 (cycles bruts, fréquence supposée indiquée comme telle) et les identifiants d'opération.
4. **Retour au fonctionnement normal.** La carte reste en quarantaine (`diagnostic_resident`) : seule `mark` est acceptée. L'opérateur fait BOOT + reset, **reconnecte en « ROM seule »**, puis clique « Récupération » : l'agent resynchronise la ROM sur cette connexion (sans reset, sans fermer le port) et ne lève la quarantaine que si aucun stub ne répond. Aucun reset automatique.
5. **A2 — lecteur standard à nouveau**, après ce retour (reconnexion **normale**, option « ROM seule » décochée), pour vérifier la stabilité. Même mesure que A1.

L'ordre A1, D, T, A2 est conservé. Si A1 et A2 diffèrent de plus de 10 %, aucune conclusion n'est tirée avant répétition.

## Si quelque chose ne va pas
Arrêt immédiat, conservation des preuves, aucune reconnexion ni rejeu. Toute erreur après le début du flux de T est `sessionIndeterminate` :
la session doit être fermée par l'opérateur. Un échec du chargement RAM avant `memBegin` laisse la carte intacte ; après, la reprise se fait par le reset ROM standard, avec l'accord de l'opérateur.
Les résultats indéterminés restent indéterminés.

## Preuves à conserver (dossier neuf, hors Git)
- le fichier `evidence.json` au schéma `idlab-p4-usb-trace-evidence/1` ci-dessous ;
- les journaux bruts de la console et de l'agent, le manifeste du build, les SHA-256 du JSON et de l'ELF chargés ;
- la révision de la puce lue, la version du serveur et de l'interface, l'identité du navigateur.

```json
{"schema": "idlab-p4-usb-trace-evidence/1",
 "region": {"address": 32768, "size": 1048576, "packet_size": 4096},
 "runs": [
  {"label": "A1", "duration_ms": 0, "md5": "", "sha256": ""},
  {"label": "D",  "duration_ms": 0, "md5": ""},
  {"label": "T",  "duration_ms": 0, "md5": "", "sha256": "", "trace": {"flash_cycles": "", "md5_cycles": "", "send_cycles": "",
        "ack_cycles": "", "fill_cycles": "", "fill_calls": 0, "packets": 0, "ack_count": 0, "bytes": 0}},
  {"label": "A2", "duration_ms": 0, "md5": "", "sha256": ""}]}
```

## Analyse
`python3 analyze_trace.py evidence.json --cpu-hz 200000000` (la fréquence est toujours fournie, jamais supposée en silence ; 200 MHz est la valeur du code du stub, à recouper).

Règles appliquées, dans cet ordre :
1. **Intégrité** : SHA-256 identiques pour A1, T et A2 ; MD5 identiques pour les quatre runs. Sinon `INVALID`, aucune conclusion sur le temps.
2. **Cohérence de la trace** : octets = taille de la région, paquets = 256, ACK = paquets, `fill_cycles ≤ send_cycles`, aucun compteur nul. Sinon `INVALID`.
3. **Recoupement d'horloge** : cycles comptés / temps mur du run T doit être de 0,8 à 1,2 fois la fréquence supposée. Sinon `CLOCK_ASSUMPTION_SUSPECT`.
4. **Stabilité** : écart A1/A2 ≤ 10 %. Sinon `UNSTABLE`.
5. **Verdict** : une phase est dominante au-delà de 50 % des cycles comptés.

| Verdict | Lecture |
| --- | --- |
| `ROM_CDC_FIFO_FILL` | examiner `esp_rom_cdc_acm_fifo_fill()` et la gestion du FIFO de la ROM |
| `SLIP_ENCODING_AND_SEND` | `send` important, `fill` faible : examiner l'encodage SLIP et l'émission octet par octet |
| `ACK_WAIT_AND_HOST_SYNC` | examiner le chemin retour USB et la synchronisation de l'hôte |
| `READING_FLASH` / `MD5` | contredirait la mesure D de 2,175 s : revérifier avant toute conclusion |
| `NO_SINGLE_DOMINANT_PHASE` | plusieurs phases contribuent ; pas de cause unique |
| `INVALID`, `UNSTABLE`, `CLOCK_ASSUMPTION_SUSPECT` | pas de conclusion |

**`fill_cycles` est inclus dans `send_cycles`** : le rapport donne `send_without_fill` et ne les additionne jamais. Les **cycles** (du stub) et les **microsecondes** (convertis ici) sont des champs distincts.
La part de l'écart que ni `send` ni `ack` n'expliquent est publiée comme `unattributed_ms`, pas masquée.

## Limites de la mesure
- `send_cycles` inclut l'encodage SLIP et la transmission synchrone ; l'achèvement asynchrone côté USB tombe dans l'attente d'ACK.
- Le temps d'ACK couvre le retour USB, le traitement du navigateur et la synchronisation avec l'hôte : on ne peut pas les séparer sans instrumentation côté hôte.
- Un intervalle isolé de plus de 21 s à 200 MHz ferait déborder le compteur 32 bits ; la trace ne le détecterait pas.
- Un run par séquence : un écart de mesure, pas une borne statistique.
- La mesure est faite avec l'instrumentation, qui ajoute quelques cycles par lecture du compteur (négligeables devant les ms par paquet, non nuls).

## Admission de T : détection ROM / stub (PR « fiabilité de la détection »)

À l'instant de T, avant toute écriture RAM, la page **refait** la synchronisation de la session courante. Elle **transmet** des trames `ESP_SYNC` (opcode `0x08`, il n'existe pas de `loader.ESP_SYNC` dans esptool-js 0.7.0) ; aucune écriture mémoire/flash, aucun reset, aucune nouvelle tentative. Relevés : `loader.sync()` (échec ou non, `syncStubDetected`), un échange brut propre (`command(0x08)` puis jusqu'à sept `readPacket(0x08)`, valeurs `val` conservées telles que reçues, int32 signé) et `IS_STUB`. Une ROM répond 8 fois, un stub une seule fois (avec `val` 0) et `sync()` échoue alors : un `sync()` incomplet n'est compatible qu'avec un stub. Aucun indice n'est une preuve absolue.

| Indices | Code | Effet |
| --- | --- | --- |
| `sync()` complet, `syncStubDetected` faux, 8 réponses non nulles | — | le chargement continue |
| une réponse à 0 (ou 8 zéros), `sync()` incomplet ou `syncStubDetected` vrai, `IS_STUB` éventuel | `diagnostic_stub_reported_running` | refus, carte laissée en quarantaine résidente |
| contradiction ou observation incomplète (valeurs mixtes, 2 à 7 réponses, heuristique ≠ échange, `IS_STUB` vrai mais synchro ROM, sonde MD5 ≠ synchro, **sonde MD5 `unknown`**) | `diagnostic_session_state_indeterminate` | refus, quarantaine résidente |
| erreur de communication, première réponse absente, **valeur invalide** (jamais convertie en 0) | `diagnostic_admission_sync_failed` | refus, quarantaine résidente |

`IS_STUB` faux n'apporte aucune information ; seul `true` compte. Le refus publie, en liste blanche : `sync_values` (≤ 8 int32), `sync_count`, `sync_completed`, `sync_zero_count`, `sync_stub_detected`, `loader_stub`, `md5_shape`. L'ancien code `diagnostic_stub_requires_a_rom_session` (runs 001–002) n'est plus produit ; son message est conservé pour lire ces preuves.

### Quarantaine après un refus avant chargement
Le serveur mémorise les drapeaux de la carte au démarrage de T (`diag_prior`). Seuls les refus de la liste fermée `P4_REFUSALS_BEFORE_ANY_CHIP_WRITE` (consentement, empreinte, paramètres, intégrité, puce ou révision inadaptée : le chargeur n'a rien reçu et le refus ne révèle aucun doute matériel), avec un échec simple, `ram_load: not_started` et une catégorie `refused_before_load`/`local_validation` cohérentes, rétablissent ces drapeaux. Tout le reste (stub signalé, état indéterminé, synchro impossible, code inconnu, `hold`, résultat tardif) garde la quarantaine + `diagnostic_resident`. Aucune quarantaine préexistante n'est levée : une carte résidente ne peut pas démarrer T, et seule la récupération avec preuve ROM la lève.

### Sonde MD5 indépendante : pertinence (non exécutée)
Une réponse `ESP_SPI_FLASH_MD5` fait 32 octets ASCII en ROM et 16 octets bruts avec un stub (observé : PC-9 `rom-ascii32`). Elle serait un indice **indépendant** de la synchro, mais c'est une commande matérielle (lecture NOR) : elle ne fait **pas** partie de l'admission par défaut. Le classificateur pur `classifyMd5Answer` et l'option `md5Probe` de `loadDiagnosticStub` existent et sont testés avec des chargeurs simulés ; rien ne les branche dans l'agent. L'usage réel exige une autorisation matérielle séparée de l'opérateur.

### Vérification contre esptool-js 0.7.0
`tests/esptool-real-sync.test.mjs` pilote le **vrai** `ESPLoader` 0.7.0 (épinglé en `devDependencies`) avec un transport scripté, hors Web Serial et hors matériel. Il établit : pas de `ESP_SYNC`, signature de `command()`/`readPacket()`, 8 réponses d'une ROM, 1 d'un stub, aucune réponse résiduelle, trames transmises limitées à l'opcode `0x08`. Reste inconnu (donc visible à la première observation réelle) : le nombre de réponses effectif de la ROM du P4 en USB OTG ; un écart donne un refus `diagnostic_session_state_indeterminate`, sans effet sur la puce.

### Preuve de retour ROM (`verifyRomReturn`)
Elle réutilise la même détection fraîche (aucun reset, chargement RAM ni écriture NOR : seules des trames `ESP_SYNC` sont transmises). Seul le verdict `rom` est une preuve. `stub` → `diagnostic_stub_still_running`, `indeterminate` → `diagnostic_recovery_state_indeterminate`, exception de synchronisation → `diagnostic_recovery_sync_failed` (valeurs brutes publiées en liste blanche) ; une perte de transport garde son propre code (`serial_disconnected`, `serial_timeout`…). Un refus n'envoie rien au serveur : la quarantaine et le drapeau résident restent.

### Connexion « ROM seule » : capacité flash inconnue
Le constat du 2026-11-20 : `identity()` exigeait un JEDEC valide (`flash_capacity_unknown`) avant l'enregistrement de l'agent, ce qui bloquait la connexion puis la récupération. En « ROM seule » uniquement (P4), l'identité du MCU est vérifiée (famille + MAC lisible et valide, sinon refus `physical_identity_unverified`), et la capacité est soit **mesurée** (JEDEC valide), soit **déclarée inconnue** : `flash_size: null`, `flash_size_known: false`, JEDEC brut et contexte de lecture (`ok`/`invalid`/`error`, rang de lecture, stub) conservés dans `flash_capacity` et affichés dans la console (« JEDEC ROM seule : … »). Une capacité mémorisée n'est jamais reprise comme mesurée ; `flash_id` reste `null` (un JEDEC non exploitable n'est pas un identifiant physique). À la reconnexion, famille et MAC sont toujours comparées ; capacité et JEDEC seulement si les deux côtés ont été mesurés. Le serveur n'accepte `flash_size: null` que sur déclaration explicite, pour un P4 dont les capacités sont limitées à `identify`, `mark` et `diagnostic.p4_usb_trace` ; `flash.read`, `partitions`, `flash.write` et `flash.erase` sont refusés (`flash_capacity_unknown`). Le diagnostic lui-même (région fixe 0x8000, 1 Mio, lecture seule) ne dépend pas de la capacité. Une perte de transport pendant la lecture du JEDEC n'est pas rétrogradée en « inconnue ».


### Observabilité de l'admission et du chargement RAM (observation seule)
Origine : run-003 (2026-11-20), échec indéterminé `diagnostic_stub_ram_load_failed` après un timeout de 3 s, sans autre localisation. Ajouté, **sans changer** commandes ROM, taille de bloc, délais, adresses, contenu du stub, admission, quarantaine ni récupération :
- **Admission réussie** : `admission` (résultat de l'opération) et, en cas d'échec ultérieur, les mêmes champs plats dans l'erreur : `sync_values`, `sync_count`, `sync_zero_count`, `sync_completed`, `sync_stub_detected`, `loader_stub`, `admission_verdict` (`rom`/`stub`/`indeterminate`).
- **Chargement RAM** : `ram_load` (résultat) et, dans l'erreur, `ram_steps` (≤ 32 entrées, tout-ou-rien), `ram_block_size`, `ram_acked_count`, `ram_failed_index`, `ram_last_acked_index`. Une entrée = `step` (`mem_begin`/`mem_block`/`mem_finish`), `segment` (`text`/`data`/`entry`), `block`, `blocks`, `address`, `size`, `ram_block`, `started`, `acked`, `duration_ms`, `outcome` (`ack`/`timeout`/`closed`/`invalid`/`error`). Une entrée est écrite **avant** la commande (commencée, `acked: 0`) et passée à `acked: 1` seulement à la réponse du MCU. Aucune donnée du stub, aucun texte du chargeur : la classe d'erreur est dérivée localement de signaux fixes.
- Le journal de l'agent affiche l'étape (`étape RAM : mem_block text bloc 1/2 commencée, non acquittée (timeout, 3001 ms)`) et les comptes de synchronisation. Le journal SQLite du serveur n'est pas modifié (le résultat complet reste dans la vue de l'opération en mémoire et dans les preuves de campagne).

Exemple (timeout du premier `memBegin`, produit par le vrai code) :
```json
{
 "code": "hardware_outcome_unknown",
 "origin_code": "diagnostic_stub_ram_load_failed",
 "phase": "diagnostic_stub_load",
 "error_type": "IdlabDiagnosticError",
 "category": "ram_load_failed",
 "ram_load": "started",
 "origin_error_type": "IdlabDiagnosticError",
 "sync_values": [
  32273,
  32273,
  32273,
  32273,
  32273,
  32273,
  32273,
  32273
 ],
 "admission_verdict": "rom",
 "ram_steps": [
  {
   "step": "mem_begin",
   "segment": "text",
   "address": 1341456384,
   "size": 9000,
   "ram_block": 6144,
   "started": 1,
   "acked": 0,
   "duration_ms": 31,
   "outcome": "timeout",
   "blocks": 2
  }
 ],
 "loader_stub": 0,
 "sync_zero_count": 0,
 "sync_stub_detected": 0,
 "sync_count": 8,
 "sync_completed": 1,
 "ram_block_size": 6144,
 "ram_acked_count": 0,
 "ram_failed_index": 0
}
```

**Borne de la liste d'étapes.** `ram_steps` publie au plus 32 entrées (tout-ou-rien côté liste blanche). Le stub instrumenté qualifié (texte 8952 octets, données 276 octets) exige **9 commandes** avec des blocs de 0x800 (6 avec 0x1800) : la borne n'est jamais atteinte. Pour un stub plus long, le rapport garde les 30 premières entrées **plus les deux dernières** (dernière commande acquittée et commande fautive, ou les deux dernières commandes en cas de succès) et publie `ram_steps_omitted` ; `ram_failed_index`, `ram_acked_count` et `ram_last_acked_index` gardent la numérotation d'origine. Aucune donnée du stub, aucun texte libre.
