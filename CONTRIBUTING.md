# Contribuer à Simple API 3cx

Merci de proposer des changements ciblés et vérifiables. Ce projet pilote un PBX : une modification qui semble mineure peut affecter la distribution des appels.

## Avant de proposer un changement

1. Ouvrir une issue qui décrit le besoin ou le défaut observé, sans y coller de clé API, mot de passe SIP, URL de lien navigateur ou donnée personnelle d’appel.
2. Expliquer la version de Debian et de 3CX concernée, ainsi que le résultat attendu.
3. Préparer une modification limitée à un comportement ou une documentation cohérente.

## Vérifications locales

~~~bash
python3 -m py_compile simple_api_3cx.py
python3 -m unittest discover -s tests -v
bash -n install.sh
sh -n scripts/simple-api-3cx
~~~

Le contrôleur C# dépend de <code>/usr/lib/3cxpbx/3cxpscomcpp2.dll</code> et ne peut être compilé que dans un environnement 3CX compatible. Un test d’écriture sur un PBX doit confirmer l’état de la file ciblée **et** celui des autres files du poste, puis rétablir l’état initial.

## Règles du projet

- Lire le PBX avec des requêtes PostgreSQL en lecture seule. Ne pas écrire directement dans les tables 3CX.
- Passer par les mécanismes 3CX pour toute action et vérifier l’état obtenu.
- Conserver les clés et secrets hors du dépôt, des exemples et des journaux.
- Mettre à jour le README et le changelog lorsqu’un endpoint ou une procédure change.
- Signaler les limites de compatibilité observées ; ne pas présumer qu’une version 3CX non testée fonctionne.

Les contributions au code et à la documentation sont publiées sous la [licence MIT](LICENSE) du dépôt.
