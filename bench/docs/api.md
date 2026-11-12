# Contrat API v1

## Agents navigateur multicarte (#40)

Le navigateur conserve sa session locale HttpOnly. Une page peut ouvrir plusieurs WebSockets
`/agent/v1/ws` au sein de cette session, avec un message initial
`{"type":"auth","version":1,"device":"board-id"}`. Le serveur vérifie que
`device` est autorisé par cette session et attribue lui-même une identité d'agent
stable pendant sa durée de vie. Cette connexion ne peut enregistrer qu'un device
(`register.device.id`) correspondant à celui du message d'authentification.
Une autre session navigateur ne récupère pas cette identité. Le broker refuse
toujours l'enregistrement simultané d'un même device logique.

Pour les opérations manuelles, le serveur détermine les privilèges locaux
uniquement à partir de l'agent qui détient effectivement le device demandé ;
le changement de sélection côté UI n'intervient pas dans cette décision.

Lors d'un upload d'artefact depuis une page multicarte, fournir
`X-Bench-Artifact-Role: agent` et `X-Bench-Device: board-id`. Le serveur
vérifie l'appartenance de ce device à la session et son agent effectivement
connecté avant de fixer le propriétaire de l'artefact. L'absence de
`X-Bench-Device` reste réservée à l'agent navigateur historique mono-carte.
Les agents distants par token continuent à utiliser leur identité de token,
sans nouveau header. Le téléchargement d'artefacts reste soumis aux règles
existantes d'affectation d'opération.

**Compatibilité** : un navigateur historique envoyant `auth` version 1
sans `device` garde l'identité mono-agent historique. Cette compatibilité
ne doit pas être utilisée par la nouvelle UI multicarte. Les clients REST
Bearer et les agents distants ne changent pas.


Les clients externes utilisent `Authorization: Bearer TOKEN` pour les routes `/api/v1/*`. La page locale utilise une session navigateur HttpOnly, contrôlée par origine et connexion locale directe ou relais local explicitement déclaré, sans token JavaScript. Un header Authorization présent est toujours vérifié et ne retombe jamais sur le cookie si son token est invalide. Les erreurs ont la forme `{"error":{"code":"...","message":"..."}}`. HTTP 400 : schéma/digest/adresse invalide ; 401 : token absent ou invalide ; 403 : scope, propriétaire ou grant refusé ; 404 : ressource absente/expirée ; 409 : conflit d’état ; 413 : taille excessive ; 503 : stockage borné plein.

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
| `POST /api/v1/manual-operations` | `device`, `generation`, `command`, `args`, `ttl`, `timeout`, `allow_destructive` | 202 : réservation atomique automatique et opération ; mêmes contrôles que la file normale |
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
| `reset` | `{}` ; en console, EN sur pont UART ou reset interne USB Serial/JTAG 303a:1001 | Non |
| `console` | `{}` | Non |
| `partitions` | `address` défaut 0x8000, `return_console` | Non |
| `flash.read` | `address`, `size`, `return_console`, `stub_digest` (booléen strict, défaut faux) ; résultat contient `artifact` et, si `stub_digest`, `stub_digests` (un état par bloc de 1 Mio : `received` + `md5`, `absent`, `partial`, `invalid`) | Non |
| `flash.md5` | `address`, `size`, `return_console`, `loader` (`stub` par défaut, ou `rom` : chargeur ROM **sans stub**, sa seule valeur sûre est de comparer des chemins de lecture ; une session déjà ouverte dans l'autre mode est refusée) ; résultat : `md5` (32 hex), `format` (`stub-raw16` ou `rom-ascii32`), `loader` (`stub` ou `rom`, mode réellement utilisé), `address`, `size`, `identify` (dont `loader_stub`, 0 sans stub). Lecture seule, calculée par la puce (SPI_FLASH_MD5) ; une réponse mal formée est `md5_response_invalid` | Non |
| `flash.write` | `chip`, `images` (1–16), `erase` optionnel, `return_console` | Oui + program + consentement |
| `flash.erase` | `chip`, `return_console` | Oui + program + consentement |
| `uart.write` | `data` base64, 1–16 384 octets | Oui |
| `uart.configure` | `baudrate` entier 1 200–3 000 000 | Non |
| `io0` | `duration_ms` 1–10 000, UART externe seulement | Oui |
| `boot.download` | `hold_ms` 100–3 000 (obligatoire), UART externe et console seulement : circuit d'auto-reset classique : EN bas (RTS), puis EN relâché IO0 bas (DTR) pendant le maintien borné, puis relâche des deux lignes ; câblage supposé, non mesuré ; ne vérifie ni le mode ni un démarrage | Oui |
| `mark` | `label` texte ≤256 caractères | Non |
| `trigger` | `data` base64 ≤256 octets, `action` mark/reset, `delay_ms` 0–10 000, `expires_ms` 1–120 000 | Oui |
| `trigger` | `action:"disarm"` uniquement ; réservation active ayant armé le déclencheur dans cette génération | Non ; scope `control` et propriétaire vérifiés |
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

Le désarmement seul accepte exactement `{"action":"disarm"}` et n’exige pas de grant. Le serveur vérifie à la soumission puis à l’exécution que le déclencheur appartient à la même réservation active ; l’agent vérifie aussi son association locale à cette réservation. Un autre propriétaire, une nouvelle réservation ou génération ne reçoit pas cette exception (`trigger_not_owned`, HTTP 403 côté serveur). Authentification, scope `control`, TTL, réservation, génération et quarantaine restent contrôlés. L’autorité locale du navigateur conserve son périmètre existant sur son propre agent. L’armement et les actions sensibles du déclencheur restent soumis au grant ; désarmer ne le prolonge pas.

Les erreurs matérielles connues utilisent des codes sans texte brut : `serial_disconnected`, `console_required`, `external_uart_required`, `partition_table_invalid`, `physical_identity_changed`, `operator_recovery_required`. Une table vide/vierge à l’adresse demandée est refusée ; il n’y a aucune recherche ni écriture automatique à une autre adresse. Les erreurs inconnues restent `hardware_command_failed`.

L’agent physique authentifié peut envoyer `{"type":"release_control","device":"…","generation":"…"}`. Le serveur refuse (`still_running`) toute exécution encore en cours ou non stabilisée, y compris après timeout ; sinon il révoque le grant et termine toutes les réservations actives de cette carte, en annulant le travail encore en attente. Réponse : `control_released` avec `device` et `generation`, sans identifiant privé de réservation. Cette action n’existe pas sur l’API REST client et ne lève aucune quarantaine. Le contrôle de génération interdit une demande par un ancien agent. Les déclencheurs sont désarmés localement avant demande et à réception des révocations.

### Lecture longue et diagnostic des échecs

Une commande `flash.read` reste une opération unique côté API et produit au plus un artefact complet. L’agent lit la région par blocs séquentiels d’au plus 1 Mio via esptool-js 0.7.0, sans nouvelle identification entre les blocs, sans retry et sans publication de préfixe partiel. La taille exacte de chaque bloc est vérifiée. L’échéance de départ n’est pas réappliquée pendant la lecture démarrée ; les contrôles de connexion, réservation, autorisation applicable et échéance d’exécution restent actifs. L’interface manuelle demande `timeout:600` pour les lectures ; les clients REST choisissent leur timeout dans les limites existantes, sans prolongation automatique.

`error` conserve un `code` et peut ajouter `phase`, `error_type`, `transport_kind`, `elapsed_ms`, `received_bytes`, `expected_bytes`, `block_address`, `block_size`, `serial_reads`, `serial_bytes`, `serial_timeouts`, `silence_limit_ms`, `last_read_ms`, `last_packet_bytes`, `start_remaining_ms`, `execution_remaining_ms`, `lease_remaining_ms`, `grant_remaining_ms`, `visibility`, `http_status`. Ces champs sont filtrés : aucun message arbitraire d’exception, corps HTTP, nom réseau, token ou contenu flash.

Une identification physique dont l'identifiant JEDEC ne correspond à aucune capacité connue produit le code `flash_capacity_unknown`, avec trois champs numériques : `jedec_id` (valeur brute de 24 bits lue), `identify_index` (rang de l'identification dans la session de loader) et `loader_stub` (1 si le stub tourne). Voir [le diagnostic de la deuxième lecture P4](p4-flash-read-diagnostic.md).

Les phases distinguent notamment `identify`, `flash_read`, `artifact_upload` et `artifact_download`. Les erreurs du transport sont traduites indépendamment de l’ordre de notification USB : `closed` → `serial_disconnected`, `no-response` → `serial_timeout`, `partial-packet` → `serial_partial_packet`, `invalid-packet` → `serial_invalid_packet`. Un échec HTTP d’artefact reste `artifact_transfer_failed`, et non une déconnexion USB. Les erreurs d’échéance distinguent connexion agent perdue, réservation expirée, grant expiré et délai d’exécution dépassé.

Après un échec de lecture flash, la session chargeur est fermée sans retry ; une erreur de nettoyage ne remplace pas l’erreur initiale. Le verrou d’écriture Web Serial est toujours relâché si l’envoi d’un acquittement échoue. Le décodage SLIP utilise un tampon à croissance géométrique au lieu d’une allocation par octet.

### Session navigateur locale et opérations manuelles

`POST /session/v1/open` exige une connexion directe de boucle locale, un Host local et un Origin égal à l’origine exacte de la requête. Il crée/réutilise un cookie `bench_ui_v1` HttpOnly, SameSite Strict, Secure en HTTPS, valable huit heures au plus. Réponse sans secret : identifiant public du client de page, liste des IDs de cartes autorisés et epoch. Le serveur borne le magasin à 128 sessions éphémères. Un redémarrage invalide tous les cookies ; l’expiration ferme la liaison agent et révoque les réservations concernées. Un relais local peut être explicitement déclaré par `BENCH_UI_FORWARD_PEERS` (pairs TCP réels autorisés) avec `BENCH_ORIGIN` (origine publique exacte, obligatoirement une URL loopback). Les headers transférés ne sont jamais utilisés comme preuve d’origine locale. Sans configuration, les relais restent refusés. Ce mode est destiné à une redirection exposée uniquement en boucle locale du PC, pas à une publication LAN.

Le même cookie authentifie REST et le WebSocket agent. Le navigateur envoie `{"type":"auth","version":1}` sans token ; le serveur lie l’agent à cette session et à sa liste de cartes. Les agents externes continuent d’envoyer leur token dans la trame d’authentification. Toute mutation REST par cookie et tout WebSocket de session exigent l’Origin exact ; les clients externes Bearer conservent leur contrat.

`manual-operations` prend une réservation exclusive de 900 s, vérifie la génération affichée et soumet l’opération dans la même file que `operations`. Un changement de génération produit `device_changed` avant départ. Si validation échoue, la nouvelle réservation est immédiatement libérée. Une réservation concurrente produit `device_reserved`, sans reprise forcée ni mise en attente implicite côté page.

L’autorité locale par opération n’est accordée que lorsque le cookie de l’opérateur correspond à la session de l’agent physique de la carte. Elle ne modifie pas `grant_until`. La confirmation destructive reste `allow_destructive:true`, avec scope `program`. Les requêtes Bearer sur cette même route ne reçoivent aucune autorité locale : elles restent soumises aux grants et scopes habituels. Les champs `local_control` ou identité agent envoyés par un client sont refusés ; seul le serveur détermine `local_control` et l’ajoute à l’offre destinée à l’agent.

Après `result_ack` et fin du retour console, l’agent envoie `{"type":"settled","device":"…","generation":"…","id":"…"}`. Le serveur vérifie la génération et l’existence d’un résultat effectivement reçu, refuse les doublons et libère la réservation automatique lorsque toutes ses opérations sont terminales et stabilisées. Une annulation/expiration avant départ la libère également ; un timeout matériel sans résultat reste protégé. Les réservations explicites des clients externes ne sont pas libérées par `settled`.

Un déclencheur automatique conserve sa réservation jusqu’à son expiration, au plus 120 s après stabilisation de l’armement, ou à son désarmement. D’autres actions manuelles de la même session peuvent réutiliser cette réservation automatique ; les autres clients restent exclus. Le désarmement stabilisé permet sa libération. Aucun rejeu n’est ajouté.

### Redirection locale de session

`create_app(..., origin="http://localhost:8080", ui_forward_peers=["172.18.0.1/32"])` configure le même contrôle que les variables d’environnement `BENCH_ORIGIN` et `BENCH_UI_FORWARD_PEERS`. Le pair réel doit correspondre à une adresse/CIDR autorisé. L’origine des mutations et du WebSocket doit être exactement celle configurée ; les en-têtes proxy et le Host interne réécrit ne la remplacent jamais. En HTTPS public, le cookie est Secure même si le relais parle HTTP au backend. Une configuration sans origine explicite, avec une origine non loopback, des credentials ou un chemin est refusée au démarrage. Ces règles s’appliquent au bootstrap, aux appels REST par cookie et à l’authentification WebSocket ; les clients Bearer gardent leur contrat.

### Reset interne USB Serial/JTAG

La capacité `reset` est annoncée pour un pont UART externe et pour le contrôleur
USB Serial/JTAG identifié par VID:PID `303a:1001`. Elle n’est pas déduite du seul VID
Espressif : un port OTG/CDC ou non identifié ne l’annonce pas. `io0` reste réservé au
pont UART externe. Les déclencheurs reset emploient la même méthode bornée et le
même verrou matériel ; aucun reset n’est exécuté en mode chargeur ou en quarantaine.

En console USB Serial/JTAG, le moteur utilise le contrôle CDC RTS avec DTR relâché :
assertion 200 ms, relâchement, attente 200 ms. Il ne pilote pas la broche EN et ne
force pas IO0. Le port de capture reste ouvert. Une erreur est remontée sans retry ;
le relâchement est tenté même si l’assertion échoue. Le résultat `reset:true` signifie
que la séquence a été envoyée, `method` vaut `usb_serial_jtag` ou `en`, et
`boot_verified:false` interdit de conclure que le firmware a effectivement démarré.

Espressif distingue ce reset des cœurs d’un reset système : les strappings ne sont
pas rééchantillonnés. Une entrée manuelle en téléchargement peut donc nécessiter un
reset physique ; aucun watchdog de secours ni accès registre implicite n’est ajouté.
Si le contrôleur disparaît du bus, aucune commande USB ne peut le réinitialiser.
Sources : [modes de reset Espressif](https://docs.espressif.com/projects/esptool/en/latest/esp32s3/esptool/advanced-options.html)
et [séquence hard_reset Espressif](https://github.com/espressif/esp-pylib/blob/master/esp_pylib/serial_reset.py).
Cette méthode doit encore être qualifiée sur la carte et le navigateur réels.

## Reliability additions (protocol v1, server/client 0.2)

See [reliability contracts](reliability.md) for the complete guarantees and limits.

- `GET /health`: adds durability and observed/declared build provenance.
- `GET /api/v1/identity`: authenticated client ID and effective scopes, no token.
- `GET /api/v1/devices`: adds agent-reported provenance and local recovery time.
- `GET /api/v1/artifacts`: adds byte capacity counters and per-artifact pins.
- `POST /api/v1/artifacts`: requires Content-Length, reserves capacity before body
  admission; 503 capacity is definitive refusal and not a request to retry.
  Agent readbacks send `X-Bench-Artifact-Role: agent`; cookie authentication binds
  this role to the session's connected agent, without accepting an agent ID from
  the request. Bearer client credentials cannot assert this role. Uploads without
  this header keep their authenticated client/agent ownership.
- `DELETE /api/v1/artifacts/{id}`: control scope plus ownership; 409 artifact_in_use
  for active/uncertain/unacknowledged execution, 403 for another owner's artifact.
- WebSocket registered replies include quarantine. Old-generation results are
  refused. Grants are never restored by server/browser reconnection.
- Durable operation views preserve their original epoch/result metadata and expose
  reconciliation_required, reconciled_at/generation, and result_sha256 where
  applicable. Journal failure is 503 journal_unavailable and fails execution closed.
