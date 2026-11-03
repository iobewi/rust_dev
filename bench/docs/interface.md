# Interface d’instrumentation

L’interface sert à connecter et piloter les cartes, observer le transport brut et inspecter les résultats matériels. Elle n’exécute pas les recettes propres aux projets et ne produit aucun verdict de qualification.

## Deux liaisons distinctes

**Connecter un port série** ouvre le sélecteur Web Serial du navigateur. Les ports déjà autorisés sont proposés par une action distincte ; aucune reprise automatique ne redémarre une carte après interruption. L’identification initiale passe par le chargeur ESP puis, si aucun blocage n’est mémorisé, revient à la console.

La capture locale est accessible sans token API. Les octets restent dans un tampon temporaire borné ; aucune commande manuelle de programmation, reset, émission UART ou autre contrôle ne contourne le broker.

**Pilotage distant et accès…** ouvre les paramètres de session : token agent, token client, identifiant logique autorisé et éventuelles origines HTTP LAN. L’origine du serveur est celle de la page ; il n’y a pas de champ d’adresse de serveur redondant. Les tokens restent en mémoire de page et ne sont pas enregistrés. Un navigateur peut aussi ouvrir une session de client API sans posséder lui-même le port physique.

## Propriété du pilotage

- **Prendre le pilotage manuel** réserve exclusivement la carte sélectionnée pour quinze minutes.
- **Autoriser le pilotage distant** termine cette réservation et laisse le port Web Serial et la capture ouverts. Les clients distants doivent toujours s’authentifier et prendre leur propre réservation.
- **Pilotée par : identifiant** indique une réservation détenue par un autre client. L’interface ne propose pas de reprise forcée.
- La famille de puce et la capacité du plan sont liées à la réservation ; un changement incohérent de cible est refusé.
- La durée restante est visible, avec une action de renouvellement. Une réservation expirée nécessite une nouvelle prise de contrôle.

Les autorisations locales temporaires sont séparées de la propriété du pilotage : avoir la réservation ne suffit pas à programmer, effacer, émettre sur UART ou armer un déclencheur privilégié.

## Organisation

La barre de session montre la carte, les liaisons USB/serveur, le mode et le détenteur du pilotage. Le panneau latéral expose l’identité matérielle, les droits et l’état de récupération.

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

Les exports sont des données de transport, pas des rapports de recette. Les projets restent responsables de leur conservation durable et de leurs assertions.

## Validation

Les tests Node vérifient l’intégrité du tampon, la continuité UTF-8, les pertes, le gel d’affichage, les entrées numériques strictes, la validation des plans, le téléchargement vérifié et le réassemblage SSE. Le parcours Chromium utilise Web Serial et un chargeur ESP simulés, puis les vraies interfaces REST et WebSocket du banc : capture sans token, ouverture de session, réservation, partitions, dump, plan flash, confirmation annulée et délégation sans fermeture du port.

Le banc ne prétend à aucune qualification matérielle réelle. Les essais physiques restent suivis par l’issue #2.
