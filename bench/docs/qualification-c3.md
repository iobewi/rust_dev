# Lot ESP32-C3 de l'issue #2

Ces recettes ajoutent identité, flash, partitions et reset C3 aux preuves automatiques
de qualification v2. Elles ne ferment pas l'issue #2 : programmation, interruptions
complexes, deux cartes simultanées et les autres lots restent distincts. Aucun essai
physique de ces nouveaux fichiers n'a été effectué par la session de développement.
Le contrôle C3 privé antérieur (identité et deux lectures) reste attaché à sa propre
recette ; il ne vaut pas exécution des tests ajoutés ici.

La carte doit être connectée dans le navigateur au même serveur que le client pytest.
Réutiliser le token client read/control par `BENCH_CLIENT_TOKEN`, sans l'afficher.
Configurer `BENCH_URL`, `BENCH_DEVICE` et les déclarations de versions/transport selon
la documentation v2. Aucune programmation, aucun effacement ni grant n'est nécessaire.
Identification et lectures peuvent redémarrer le firmware. Pas de comparaison avec
la sauvegarde S3 : la carte C3 a sa propre identité, sa capacité et son contenu.

## Identité, lectures et table

Depuis la racine du dépôt :

```sh
umask 077
mkdir -p /qualification-runs/c3
python -m pytest bench/recipes/qualification_v2/test_c3.py --bench \
  --bench-results /qualification-runs/c3/read-only \
  --junitxml=/qualification-runs/c3/read-only.xml -v
```

La recette vérifie `esp32c3`, la capacité flash, deux lectures de 4 Kio à zéro de
tailles/digests exacts et d'octets identiques. Une S3 sélectionnée par erreur échoue
avant les lectures. L'identité sauvegardée ne reprend pas la MAC.

La table est cherchée par **deux lectures candidates bornées**, à 0x8000 et 0x10000.
Seul `partition_table_invalid` autorise le passage à l'autre adresse ; un défaut USB
ou une autre erreur fait échouer le test. Une seule table valide est exigée. Rien
n'est écrit. Si le firmware a une adresse connue, limiter explicitement le test :

```sh
python -m pytest bench/recipes/qualification_v2/test_c3.py --bench \
  --bench-c3-table-address 0x8000 \
  --bench-results /qualification-runs/c3/known-table \
  --junitxml=/qualification-runs/c3/known-table.xml -v
```

Les adresses candidates ne constituent pas une garantie : une table ailleurs ou
deux tables plausibles font échouer le lot avec un diagnostic demandant l'adresse
du firmware. L'adresse acceptée, les résultats des sondes, les entrées et le dump
privé sont enregistrés. Un décodeur indépendant recoupe labels, adresses et tailles ;
les chevauchements et débordements de capacité sont refusés. Ce test ne qualifie pas
la validité des images applicatives, la signature ou la politique de boot.

## Reset explicite et capture ROM

Le troisième test est SKIP par défaut. Une fois le redémarrage de cette carte autorisé :

```sh
python -m pytest bench/recipes/qualification_v2/test_c3.py -k reset --bench \
  --bench-c3-reset --bench-results /qualification-runs/c3/reset \
  --junitxml=/qualification-runs/c3/reset.xml -v
```

Il vérifie la famille C3, prend le curseur UART avant le reset, exige une méthode
`en` ou `usb_serial_jtag` et attend `ESP-ROM:esp32c3`. Un reset annoncé sans marqueur
ROM n'est pas un PASS. Sur méthode USB Serial/JTAG, IO0 ne doit pas être exposé ;
aucune commande EN/IO0 manuelle n'est envoyée. Sur un transport sans reset, le lot
est SKIP, jamais présenté comme validé.

Le marqueur ROM prouve une émission observée après le curseur, pas un démarrage
correct de l'application. Si l'USB disparaît ou si la génération change pendant le
reset, l'attente de continuité échoue : conserver les preuves et traiter ce scénario
séparément ; ne pas reconnecter silencieusement l'ancienne instance Board.

## État des preuves et suite opérateur

Chaque test conserve recette/supports exacts, hashes, before/after, réservations,
diagnostics, compteurs, événements filtrés, verdicts des trois phases et nettoyage.
Voir `qualification-v2.md` pour les limites des déclarations de transport/page et
l'absence d'UART brut. Ne publier ni dumps, ni captures sensibles, ni tokens.

À exécuter avec Claude dans le conteneur qui accède réellement au serveur :

1. Installer le client de ce SHA ; relever aussi les versions serveur et page.
2. Vérifier la carte C3 et l'état propre ; lancer le lot lecture/table.
3. Avec le reset autorisé, lancer séparément le lot reset ; relever méthode et ROM.
4. Vérifier l'état final et fournir l'archive expurgée, avec JUnit, sources et preuves.
5. Rapporter PASS/FAIL/NON EXÉCUTÉ par sous-cas et par transport. Ne pas fermer #2
   à partir de ces seuls essais ; ne pas qualifier les écritures par déduction.
