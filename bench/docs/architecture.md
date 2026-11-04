# Audit et architecture

## Références examinées

| Branche | SHA | Relation à `bench/audit-final` |
|---|---|---|
| `main` | `e938657` | 21 commits ancêtres |
| `bench/p5c-preparation` | `511897b` | 5 commits ancêtres |
| `bench/canonical-preparation` | `ddc214e` | 2 commits ancêtres |
| `bench/rollback-preparation` | `1d5f236` | 1 commit ancêtre |
| `bench/audit-final` | `9938b64` | Référence complète retenue |

La comparaison GitHub confirme `behind_by:0` pour chacune de ces comparaisons vers audit-final. Aucun cherry-pick n’est nécessaire. L’ancienne suite a été exécutée avant refonte : **78 tests Python et 57 tests Node passent**. Ce résultat concerne uniquement le logiciel historique.

## Constats

L’ancien serveur utilise `ThreadingHTTPServer`, une file de commandes avec claim/ack et des inventaires de fichiers. Il accepte des requêtes de commande sans authentification. Les limites de taille des corps ne sont pas définies. La page contient l’UI, le moteur matériel, les règles Wi-Fi, `OTA ready`, l’auto-reset et le pilotage HTTP LAN. La capture et les archives de dumps se mêlent aux résultats de campagne dans le répertoire du banc. Les clients spécialisés OTA, Improv et les modèles de rollback résident dans l’infrastructure.

Deux composants ont une valeur matérielle distincte :

- `bench-transport.js` : réveil sur réception, horloge monotone, classification des lectures et correction du bridage des minuteurs de l’onglet.
- `flash-op.js` : classification selon les commandes effectivement envoyées, MD5 du chargeur, résultat indéterminé dès qu’un effacement peut avoir commencé. La refonte ajoute la restauration des méthodes instrumentées après chaque opération, pour éviter d’empiler les wrappers entre écritures.

Les recettes historiques sont retirées, sans être transplantées dans d’autres dépôts. Elles restent dans les branches sources. L’ancien audit VerifiedBoot est conservé comme référence historique uniquement, avec une mention de son statut.

## Découpage implémenté

| Module | Responsabilité |
|---|---|
| `protocol/` | Validation des commandes, tailles, plans flash, HTTP privé et primitives binaires |
| `server/core.py` | Modèle de cartes, réservations, opérations, artefacts et anneaux d’événements |
| `server/ui_sessions.py` | Sessions navigateur éphémères, accès direct loopback et contrôles Host/origine |
| `server/bench_server.py` | REST, authentification, scopes, WebSocket, SSE, contenu statique fermé |
| `web/agent.js` | Authentification agent, génération de session, rendez-vous offer/ready/execute, contrôle du délai, transport d’événements et artefacts |
| `web/esp-engine.js` | Web Serial, esptool, console/chargeur, identité physique, flash, UART, EN/BOOT et déclencheurs génériques |
| `web/protocol.js` | Validation indépendante des plans flash et résolution des manifestes locaux |
| `web/api-client.js`, `web/ui.js` | Client manuel de la même API ; autorisations locales explicites |
| `tools/sim_agent.py` | Agent externe simulant un périphérique, sans accès USB |
| `examples/client.py` | Client indépendant, export et assertions appartenant à l’appelant |

Python est conservé. `aiohttp` apporte HTTP asynchrone, WebSocket authentifié, heartbeat et SSE, sans implémenter un protocole WebSocket artisanal ni ajouter un framework applicatif lourd. Toutes les mutations du broker sont synchrones dans une seule boucle asyncio : le test concurrence vérifie qu’une seule réservation gagne. Les transferts réseau ne gardent aucun verrou matériel.

## Exécution, sécurité et récupération

Pour les agents externes, un token agent est lié à une liste explicite de cartes. Une carte n’a qu’une génération active. Chaque token client porte `read`, `control` et éventuellement `program`. Une réservation est liée à l’identité du client, pas à la connexion HTTP. Un client différent ne peut ni lire ses résultats, ni renouveler/libérer sa réservation.

`queued → offered → running → completed|failed|indeterminate|cancelled` est le chemin d’exécution. `queued/offered` peuvent devenir `expired` ou `cancelled` sans exécution. Le serveur marque `running` avant d’envoyer `execute` : si cette transmission est perdue, il annonce une incertitude, jamais une réussite. Aucun identifiant n’est remis en file après offre ou démarrage.

L’agent établit une correspondance conservative entre les horloges monotones au message d’authentification. Une échéance serveur est projetée depuis l’instant **d’envoi** de l’authentification, avec une marge supplémentaire de 100 ms : la latence réduit la fenêtre d’exécution au lieu de l’allonger. Aucun réglage d’horloge civile n’est nécessaire. La dérive d’horloge n’est pas corrigée pendant une très longue connexion : reconnecter entre campagnes longues. Les tests couvrent un message execute devenu périmé en transit et le réveil tardif d’un onglet.

Les données firmware sont téléchargées et vérifiées avant toute écriture, puis les régions et l’identité physique sont vérifiées à nouveau dans l’agent. Les chevauchements sont évalués à la granularité des secteurs d’effacement de 4 Kio, même si les octets utiles ne se chevauchent pas. Aucun manifeste ne peut déclencher un effacement implicite. Les manifestes résolvent exclusivement des fichiers locaux déjà uploadés et une seule famille de puce correspondante.

La perte de l’agent invalide la réservation. Une commande déjà `running`, un timeout d’exécution ou la libération d’une réservation en cours de commande produit `indeterminate` et met la carte en quarantaine. Les commandes risquées déjà en attente sont annulées. La relecture, les partitions, l’identification et les repères restent possibles sous réservation pour préparer la récupération. L’opérateur peut lever le blocage localement après vérification et seulement quand aucune exécution n’est encore en suspens. L’état dangereux est persisté dans le navigateur **avant** toute exécution ; une page rechargée ne rouvre pas spontanément la console.

Une annulation demandée sur une commande en cours positionne `cancel_requested`; elle ne prétend pas interrompre esptool. Un résultat tardif après timeout est conservé dans `late_result`, sans transformer le résultat indéterminé en succès. Le serveur accuse réception de chaque résultat avec son état de quarantaine. Le navigateur ne lève pas son blocage persistant et ne retourne pas automatiquement à la console avant cet accusé. Un accusé perdu laisse le blocage actif, sans réexécution matérielle. Une panne de retour console est signalée au serveur pour conserver la quarantaine.

Une fin d’exécution connue après annulation demandée peut être `completed` avec `cancel_requested:true` : la demande n’a pas interrompu le matériel.

Les requêtes HTTP LAN sont exécutées uniquement par le navigateur, vers une origine RFC1918 littérale déclarée localement, sans credentials automatiques ni redirection, GET/POST et tailles/délais bornés. La réponse `no-cors` est opaque : `http_status:"unknown", verified:false`. Elles ne prouvent aucune assertion métier. HTTPS de la page, politiques navigateur de réseau local et contenu mixte peuvent bloquer cet accès ; c’est une limitation explicite à qualifier.

Le serveur ne sert aucun répertoire arbitraire, n’exécute aucune commande système et n’écrit aucune capture de qualification. Les tokens ne sont jamais dans les URL ; les logs HTTP d’accès sont désactivés. Le flux brut reste sensible s’il contient ce que le firmware a émis. Les tokens agents sont une frontière de confiance : un agent autorisé est responsable de la véracité de ses déclarations matérielles et des grants locaux.

## Limites du premier contrat

- État en mémoire, un seul processus autoritaire, pas de stockage durable ni de déploiement cloud.
- Identité de carte logique associée par l’opérateur ; absence de preuve cryptographique d’identité du matériel.
- Les captures ont un historique borné : `lost`, `source_lost`, `epoch` et les déconnexions permettent de détecter les lacunes, pas de récupérer des octets déjà perdus. Les clients doivent s’abonner et stocker leurs preuves.
- Pas d’arrêt forcé garanti d’un effacement ou d’une programmation ; quarantaine et récupération explicites.
- Reconnexion USB automatique limitée au port préalablement choisi et à la même identité physique, sans rejeu de commande. La déconnexion volontaire désactive cette reprise. Un blocage persistant interdit le retour console automatique ; la liaison serveur seule se reconnecte explicitement. Toute ouverture du chargeur ou de la console peut redémarrer la carte.
- Le retour console ouvre le port et peut réinitialiser la carte ; l’absence de reset supplémentaire et la capture depuis la ROM restent à qualifier physiquement.
- Dépendance navigateur externe esptool-js 0.7.0 ; les particularités S3/C3 et les modes USB doivent être validés sur cartes réelles.

Seul l’agent physique authentifié peut libérer un pilotage abandonné après confirmation opérateur. Le serveur refuse cette libération tant que le matériel reste en exécution ou non stabilisé ; elle ne lève pas la quarantaine. Le maintien d’écran est une demande Screen Wake Lock pendant le travail, sans garantie d’exécution en arrière-plan.

Le client manuel verrouille la sélection sur la carte réservée ; les commandes revérifient cette correspondance et utilisent la famille de puce associée à la réservation. La liste peut être actualisée sans changer la cible active.

## Session de la page et pilotage sans réservation manuelle

La session locale du navigateur authentifie ses appels REST et son WebSocket sans exposer une clé API. Les clients externes gardent leurs tokens et scopes. Le serveur associe l’autorité locale d’une opération à son agent physique, sans grant global. `manual-operations` réserve atomiquement la carte et réutilise le broker normal. `settled`, envoyé après traitement de l’accusé et retour console, borne la libération automatique : la carte ne devient pas disponible prématurément pendant une réouverture du port. Les déclencheurs gardent le droit nécessaire à leur durée d’armement. La page ne propose plus de prise de main/renouvellement/délégation préalables aux commandes.

La session automatique vérifie réseau loopback, Host local, origine exacte des mutations et WebSocket, et cookie HttpOnly/SameSite Strict. Un relais de devcontainer peut être déclaré par son pair TCP réel et l’origine publique locale exacte ; les en-têtes transférés ne peuvent pas accorder cette confiance. Ce profil suppose une redirection réservée à la boucle locale du PC et n’est pas une solution de connexion utilisateur depuis le LAN. Aucune origine réseau distante n’est implicitement privilégiée parce que le serveur sert la page.
