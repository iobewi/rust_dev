# M40-ID et M40-PHYS-B — identité matérielle et association physique (issue #40)

**Statut : écrites et qualifiées en SIMULATION uniquement. NON exécutées sur matériel.** Chaîne obligatoire : pytest → client Python → API
REST → serveur → agent navigateur → ESP32. Aucune écriture, aucun effacement, aucun grant, aucune modification du serveur ni du protocole.

## 1. Pourquoi une empreinte, et ce qu'elle prouve
Jusqu'ici, le banc comparait l'identité (puce, taille de flash, RDID, MAC) **dans le navigateur** à la reconnexion ; le serveur ne vérifie que la
puce et la taille de flash. Ni la MAC ni le `flash_id` n'étaient visibles de l'API. `identify` est la seule commande qui renvoie la MAC.
- **Empreinte** : prouve que *le même matériel* a répondu (ou un autre). Elle ne dit pas sur quel câble il est branché.
- **Association physique (M40-PHYS-B)** : prouve, par le geste de l'opérateur, quel câble correspond à quel identifiant logique. Elle ne dit rien
  de l'identité matérielle. Les deux preuves sont **séparées** dans les rapports ; seule leur conjonction (option `M40-PHYS-B-ID`) relie
  identifiant logique, câble et matériel.
- Une **nouvelle génération d'agent n'est pas une nouvelle identité physique**.

## 2. Format canonique et empreinte (version 1)
```
iobewi-identity-v1|chip=<puce>|mac=<12 hexa minuscules>|flash_size=<octets>|flash_id=0x<hexa>
empreinte = HMAC-SHA-256(clé de 32 octets, forme canonique), en hexadécimal
```
- **HMAC-SHA-256** plutôt que `SHA-256(clé ‖ texte)` : construction standard, sans faiblesse d'extension de longueur.
- Champs conservés pour le diagnostic : puce, taille de flash et `flash_id` en clair (données de modèle, pas d'unité) ; la MAC sous la forme
  `HMAC(clé, "mac|" + 12 hexa)` pour pouvoir nommer *quel champ* a changé sans conserver la MAC ; **deux derniers octets** de la MAC (4 hexa).
- **Jamais** dans une preuve : la MAC brute, la clé. Les messages d'erreur ne répètent pas la valeur fautive.

## 3. Politique de stockage
| Élément | Règle |
|---|---|
| Clé | 32 octets d'un CSPRNG, créée **une seule fois** (`O_EXCL`, sans suivre de lien, 0600 dès le premier octet), répertoire parent 0700, **hors Git** et hors `evidence_root`. Création seulement si `m40.identity.create_key` est explicitement `true`. Jamais remplacée. |
| Référence | Créée **une seule fois, atomiquement** (fichier temporaire complet, fsyncé, 0600, puis lien dur vers le nom final : échoue si le nom existe), jamais écrasée, jamais partielle (un crash ne laisse aucun fichier final), **indépendante** des résultats de campagne (chemin hors `evidence_root`). Contient les empreintes et les suffixes, pas la MAC. |
| Enrôlement | Seulement avec le consentement `M40-ID-ENROLL`, **après** toutes les autres vérifications de la recette, et seulement si aucune référence n'existe. Si elle existe : comparaison, jamais ré-enrôlement. Une référence illisible, partielle, en lien symbolique ou aux permissions ouvertes **bloque** la recette (jamais réparée ni remplacée). |
| Preuves | Empreintes, suffixes, étapes horodatées en 0600 dans le répertoire privé de la campagne, écrites dans un `finally`, fichier par fichier, sur tous les verdicts. |

**Limite à connaître** : la MAC brute **existe** dans l'agent navigateur, dans la mémoire du serveur et dans la réponse HTTP de l'opération `identify`
(le journal durable SQLite l'écarte : `safe_result` n'a pas de champ `mac`). Ces composants ne peuvent pas être modifiés ici ; les recettes la
réduisent à son empreinte dès la réponse, effacent l'objet brut (le `Board` en garde le même dictionnaire) et **refusent de s'exécuter avec
`--showlocals`** (les tracebacks afficheraient les variables).

## 4. M40-ID — deux `identify` indépendants par carte
- **Commandes** : 4 `identify` (A, B, A, B) sur deux réservations simultanées. `identify` n'accepte aucun argument : il entre en loader puis revient
  en console. **8 transitions matérielles** (2 resets par `identify`, 4 par carte), pas de lecture double possible dans une même session loader.
- **PASS** : chaque carte stable entre ses deux lectures (puce, MAC, taille, `flash_id`), cartes distinctes (digest et MAC), chaque `identify`
  suivi de `opening loader` et `console opened` **sur sa carte**, rien d'étranger, générations/agents/epoch inchangés, exactement
  `[identify, identify]` par carte et 4 soumissions, réservations libérées, référence égale si elle existe.
- **FAIL** : champ modifié entre deux lectures (nommé), cartes identiques, événement étranger, référence différente, enrôlement impossible.
- **BLOCKED** : consentement/section/clé absents, référence inutilisable, carte non prête, `--showlocals`.
- **INDETERMINATE** : opération non réglée, continuité perdue, **retour console non prouvé** — la carte reste réservée (jamais libérée sur un état
  inconnu), aucun rejeu.
- Effet de bord utile : la taille et le `flash_id` exacts de B (écart 4 Mio / 16 Mio).

## 5. M40-PHYS-B — association physique par le câble de B
1. Observation initiale (GET) de A et B ; `BoardWatch` sur A pendant toute la recette.
2. Demande à l'opérateur (affichée en direct, sans `-s`) : débrancher **uniquement** le câble de B puis le rebrancher.
3. Attendu dans les événements du serveur : `disconnected` de B, puis `connected` de B sous une **nouvelle génération** ; A ne bouge pas.
- **FAIL** : le geste frappe A d'abord (A perdue alors que B ne l'est pas : mauvais câble), quarantaine/grant/événement étranger sur A,
  identité différente (option ID). **INDETERMINATE** : B ne revient pas, A se ré-enregistre (cause non attribuable au geste). **BLOCKED** :
  geste jamais vu dans le délai, consentement/section absents.
- **Option `M40-PHYS-B-ID`** (consentement séparé) : après le retour de B, réservation, **un seul** `identify`, empreinte comparée à la référence
  existante (obligatoire, vérifiée **avant** de demander le geste), retour console contrôlé, libération seulement si l'état est sûr. L'identité lue
  ne remplace jamais la référence.
- **Transitions de B** : le geste (la page rouvre le port et ré-identifie B : 2 resets, non contrôlés par la recette) ; avec l'option, 2 de plus.

## 6. Procédure de consentement pour les essais réels
Chaque étape demande un accord explicite de l'opérateur, une session privée dédiée en 0600 dont les SHA-256 avant/après consentement sont consignés.
1. **M40-ID** (`M40-ID`, 4 `identify`, 8 transitions sur A **et** B, commandes sur B) ; `M40-ID-ENROLL` seulement si aucune référence n'existe encore
   et que vous voulez l'enregistrer. Section `m40.identity` (`key_file`, `reference_file`, `create_key`) dans la session privée.
2. **M40-PHYS-B** (`M40-PHYS-B`) : débranchement/rebranchement du câble de B ; section `m40.phys_b` (délais).
3. **M40-PHYS-B-ID** (`M40-PHYS-B-ID`) : 1 `identify` de B après retour, **seulement après** M40-ID et une référence enregistrée.
Commande type (token dans l'environnement du processus, jamais en ligne de commande ; sans `--showlocals`, `--qual-allow-reset`, `--qual-allow-dirty`) :
```
cd bench && python -m pytest recipes/qualification_campaign/test_m40_identity.py::<test> --bench --bench-url http://127.0.0.1:8080 \
  --bench-device esp32-local --qual-session <privé>/session.m40-<id>.json --bench-results <privé>/runs --junitxml=<privé>/junit-<id>.xml -rA -p no:cacheprovider
```
Arrêt sans relance : serveur incompatible, génération/epoch incohérents, quarantaine, opération préexistante, réservation impossible, rejeu possible, retour
console non prouvé, observation incomplète, consentement manquant.

## 7. Limites de la preuve
- Une empreinte identique à la référence prouve la même MAC/puce/flash, pas l'intégrité du firmware ni l'absence de clone de MAC.
- Première exécution : stabilité *intra-campagne* seulement ; la stabilité inter-sessions n'est démontrée qu'avec une référence.
- Perte de la clé : les empreintes ne sont plus comparables (ré-enrôlement nécessaire) ; ce n'est pas une faille.
- Vue API seulement : pas d'objet `SerialPort`, pas de fermeture de port observable ; les horodatages d'événements sont ceux du serveur.
- Simulation : `SIM_IDENTITY`, `SIM_IDENTITY_CHANGE`, `SIM_IDENTIFY_EVENTS`, `SIM_NO_CONSOLE_RETURN`, `SIM_REENUMERATE_AFTER_IDENTIFY` et le canal
  `--control` du simulateur imitent l'agent et les gestes de l'opérateur ; ils ne prouvent rien sur de vraies cartes.
