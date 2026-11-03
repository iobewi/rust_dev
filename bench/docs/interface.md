# Interface d’instrumentation

La page connecte les cartes, présente le transport brut et demande les opérations au serveur. Les recettes, assertions et preuves durables restent dans les projets clients.

## Connexion locale sans token

En accès local par `localhost`, `127.0.0.1` ou `::1` (direct ou redirection locale explicitement déclarée), la page ouvre automatiquement sa session sur le serveur. Un cookie HttpOnly, SameSite Strict, partagé par REST et WebSocket, remplace les deux champs de tokens. Le cookie ne contient aucune clé API externe et n’est pas accessible au JavaScript. La session est éphémère, expire après huit heures et disparaît au redémarrage serveur.

En mode direct, le serveur vérifie la connexion réseau de boucle locale, le Host local et l’origine exacte des mutations et du WebSocket. Pour un devcontainer ou un relais local, `BENCH_UI_FORWARD_PEERS` déclare les pairs de confiance et `BENCH_ORIGIN` l’URL locale exacte du navigateur. Cela permet une adresse interne et un Host réécrit différents, sans faire confiance aux en-têtes transférés. Les requêtes de site tiers et les relais non déclarés restent refusés. L’ouverture automatique n’est pas disponible par un accès LAN ou un nom DNS arbitraire non authentifié. Un déploiement réseau avec authentification utilisateur n’est pas implémenté dans cette version ; aucune session privilégiée n’est accordée simplement parce que le serveur fournit la page.

**Connecter un port série** reste le seul choix matériel nécessaire. Le navigateur demande sa propre permission Web Serial, identifie la carte puis ouvre la console si aucun blocage n’est mémorisé. La liaison agent au serveur s’établit automatiquement. **Paramètres du banc…** conserve l’identifiant logique autorisé et les origines HTTP LAN ; **Rétablir la liaison au serveur** sert à une reconnexion explicite après une interruption réseau.

Les identifiants de cartes disponibles viennent de la configuration `devices` des agents ; en l’absence d’agent externe configuré, le serveur utilise `esp32-local`. Un client externe tel que Claude conserve son token REST indépendant et ses scopes. Le navigateur ne le reçoit pas et ne le stocke pas.

## Commandes directes

Il n’y a plus de bouton « Prendre la main », de renouvellement manuel ou de délégation nécessaire avant une action. Un clic sur Identifier, Lire, Reset ou une autre commande envoie une opération manuelle au serveur. Celui-ci prend atomiquement une réservation exclusive, vérifie la génération de carte, puis utilise la même file et le même agent que les clients externes.

La réservation est libérée après réception du résultat par le serveur et confirmation par l’agent que le traitement de l’accusé et le retour console sont terminés. Elle ne disparaît pas prématurément pendant une réouverture de port. Une commande annulée ou expirée avant départ libère sa réservation ; une issue matérielle non stabilisée conserve les protections d’exclusion et de quarantaine.

Les actions restent refusées si un autre client possède la carte. La page affiche son propriétaire et son activité. « Client bloqué » permet à l’opérateur physique de libérer une réservation abandonnée après confirmation, sans redémarrer le serveur ; cette action refuse un matériel encore en cours de traitement et ne lève pas la quarantaine.

Un déclencheur armé conserve automatiquement sa réservation jusqu’à son expiration ou à son désarmement. La page peut le désarmer ou effectuer d’autres commandes dans cette réservation ; aucun renouvellement manuel n’est demandé. Les accès matériels restent sérialisés.

## Une confirmation pour les actions locales sensibles

Le plan flash conserve une confirmation explicite de la carte, de sa génération et des régions. L’effacement global possède une confirmation distincte. Ces confirmations autorisent uniquement l’opération manuelle adressée à l’agent physique de la même session ; elles n’ouvrent aucun grant temporaire utilisable par Claude.

Les commandes locales UART, BOOT, HTTP et déclencheurs ne nécessitent plus un bouton préalable d’accès privilégié. Les contrôles serveur, le choix explicite de commande et les restrictions matérielles restent applicables. Une carte en quarantaine reste limitée aux commandes de vérification autorisées.

Pour les clients externes, la réservation, le scope `program`, le consentement `allow_destructive:true` et le grant local restent nécessaires selon la commande. Le panneau avancé **Autoriser les opérations distantes sensibles** permet de choisir 1 minute, 5 minutes par défaut, 30 minutes ou 2 heures. La révocation reste visible pendant le grant. Une autorisation distante n’est jamais créée automatiquement par un clic sur une commande locale.

## Consoles et outils

La console UART contrastée et redimensionnable reste visible au-dessus des onglets Partitions, Plan flash, Lecture mémoire, Opérations et Déclencheurs/outils. Les partitions sont lues à l’adresse explicite du projet ; aucune table valide à l’adresse demandée produit un diagnostic dédié. Manifestes et binaires alimentent le même plan ; aucun fichier ne déclenche une écriture à son ajout.

`ConsoleBuffer` conserve jusqu’à 1 Mio / 4 096 événements par carte. Les lignes texte suivent les fins de ligne du firmware (LF, CRLF, CR), et non les fragments USB. Une ligne partielle reste visible et se complète en direct. Horodatage et séquence correspondent au premier fragment retenu de la ligne. La recherche porte sur les lignes assemblées. Hex, pause, recherche et effacement de l’affichage ne changent pas l’export brut. Le retour au direct est explicite après consultation de l’historique.

Le journal de l’agent conserve 300 lignes : connexions, modes console/chargeur, commandes, progression, transferts, résultats et durées. Les erreurs indiquent la phase, le type de transport et les délais restants. Les nouveaux diagnostics n’exposent ni tokens, ni arguments, ni texte arbitraire d’exception. Le flux UART peut lui-même contenir des données sensibles du firmware.

Les lectures demandent au plus dix minutes et utilisent des blocs internes d’au plus 1 Mio. Aucun bloc échoué n’est rejoué et aucun dump partiel n’est publié. Le téléchargement vérifie la taille et le SHA-256.

## Reconnexion et anti-veille

Après débranchement/rebranchement, seul le port précédemment choisi est repris automatiquement si le navigateur le reconnaît. Puce, capacité, RDID et MAC sont revérifiés avant retour console. Aucune opération interrompue n’est rejouée. Un blocage persistant interdit le retour console automatique ; une déconnexion volontaire désactive la reprise. Ouvrir le chargeur ou revenir à la console peut redémarrer la carte.

Screen Wake Lock demande le maintien d’écran pendant l’activité de l’agent, jusqu’au traitement de l’accusé et du retour console. Il est libéré au repos, à la déconnexion ou au départ de page. La visibilité, la réacquisition et les refus du navigateur sont signalés ; cela ne garantit pas l’exécution d’un onglet masqué.

## Validation

Les tests serveur couvrent sessions locales, origine/Host, cookies, expiration, maintien des accès Bearer, concurrence, autorité locale limitée à son agent, confirmation destructive et libération automatique après stabilisation. Les parcours Chromium utilisent un port et un chargeur simulés, avec les vraies interfaces REST et WebSocket.

Les retours matériels sur les SHA antérieurs ne qualifient pas cette évolution de session. Les cas non exécutés de l’issue #2 restent ouverts, notamment programmation, autres cartes/interfaces, onglet masqué et libération effective de l’anti-veille. Les anciens resets et l’échec historique vers 350 s n’ont pas de cause isolée démontrée.
