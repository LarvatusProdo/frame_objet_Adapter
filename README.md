# frame_dict — éditeur graphique d'objets Python

Petit éditeur **tkinter** pour consulter et modifier des variables Python (`dict`, `list` imbriqués…) dans une arborescence, directement depuis un script ou à partir d'un fichier JSON.

Un seul fichier, aucune dépendance externe : uniquement la bibliothèque standard.

![Aperçu de l'éditeur](docs/apercu.png)

## Fonctionnalités

- **Arborescence colorée** : une couleur par type (`str`, `int`, `float`, `bool`, `None`…), et un fond distinct en gras pour les conteneurs `dict` et `list`.
- **Édition en place** : double-clic sur une cellule pour modifier une **valeur** ou renommer une **clé** directement dans l'arbre, sans boîte de dialogue.
- **Navigation au clavier** entre les cellules (`Tab`, flèches), pour enchaîner les modifications.
- **Chemins de fichiers** : les chaînes qui ressemblent à un chemin (`C:/Users`, `./config`, `~/data.csv`…) sont détectées et reçoivent un bouton pour choisir un dossier ou un fichier.
- **Annulation** des modifications (`Ctrl+Z`, jusqu'à 50 actions).
- **Lecture / écriture JSON**.
- **L'objet d'origine n'est jamais modifié** : l'éditeur travaille sur une copie et ne renvoie le résultat qu'après validation.
- **Architecture extensible** : un nouveau type d'objet se prend en charge en écrivant un *adaptateur*, sans toucher à l'interface.

## Prérequis

- Python 3.9 ou plus récent (développé et testé avec Python 3.12).
- tkinter, fourni avec Python sur Windows et macOS. Sous Linux, il peut falloir l'installer à part (ex. `sudo apt install python3-tk`).

## Utilisation

### Depuis la ligne de commande

```bash
# Ouvre un exemple intégré
python frame_objet_Adapter.py

# Ouvre un fichier JSON (qui doit contenir un objet au premier niveau)
python frame_objet_Adapter.py config.json
```

À la fermeture, l'objet validé est affiché au format JSON dans la console (ou `Annulé.`).

### Depuis un script Python

```python
from frame_objet_Adapter import edit_object

config = {
    "nom": "Projet",
    "base": {"hote": "localhost", "port": 5432},
    "dossier_sortie": "C:/Users/moi/Documents",
}

resultat = edit_object(config, title="Configuration")
if resultat is None:
    print("Modifications annulées")
else:
    config = resultat
```

`edit_object` ouvre la fenêtre, attend sa fermeture puis retourne une **copie modifiée** de l'objet, ou `None` si l'utilisateur annule.

## Saisie des valeurs

Le texte saisi est interprété comme un littéral Python (`ast.literal_eval`) :

| Saisie | Valeur obtenue |
|---|---|
| `42` | `int` |
| `3.5` | `float` |
| `True` / `False` | `bool` |
| `None` | `None` |
| `[1, 2]`, `{"a": 1}` | `list`, `dict` |
| `'texte'` | `str` |
| `texte` (sans guillemets) | `str` (repli si le texte n'est pas un littéral valide) |

Pour forcer une chaîne qui ressemble à un nombre, il faut l'entourer de guillemets : `'42'`.

Pour les **clés**, une clé `str` reste une `str` : le texte est pris tel quel, sans guillemets. Une clé d'un autre type (`int`, `tuple`…) est interprétée comme ci-dessus. Le renommage conserve l'ordre des clés.

## Raccourcis

**Dans l'arbre**

| Touche / action | Effet |
|---|---|
| Double-clic sur la colonne *Clé* | Renommer la clé |
| Double-clic sur la colonne *Valeur* | Modifier la valeur |
| `Entrée` | Modifier la valeur de la ligne sélectionnée |
| `F2` | Renommer la clé de la ligne sélectionnée |
| `Tab` / `Maj+Tab` | Ouvrir la première / dernière cellule éditable de la ligne |
| `Suppr` | Supprimer l'élément sélectionné |

**Pendant la saisie**

| Touche | Effet |
|---|---|
| `Entrée` | Valider |
| `Échap` | Annuler la saisie |
| `Tab` / `Maj+Tab` | Valider et passer à la cellule suivante / précédente |
| `↓` / `↑` | Valider et passer à la même colonne, ligne suivante / précédente |

**Partout**

| Raccourci | Effet |
|---|---|
| `Ctrl+Z` | Annuler la dernière modification |
| `Ctrl+O` | Ouvrir un fichier JSON |
| `Ctrl+S` | Enregistrer sous… |

Les valeurs `dict` et `list` ne sont pas éditables directement : on modifie leurs éléments, ou on utilise les boutons *Ajouter* / *Supprimer*.

## Détection des chemins

Une chaîne est considérée comme un chemin si :

- elle désigne un dossier ou un fichier **existant** et contient un séparateur (`/` ou `\`) ; ou
- elle **commence comme un chemin** : `C:\`, `C:/`, `\\serveur`, `/`, `~`, `./`, `../`.

Un chemin existant est classé d'après le disque. Pour un chemin qui n'existe pas encore, une extension indique un fichier et l'absence d'extension un dossier. La colonne *Type* affiche alors `str (dossier)` ou `str (fichier)`.

Le bouton situé à droite de la valeur ouvre `askdirectory` ou `askopenfilename`, positionné sur l'emplacement actuel. Le style de séparateur d'origine (`\` ou `/`) est conservé.

## Architecture

```
Node             description neutre d'une ligne de l'arbre (sans dépendance tkinter)
ObjectAdapter    contrat entre un type d'objet et l'éditeur
DictAdapter      implémentation pour dict (avec dict / list imbriqués)
AdapterRegistry  choisit l'adaptateur selon le type de l'objet
ObjectEditorApp  fenêtre tkinter, indépendante du type édité
```

L'interface ne manipule jamais l'objet directement : elle demande à l'adaptateur la liste des nœuds (`children`) et lui délègue toutes les modifications (`set_value`, `rename_key`, `add_item`, `delete_item`).

### Ajouter un nouveau type d'objet

Il suffit d'écrire un adaptateur et de l'enregistrer. Le dernier adaptateur enregistré qui accepte l'objet est utilisé.

```python
from frame_objet_Adapter import ObjectAdapter, Node, registry, edit_object


@registry.register
class MonAdapter(ObjectAdapter):
    file_types = [("Mon format", "*.ext"), ("Tous les fichiers", "*.*")]

    @classmethod
    def supports(cls, obj):
        return isinstance(obj, MonType)

    def children(self, obj, path):
        """Retourne la liste des Node enfants du nœud situé à `path`."""
        ...

    def set_value(self, obj, path, raw):
        """Modifie en place la valeur située à `path` à partir du texte saisi."""
        ...

    # Facultatif : can_add, requires_key, add_item, delete_item,
    #              rename_key, read, write


edit_object(mon_objet)
```

Les champs de `Node` pilotent l'affichage et les possibilités d'édition :

| Champ | Rôle |
|---|---|
| `path` | Chemin d'accès à l'élément, ex. `("base", "port")` |
| `label` | Texte de la colonne *Clé* |
| `display` | Texte de la colonne *Valeur* |
| `type_name` | Nom du type (sert aussi à choisir la couleur) |
| `edit_text` | Texte proposé à l'édition de la valeur |
| `is_container` | Le nœud a-t-il des enfants ? |
| `editable` | La valeur est-elle modifiable ? |
| `renamable` | La clé est-elle modifiable ? |
| `key_edit_text` | Texte proposé au renommage de la clé |
| `path_kind` | `"dir"`, `"file"` ou `None` : affiche le bouton de choix de chemin |

### Personnaliser les couleurs

Les couleurs sont des attributs de classe d'`ObjectEditorApp` : `TYPE_COLORS`, `CONTAINER_BACKGROUNDS`, `SELECTION_BACKGROUND`… On peut les modifier dans une sous-classe :

```python
from frame_objet_Adapter import ObjectEditorApp


class MonEditeur(ObjectEditorApp):
    TYPE_COLORS = {**ObjectEditorApp.TYPE_COLORS, "str": "#006400", "ndarray": "#8b008b"}


app = MonEditeur({"a": 1, "b": "texte"}, title="Éditeur personnalisé")
app.mainloop()
print(app.result)  # None si annulé
```

## Limites connues

- Les fichiers JSON doivent contenir un objet (`dict`) au premier niveau. Les clés non textuelles sont converties en chaînes par `json.dump`.
- Un `ttk.Treeview` colore des lignes entières, pas des cellules : la couleur d'un type s'applique à toute la ligne.
- La détection des chemins interroge le disque à chaque rafraîchissement, ce qui peut ralentir l'affichage avec des chemins réseau lents.
- Un chemin inexistant et sans extension est toujours traité comme un dossier.

## Licence

Distribué sous licence MIT. Voir le fichier [LICENSE](LICENSE).
