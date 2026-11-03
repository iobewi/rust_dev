# Interface d’instrumentation

L’interface sert à connecter et piloter les cartes, observer le transport brut et inspecter les résultats matériels. Elle n’exécute pas les recettes propres aux projets et ne produit aucun verdict de qualification.

## Deux liaisons distinctes

**Connecter un port série** ouvre le sélecteur Web Serial du navigateur. Les ports déjà autorisés sont proposés par une action distincte ; après débranchement/rebranchement, le port précédemment choisi est reconnecté automatiquement si le navigateur le reconnaît parmi les ports autorisés. La puce, la capacité, le RDID et la MAC sont comparés avant tout retour console. Une autre carte exige une sélection explicite. Aucune commande interrompue n’est rejouée ; un blocage persistant est conservé, sans retour console automatique. Une déconnexion volontaire désactive cette reprise. La reconnexion peut changer le mode de démarrage, comme toute ouverture du chargeur. L’identification initiale passe par le chargeur ESP puis, si aucun blocage n’est mémorisé, revient à la console.

La capture locale est accessible sans token API. Les octets restent dans un tampon temporaire borné ; aucune commande manuelle de programmation, reset, émission UART ou autre contrôle ne contourne le broker.

**Paramètres du banc…** ouvre les paramètres de session : token agent, token client, identifiant logique autorisé et éventuelles origines HTTP LAN. L’origine du serveur est celle de la page ; il n’y a pas de champ d’adresse de serveur redondant. Les paramètres se replient après ouverture de session réussie et restent accessibles par le bouton de paramètres. Les tokens restent en mémoire de page et ne sont pas enregistrés. Un navigateur peut aussi ouvrir une session de client API sans posséder lui-même le port physique.

## Propriété du pilotage

- **Prendre la main** réserve exclusivement la carte sélectionnée. La réservation de quinze minutes est renouvelée automatiquement tant que la page conserve le pilotage ; sa gestion est masquée dans l’usage courant.
- **Autoriser le pilotage distant** termine cette réservation et laisse le port Web Serial et la capture ouverts. Les clients distants doivent toujours s’authentifier et prendre leur propre réservation.
- **Pilotée par : identifiant** indique une réservation détenue par un autre client. Le bouton normal de prise en main ne force jamais une réservation concurrente. Dans « Client bloqué », l’opérateur de l’agent physique dispose de « Libérer le pilotage bloqué… » : confirmation locale, révocation du grant, annulation des commandes en attente et libération de la réservation sans son identifiant. Le serveur refuse cette action tant qu’une exécution matérielle reste en cours ou en suspens. Elle ne lève jamais la quarantaine.
- La famille de puce et la capacité du plan sont liées à la réservation ; un changement incohérent de cible est refusé.
- Une réservation expirée, une nouvelle génération ou un changement d’epoch nécessite une nouvelle prise de contrôle. Les anciennes commandes ne sont jamais resoumises.

L’action manuelle sensible propose une autorisation locale au moment nécessaire, avec une durée sélectionnée (1 minute, 5 minutes par défaut, 30 minutes ou 2 heures). L’interface attend sa visibilité côté serveur avant de soumettre la commande. Le client distant utilise le panneau avancé d’autorisation ; la révocation reste visible tant que le grant est actif. Les autorisations locales temporaires sont séparées de la propriété du pilotage : avoir la réservation ne suffit pas à programmer, effacer, émettre sur UART ou armer un déclencheur privilégié.

## Organisation

La barre de session montre la carte, les liaisons USB/serveur, le mode et le détenteur du pilotage. Le panneau latéral expose l’identité matérielle, les droits et l’état de récupération.

La console UART a un fond contrasté et une hauteur réglable par son bord inférieur. Le journal distinct de l’agent affiche les connexions, ouvertures de console/chargeur, commandes de tous ses clients, transferts, résultats et durées. Il est borné à 300 lignes et ne journalise ni tokens, ni arguments de commande, ni contenu UART/HTTP, ni texte arbitraire des exceptions. Le tampon UART reste potentiellement sensible.

La console reste visible pendant l’utilisation des onglets :

- **Partitions** : table réelle, types numériques, offsets, tailles et fins exclues ; relecture par partition.
- **Plan flash** : fichiers uploadés, adresses explicitement renseignées, tailles et empreintes. Les manifestes alimentent ce même plan et ne déclenchent pas une installation immédiate. La validation refuse les bornes invalides et les secteurs d’effacement communs. Une confirmation récapitule la carte et les régions avant soumission.
- **Lecture mémoire** : région arbitraire, aperçu hexadécimal limité à 256 octets, taille et SHA-256. Le téléchargement est proposé seulement après vérification des octets reçus.
- **Opérations** : commandes du client manuel, états, durée observée et résultat structuré. Les résultats des autres clients restent privés ; le propriétaire de la réservation est visible dans l’état de carte, sans exposer son identifiant de réservation.
- **Déclencheurs et outils** : baudrate, motif UART, action, délai, expiration et requête HTTP LAN opaque.

L’effacement global garde une action et une confirmation distinctes.

## Capture et affichage

`ConsoleBuffer` conserve des copies des octets RX, séparées du texte affiché. Texte/hex, recherche, horodatage, pause et effacement de l’affichage ne modifient pas l’export brut. La pause fige le curseur de présentation pendant que la capture continue. Les caractères UTF-8 fragmentés sont décodés en continu ; le mode hex préserve la lecture des données binaires.

Le tampon de chaque carte est borné à 1 Mio / 4 096 événements. L’interface affiche les évictions locales. L’abonnement SSE reprend avec le curseur du serveur et signale les pertes de source, les événements devenus irrécupérables et les changements d’epoch. Un flux local déjà capturé n’est pas ajouté une deuxième fois par son écho serveur.

Le suivi du direct est automatique en bas du tampon. Remonter dans l’historique préserve la position ; un indicateur et « Reprendre le défilement » permettent de retrouver le direct.

Pendant l’exécution par l’agent et jusqu’à l’accusé serveur, Screen Wake Lock demande le maintien de l’écran allumé. Le verrou est libéré au repos, à la déconnexion ou au départ de la page, et redemandé au retour visible si une opération continue. Refus et indisponibilité sont affichés sans bloquer le matériel. Cela ne garantit pas l’exécution d’un onglet masqué.

Les exports sont des données de transport, pas des rapports de recette. Les projets restent responsables de leur conservation durable et de leurs assertions.

## Validation

Les tests Node vérifient l’intégrité du tampon, la continuité UTF-8, les pertes, le gel d’affichage, les entrées numériques strictes, la validation des plans, le téléchargement vérifié et le réassemblage SSE. Le parcours Chromium utilise Web Serial et un chargeur ESP simulés, puis les vraies interfaces REST et WebSocket du banc : capture sans token, ouverture de session, réservation, partitions, dump, plan flash, confirmation annulée et délégation sans fermeture du port.

La campagne matérielle rapportée sur d548dab couvre une S3 N16 par pont UART externe, avec des limites et des cas non exécutés consignés dans l’issue #2. Ces corrections sont vérifiées par tests logiciels ; elles ne valent pas requalification matérielle. L’observation de redémarrages répétés reste à investiguer : aucun correctif de reset n’est revendiqué sans reproduction.
