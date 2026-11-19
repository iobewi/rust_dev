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
