# Méthode de préparation et de qualification

## But et responsabilités

Les recettes sont du code relu et versionné avant leur exécution matérielle.
Claude prépare les scénarios et leur implémentation dans une PR ; Codex réalise
une revue indépendante ; Claude traite les remarques. Lionel arbitre les choix
fonctionnels, valide le périmètre matériel et décide de la fusion. Une revue
rédigée par l'auteur lui-même ne remplace pas la revue croisée. Chaque assistant
rapporte uniquement les actions et vérifications qu'il a réellement effectuées.

La méthode s'applique aux nouveaux tests et aux modifications de tests existants.
Les tests logiciels sans matériel peuvent être exécutés pendant la préparation.
Les gestes de récupération déjà autorisés d'une carte non restaurée restent
prioritaires ; suspendre une qualification ne signifie pas abandonner une restauration.

## 1. Définir le contrat du test

Décrire avant le code : objectif, comportement attendu, assertions, prérequis,
effets matériels, conditions d'arrêt et limites de la preuve. Distinguer un
contrôle du banc d'un contrôle du firmware consommateur.

Les recettes qui qualifient le banc générique sont versionnées dans rust_dev,
dans l'arborescence de recettes existante. Les recettes métier et OTA sont
versionnées dans leur dépôt consommateur. Ne pas déplacer les assertions métier
dans le serveur. Un dossier privé est destiné aux données d'exécution, pas à
l'unique exemplaire des tests maintenus.

Prévoir des scénarios positifs et des refus précis. Un test ne réussit pas en
acceptant n'importe quelle exception. Prévoir également les erreurs,
interruptions, opérations indéterminées et moyens de récupération pertinents.

## 2. Rédiger et proposer une PR

Utiliser une branche dédiée. Paramétrer la carte logique, la puce, le transport,
l'offset de table, les régions autorisées et le dossier de preuves. Ne pas
transformer les adresses d'une carte en valeurs destructives par défaut.

Versionner le code, les modèles de manifestes, la documentation des paramètres,
les commandes de collecte/exécution et les critères de verdict. Conserver les
identifiants privés et les consentements de session hors Git. Un modèle de
manifeste ne vaut jamais autorisation de toucher une carte.

La collecte pytest et la CI ordinaire doivent fonctionner sans carte, sans
connexion API réelle ni accès série. Les effets matériels nécessitent un opt-in
explicite ; l'opt-in ne remplace pas les consentements et le grant local.
Vérifier cette propriété avant de lancer une collecte de recettes nouvellement écrites.

La PR indique les contrôles logiciels effectués et porte explicitement la mention
« essais matériels non exécutés » tant que c'est le cas.

## 3. Revue croisée avant matériel

Présenter la PR et le SHA de tête à Codex. La revue examine :

- le comportement réellement démontré et les limites annoncées ;
- les assertions, codes de refus et états terminaux attendus ;
- la paramétrisation et l'absence de données privées ;
- les réservations, grants, délais et consentements ;
- la sauvegarde, la comparaison indépendante et la restauration ;
- les chemins d'échec, la clôture et l'absence de rejeu implicite ;
- la traçabilité des versions et la séparation simulation/matériel.

Claude corrige les remarques dans la branche. Une correction substantielle des
scénarios ou assertions demande une nouvelle revue du nouveau SHA.
Le point de validation est traçable : URL de PR, SHA, revue indépendante,
remarques bloquantes résolues et accord de Lionel pour engager la campagne.
Le seul statut « PR ouverte » ou une CI verte n'autorise pas les essais matériels.

## 4. Exécuter le commit validé

Une fusion préalable n'est pas nécessaire. Exécuter le commit relu de la branche
avec un arbre propre pour les sources pertinentes. Relever séparément les versions
du serveur réellement lancé, du client, de l'agent navigateur et de la recette.
Ne pas changer les sources du serveur en cours de campagne ni le redémarrer
simplement parce qu'une branche de recettes a changé.

Avant le premier effet matériel, vérifier la carte réelle, le transport, le
manifeste de session, les secteurs approuvés et les autorisations applicables.
Réutiliser les consentements explicites encore valables dans leur périmètre ;
demander seulement les informations ou gestes réellement manquants.
Lire une flash peut entrer dans le loader et réinitialiser le MCU.

Le navigateur du PC de l'opérateur reste seul propriétaire de l'USB.
Utiliser le client officiel, Campaign et Orchestrator selon les contrats existants.
Prévoir le grant et la réservation pour l'opération, sa stabilisation et la récupération.
Ne jamais renouveler un grant à distance.

Chaque exécution reçoit un nouveau dossier de preuves. Ne pas modifier une
assertion pour faire passer un run en cours. En cas de défaut de recette :
conserver le résultat, corriger dans Git, refaire la revue nécessaire, puis
lancer une nouvelle exécution depuis le nouveau commit. Aucune mutation
incertaine n'est relancée automatiquement.

## 5. Examiner les résultats et décider

Rapporter PASS, FAIL, SKIP, BLOCKED ou INDETERMINATE avec leurs preuves et leur
périmètre exact. Distinguer défaut du banc, du client, du firmware, de la recette,
de l'infrastructure ou geste opérateur. Un PASS simulé n'est pas un PASS matériel ;
un cycle réussi n'est pas une preuve d'endurance.

Vérifier la restauration, la clôture, les réservations, les grants et les
opérations restantes. Une restauration bloquée est signalée immédiatement.
La revue des résultats précède la décision de fusion de Lionel ; aucun assistant
ne fusionne ou ne clôt une issue par déduction.

## Code public et données privées

| Versionné | Privé, hors Git |
| --- | --- |
| Recettes et utilitaires réutilisables | Tokens et configuration privée |
| Modèles de manifestes et documentation | Manifeste réel comportant des informations privées |
| Matrice des scénarios et critères | Dumps, sauvegardes et captures UART brutes |
| Compte rendu expurgé, versions et limites | Traces détaillées et consentements de session |

Les preuves et sauvegardes doivent être placées sur un stockage dont la
persistance est vérifiée. Dans le devcontainer actuel, /root/bench-recipes et
/root/bench-evidence ne sont pas couverts par les volumes d'état documentés.
Ne pas supposer qu'ils survivent à une recréation du conteneur. Les préserver
avant toute reconstruction, sans déplacer ni supprimer le journal du serveur.

## Reprise des premiers essais S3

Le retour opérateur décrit un Q1 S3/UART réussi en un cycle avec une recette
locale v2, après une v1 en erreur d'import. Préserver exactement ces versions et
leurs preuves ; ne pas attribuer ce PASS à une future recette Git différente.

Intégrer une version réutilisable de Q1 dans une PR, puis préparer Q2
(désarmement après expiration/révocation) et Q3 (redémarrage coordonné) pour revue.
L'offset 0x10000 de la table et la région 0x400000 + 0x2000 sont des paramètres de
la session S3 rapportée, jamais une autorisation universelle. Suspendre les
nouveaux essais matériels jusqu'à validation des recettes. Rejouer Q1 depuis
le commit validé avant d'étendre aux autres scénarios puis au C3.
L'OTA demeure hors périmètre de cette première qualification du banc.
