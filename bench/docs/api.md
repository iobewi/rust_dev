# Contrat API v1

Toutes les routes `/api/v1/*` exigent `Authorization: Bearer TOKEN`. Les erreurs ont la forme `{"error":{"code":"...","message":"..."}}`. HTTP 400 : schéma/digest/adresse invalide ; 401 : token absent ou invalide ; 403 : scope, propriétaire ou grant refusé ; 404 : ressource absente/expirée ; 409 : conflit d’état ; 413 : taille excessive ; 503 : stockage borné plein.

`read` permet les GET. `control` permet les mutations et soumissions. `program` s’ajoute pour flash.write/flash.erase. Une identité voulant consulter les résultats de ses commandes possède donc `read` et `control`.

## REST

| Méthode et chemin | Entrée | Sortie |
|---|---|---|
| `GET /health` | Public | `status`, version du protocole, `epoch` |
| `GET /api/v1/devices` | — | `devices`, `agents` connectés ; carte : id, famille, capacité, capacités, génération, connexion, quarantaine, durée restante du grant, réservation publique (`owner`, `remaining_seconds`) sans identifiant de réservation |
| `POST /api/v1/leases` | `{"device":"esp32-local","ttl":900}` | 201 : `id`, `device`, `owner`, `until`, `active` |
| `GET /api/v1/leases/{id}` | Propriétaire | Réservation active |
| `PATCH /api/v1/leases/{id}` | `{"ttl":900}` | Renouvellement de la réservation active |
| `DELETE /api/v1/leases/{id}` | Propriétaire | Libération ; travail en cours = indéterminé |
| `POST /api/v1/operations` | Voir ci-dessous | 202 : opération asynchrone |
| `GET /api/v1/operations/{id}` | Propriétaire | État, résultat, erreur, `cancel_requested`, éventuel `late_result` |
| `DELETE /api/v1/operations/{id}` | Propriétaire | Annulation en attente ou demande d’annulation en cours |
| `GET /api/v1/devices/{id}/events?after=N` | `read` | Lot d’événements bruts |
| `GET /api/v1/devices/{id}/events/stream?after=N` | `read` | SSE : `data: {LOT}` toutes les 50 ms, même vide |
| `GET /api/v1/artifacts` | `read` | Liste temporaire : id, size, sha256, created |
| `POST /api/v1/artifacts` | Corps binaire + `X-SHA256` hex minuscule | 201 : id, size, sha256, created |
| `GET /api/v1/artifacts/{id}` | `read` | Octets, `X-SHA256`, téléchargement `.bin` |

Les agents utilisent REST seulement pour uploader les dumps et télécharger les artefacts affectés à une opération en cours sur une carte autorisée. Ils ne disposent pas de l’API de contrôle client. Les cartes et le flux matériel sont visibles aux lecteurs autorisés du même banc ; il n’y a pas d’isolation multi-locataire des captures ou artefacts.

`until` et `created` sont des secondes monotones du processus serveur, uniquement informatives. `at` des événements est un horodatage Unix en secondes. `agent_at` UART est un horodatage Unix en millisecondes, fourni par l’agent et non utilisé pour l’ordonnancement.

### Soumettre une commande

```json
{
  "lease": "IDENTIFIANT_RESERVATION",
  "command": "flash.write",
  "args": {
    "chip": "esp32s3",
    "images": [{"artifact": "IDENTIFIANT_UPLOAD", "sha256": "SHA256_HEX", "address": 65536}],
    "erase": false,
    "return_console": true
  },
  "ttl": 30,
  "timeout": 120,
  "allow_destructive": true
}
```

`ttl` borne le délai de départ (0,1 à 120 s). `timeout` borne l’attente du résultat après autorisation de départ (0,1 à 600 s). Le TTL ne sert pas à couper une programmation démarrée. L’agent contrôle aussi l’échéance d’exécution du serveur : après expiration, une opération flash en cours peut se terminer, mais le blocage reste actif et aucun retour console automatique ne redémarre la carte. La réservation dure de 1 à 3 600 s. Aucun retry automatique : après une réponse de soumission perdue, inspecter l’état du banc et ne pas renvoyer aveuglément une écriture. La réservation reste exclusive même si le client HTTP disparaît, jusqu’à libération, expiration ou libération explicite par l’opérateur de l’agent physique (voir `release_control`).

| Commande | Arguments | Grant local temporaire |
|---|---|---|
| `identify` | `{}` | Non |
| `reset` | `{}` ; console externe EN seulement | Non |
| `console` | `{}` | Non |
| `partitions` | `address` défaut 0x8000, `return_console` | Non |
| `flash.read` | `address`, `size`, `return_console` ; résultat contient `artifact` | Non |
| `flash.write` | `chip`, `images` (1–16), `erase` optionnel, `return_console` | Oui + program + consentement |
| `flash.erase` | `chip`, `return_console` | Oui + program + consentement |
| `uart.write` | `data` base64, 1–16 384 octets | Oui |
| `uart.configure` | `baudrate` entier 1 200–3 000 000 | Non |
| `io0` | `duration_ms` 1–10 000, UART externe seulement | Oui |
| `mark` | `label` texte ≤256 caractères | Non |
| `trigger` | `data` base64 ≤256 octets, `action` mark/reset, `delay_ms` 0–10 000, `expires_ms` 1–120 000 | Oui |
| `trigger` | `action:"disarm"` | Oui |
| `http.request` | `url`, `method` GET/POST, `body` texte ≤16 Kio, `timeout_ms` 100–30 000 | Oui |

Les arguments inconnus sont refusés. Les opérations flash lisent la capacité mesurée ; les écritures doivent annoncer la famille de puce exactement. Les plages invalides, dépassements, chevauchements de secteurs et digests différents sont refusés avant programmation. Les familles sont normalisées (`esp32s3`, `esp32c3`). Les déclencheurs recherchent une séquence d’octets exacte, sur plusieurs chunks si nécessaire ; ils sont à usage unique, bornés par leur TTL et la réservation, et n’exécutent pas de reset pendant une opération matérielle. Le désarmement et le réarmement invalident aussi toute action différée déjà programmée après détection du motif. Ils ne portent aucune règle OTA/Wi-Fi. Le renouvellement d’une réservation ne prolonge pas un déclencheur déjà armé.

`return_console` vaut vrai par défaut pour les opérations chargeur. Une issue inconnue bloque ce retour. Une écriture vérifiée par le chargeur rapporte MD5 ; `sha256_readback:"not-run"` ne doit pas être interprété comme une relecture indépendante. Le client peut demander `flash.read` puis comparer SHA-256.

### Flux UART

Un lot contient `epoch`, `next`, `latest`, `lost` et `events`. Les lots sont bornés à environ 48 Kio pour être consommables comme lignes SSE ; `latest` indique le dernier numéro disponible et `next` le dernier numéro effectivement livré. Chaque événement porte `seq`, `epoch`, `at`, `kind`. Un événement UART ajoute `data` base64, `source_seq`, `source_lost`, `agent_at`. Le prochain appel passe `after=next` : la borne est exclusive. `lost` compte les événements du tampon serveur devenus irrécupérables ; `source_lost` compte les chunks abandonnés par l’agent avant réception serveur. Un changement `epoch` nécessite de repartir de zéro et signale une rupture de continuité. Une nouvelle génération agent et les événements `connected/disconnected` signalent également une rupture. Le numéro source recommence à 1 dans une nouvelle génération.

Le canal SSE exige le header Bearer ; utiliser fetch streaming ou le client Python, pas un token dans l’URL EventSource. Une connexion SSE lente est fermée après 5 s de blocage d’écriture. La réouverture reprend au dernier curseur connu et signale les pertes éventuelles.

## Agent WebSocket

Connexion même origine : `/agent/v1/ws`. Dans les cinq secondes, première trame JSON :

```json
{"type":"auth","version":1,"token":"TOKEN_AGENT"}
```

Réponse `welcome` : version, epoch, `server_ms` monotone. Puis `register` :

```json
{"type":"register","device":{"id":"esp32-local","chip":"esp32s3","flash_size":4194304,"capabilities":["identify","flash.read","flash.write"],"hold":false,"http_origins":[]}}
```

Réponse `registered` : `device`, `generation`. Une connexion concurrente du même agent ou de la même carte est refusée. Les événements de carte portent toujours `device` et `generation`.

1. Serveur → `offer` : id, device, generation, command, args, lease, lease_deadline_ms, deadline_ms, remaining_ms, timeout_ms.
2. Agent → `ready` : id, sans exécution matérielle.
3. Serveur revérifie le TTL, la génération, la réservation, la quarantaine et le grant ; → `execute` : id, deadline_ms, remaining_ms, execution_deadline_ms (échéance monotone serveur de fin d’exécution). Le serveur a alors enregistré `running`.
4. L’agent revérifie l’échéance monotone et les autorisations, garde l’identifiant dans un ensemble anti-rejeu, exécute une seule fois et → `result` : id, state (`completed/failed/indeterminate/cancelled`), result, error, hold éventuel.

5. Serveur → `result_ack` : id, device, generation, state, quarantined. L’agent conserve un blocage persistant jusqu’à cet accusé correspondant. Un résultat tardif, une quarantaine ou un accusé perdu empêche le retour console automatique. Les offres suivantes attendent la fin de ce retour console côté agent. Une panne de retour console produit `hold`, qui remet aussi la carte en quarantaine côté serveur.

Une trame `error` refuse une transition : elle n’autorise aucune exécution. Le serveur ne remet aucune offre en file après déconnexion. Le message `lease_revoked` supprime les déclencheurs et clôt les droits associés ; l’agent contrôle aussi son échéance locale de réservation, indépendamment de la réception de ce message. `cancel` exprime uniquement une demande. Heartbeat WebSocket serveur 15 s.

Événements agent : `uart` (`seq`, `data`, `at`), `mark/transport/trigger` (`detail` ≤256 caractères), `grant` (`seconds` 0–14 400 ; zéro révoque) et `hold` (blocage de sécurité après incident), `recover` (levée opérateur du blocage, refusée tant qu’une exécution reste en suspens). Un grant ne peut venir que de l’agent authentifié ; l’UI réelle exige une confirmation locale.

Corps JSON REST et trames WS : 64 Kio maximum ; chunk UART : 16 Kio maximum. Les octets secrets d’émission UART et corps HTTP ne sont pas inclus dans les diagnostics d’opération. Les résultats et le flux UART brut restent accessibles aux clients autorisés : les projets doivent les traiter comme potentiellement sensibles.

### Diagnostics et libération opérateur

Les refus de programmation distinguent `program_permission_required` (scope `program` absent) et `destructive_consent_required` (`allow_destructive:true` absent). Le contrôle du grant reste indépendant et intervient avant ces deux refus.

Les erreurs matérielles connues utilisent des codes sans texte brut : `serial_disconnected`, `console_required`, `external_uart_required`, `partition_table_invalid`, `physical_identity_changed`, `operator_recovery_required`. Une table vide/vierge à l’adresse demandée est refusée ; il n’y a aucune recherche ni écriture automatique à une autre adresse. Les erreurs inconnues restent `hardware_command_failed`.

L’agent physique authentifié peut envoyer `{"type":"release_control","device":"…","generation":"…"}`. Le serveur refuse (`still_running`) toute exécution encore en cours ou non stabilisée, y compris après timeout ; sinon il révoque le grant et termine toutes les réservations actives de cette carte, en annulant le travail encore en attente. Réponse : `control_released` avec `device` et `generation`, sans identifiant privé de réservation. Cette action n’existe pas sur l’API REST client et ne lève aucune quarantaine. Le contrôle de génération interdit une demande par un ancien agent. Les déclencheurs sont désarmés localement avant demande et à réception des révocations.

### Lecture longue et diagnostic des échecs

Une commande `flash.read` reste une opération unique côté API et produit au plus un artefact complet. L’agent lit la région par blocs séquentiels d’au plus 1 Mio via esptool-js 0.7.0, sans nouvelle identification entre les blocs, sans retry et sans publication de préfixe partiel. La taille exacte de chaque bloc est vérifiée. L’échéance de départ n’est pas réappliquée pendant la lecture démarrée ; les contrôles de connexion, réservation, autorisation applicable et échéance d’exécution restent actifs. L’interface manuelle demande `timeout:600` pour les lectures ; les clients REST choisissent leur timeout dans les limites existantes, sans prolongation automatique.

`error` conserve un `code` et peut ajouter `phase`, `error_type`, `transport_kind`, `elapsed_ms`, `received_bytes`, `expected_bytes`, `block_address`, `block_size`, `serial_reads`, `serial_bytes`, `serial_timeouts`, `silence_limit_ms`, `last_read_ms`, `last_packet_bytes`, `start_remaining_ms`, `execution_remaining_ms`, `lease_remaining_ms`, `grant_remaining_ms`, `visibility`, `http_status`. Ces champs sont filtrés : aucun message arbitraire d’exception, corps HTTP, nom réseau, token ou contenu flash.

Les phases distinguent notamment `identify`, `flash_read`, `artifact_upload` et `artifact_download`. Les erreurs du transport sont traduites indépendamment de l’ordre de notification USB : `closed` → `serial_disconnected`, `no-response` → `serial_timeout`, `partial-packet` → `serial_partial_packet`, `invalid-packet` → `serial_invalid_packet`. Un échec HTTP d’artefact reste `artifact_transfer_failed`, et non une déconnexion USB. Les erreurs d’échéance distinguent connexion agent perdue, réservation expirée, grant expiré et délai d’exécution dépassé.

Après un échec de lecture flash, la session chargeur est fermée sans retry ; une erreur de nettoyage ne remplace pas l’erreur initiale. Le verrou d’écriture Web Serial est toujours relâché si l’envoi d’un acquittement échoue. Le décodage SLIP utilise un tampon à croissance géométrique au lieu d’une allocation par octet.
