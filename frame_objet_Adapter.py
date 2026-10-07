"""Éditeur graphique d'objets Python basé sur tkinter.

Architecture
------------
- ``Node`` : description neutre d'une ligne de l'arbre (aucune dépendance GUI).
- ``ObjectAdapter`` : contrat à implémenter pour chaque type d'objet éditable.
- ``DictAdapter`` : implémentation pour ``dict`` (avec ``dict`` et ``list`` imbriqués).
- ``DatasetAdapter`` : implémentation pour ``xarray.Dataset`` (si xarray est installé).
- ``AdapterRegistry`` : choisit automatiquement l'adaptateur selon le type de l'objet.
- ``ObjectEditor`` : widget tkinter (``ttk.Frame``), indépendant du type édité,
  intégrable dans n'importe quelle application.
- ``ObjectEditorApp`` / ``ObjectEditorDialog`` : fenêtres prêtes à l'emploi
  (application autonome / boîte de dialogue modale) autour d'``ObjectEditor``.

Pour supporter un nouveau type, il suffit d'écrire un adaptateur et de le
déclarer avec ``@registry.register``. La GUI ne change pas.

Usage
-----
    python frame_objet_Adapter.py [fichier.json | fichier.nc | --xarray]

    from frame_objet_Adapter import edit_object
    nouveau = edit_object({"a": 1})                # application autonome
    nouveau = edit_object({"a": 1}, parent=root)   # dialogue modal dans une appli tkinter
    # None si l'utilisateur annule

    from frame_objet_Adapter import ObjectEditor
    editeur = ObjectEditor(cadre, {"a": 1})        # widget à placer avec pack/grid
    editeur.bind("<<ObjectChanged>>", lambda e: print(editeur.obj))

Voir ``exemple_integration.py`` pour une application complète.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import fnmatch
import json
import os
import re
import sys
import tkinter as tk
import tkinter.font as tkfont
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable

try:  # dépendances facultatives : seulement pour éditer des objets xarray
    import numpy as np
    import pandas as pd
    import xarray as xr
except ImportError:
    np = pd = xr = None

Path = tuple[Any, ...]  # chemin d'accès à un élément, ex : ("db", "ports", 0)


# ---------------------------------------------------------------------------
# Modèle neutre
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Node:
    """Une ligne de l'arbre, indépendante de tkinter."""

    path: Path
    label: str
    display: str  # texte affiché dans la colonne « Valeur »
    type_name: str
    edit_text: str  # texte proposé lors de l'édition
    is_container: bool
    editable: bool
    renamable: bool = False  # la clé peut-elle être modifiée ?
    key_edit_text: str = ""  # texte proposé lors du renommage de la clé
    path_kind: str | None = None  # "dir" / "file" si la valeur ressemble à un chemin
    type_label: str = ""  # texte de la colonne « Type » (par défaut : type_name)


@dataclass(frozen=True)
class NewRoot:
    """Résultat d'une opération qui remplace l'objet racine au lieu de le
    modifier en place (ex : renommer une variable d'un ``xarray.Dataset``)."""

    obj: Any
    path: Path | None = None  # chemin à sélectionner dans le nouvel objet


# Début typique d'un chemin : « C:\ », « C:/ », « \\serveur », « / », « ~/ », « ./ », « ../ ».
_PATH_START = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/|~[\\/]?|\.{1,2}[\\/])")


def guess_path_kind(value: Any) -> str | None:
    """Retourne ``"dir"``, ``"file"`` ou None selon que ``value`` ressemble à un chemin.

    Un chemin existant est classé d'après le disque. Sinon, on se fie à sa forme :
    avec une extension c'est un fichier, sans extension un dossier. Une chaîne
    relative sans séparateur (« python ») n'est jamais considérée comme un chemin.
    """
    if not isinstance(value, str) or not value.strip() or "\n" in value:
        return None
    has_separator = "/" in value or "\\" in value
    if not (_PATH_START.match(value) or has_separator):
        return None
    expanded = os.path.expanduser(value)
    if os.path.isdir(expanded):
        return "dir"
    if os.path.isfile(expanded):
        return "file"
    if not _PATH_START.match(value):  # « km/h », « a/b »… : pas assez fiable
        return None
    return "file" if os.path.splitext(value)[1] else "dir"


def parse_literal(text: str) -> Any:
    """Convertit un texte saisi en valeur Python.

    ``42`` -> int, ``3.5`` -> float, ``True`` -> bool, ``None`` -> None,
    ``[1, 2]`` -> list, ``'abc'`` -> str. Un texte non interprétable reste une
    chaîne. Pour forcer une chaîne numérique, saisir ``'42'`` avec les guillemets.
    """
    try:
        return ast.literal_eval(text.strip())
    except (ValueError, SyntaxError):
        return text


# ---------------------------------------------------------------------------
# Contrat des adaptateurs
# ---------------------------------------------------------------------------
class ObjectAdapter(ABC):
    """Interface entre un type d'objet et l'éditeur graphique."""

    file_types: list[tuple[str, str]] = [("Tous les fichiers", "*.*")]
    # Profondeur des nœuds ouverts à l'affichage initial (None : tout ouvrir).
    expand_depth: int | None = None

    @classmethod
    @abstractmethod
    def supports(cls, obj: Any) -> bool:
        """Retourne True si cet adaptateur sait gérer ``obj``."""

    @abstractmethod
    def children(self, obj: Any, path: Path) -> list[Node]:
        """Liste les enfants directs du nœud situé à ``path``."""

    @abstractmethod
    def set_value(self, obj: Any, path: Path, raw: str) -> NewRoot | None:
        """Modifie en place la valeur située à ``path``.

        Comme ``rename_key``, ``add_item`` et ``delete_item``, peut retourner un
        ``NewRoot`` si l'objet ne peut pas être modifié en place.
        """

    def can_add(self, obj: Any, path: Path) -> bool:
        return False

    def requires_key(self, obj: Any, path: Path) -> bool:
        return False

    def add_hint(self, obj: Any, path: Path) -> str:
        """Invite affichée lors de la saisie d'une nouvelle valeur dans ``path``."""
        return "Valeur (ex : 42, 'texte', [1, 2]) :"

    def rename_key(self, obj: Any, path: Path, raw: str) -> Path | NewRoot:
        """Renomme la clé située à ``path`` et retourne le nouveau chemin."""
        raise NotImplementedError("Renommage non supporté pour ce type.")

    def add_item(self, obj: Any, path: Path, key: str | None, raw: str) -> Path | NewRoot:
        """Ajoute un élément dans le conteneur ``path`` et retourne son chemin."""
        raise NotImplementedError("Ajout non supporté pour ce type.")

    def delete_item(self, obj: Any, path: Path) -> NewRoot | None:
        raise NotImplementedError("Suppression non supportée pour ce type.")

    def read(self, filename: str) -> Any:
        raise NotImplementedError("Lecture de fichier non supportée.")

    def write(self, obj: Any, filename: str) -> None:
        raise NotImplementedError("Écriture de fichier non supportée.")


class AdapterRegistry:
    """Associe un objet à l'adaptateur adapté (le dernier enregistré gagne)."""

    def __init__(self) -> None:
        self._adapters: list[type[ObjectAdapter]] = []

    def register(self, adapter_cls: type[ObjectAdapter]) -> type[ObjectAdapter]:
        self._adapters.append(adapter_cls)
        return adapter_cls

    def resolve(self, obj: Any) -> ObjectAdapter:
        for adapter_cls in reversed(self._adapters):
            if adapter_cls.supports(obj):
                return adapter_cls()
        raise TypeError(f"Aucun adaptateur pour le type {type(obj).__name__}.")

    def for_file(self, filename: str) -> ObjectAdapter | None:
        """Adaptateur dont un motif de ``file_types`` correspond à ``filename``."""
        name = os.path.basename(filename).lower()
        for adapter_cls in reversed(self._adapters):
            for _label, patterns in adapter_cls.file_types:
                if any(p != "*.*" and fnmatch.fnmatch(name, p) for p in patterns.split()):
                    return adapter_cls()
        return None

    def file_types(self, first: ObjectAdapter | None = None) -> list[tuple[str, str]]:
        """Types de fichiers de tous les adaptateurs, ceux de ``first`` en tête."""
        ordered = [type(first)] if first is not None else []
        ordered += [cls for cls in reversed(self._adapters) if cls not in ordered]
        types = [ft for cls in ordered for ft in cls.file_types if ft[1] != "*.*"]
        return list(dict.fromkeys(types)) + [("Tous les fichiers", "*.*")]


registry = AdapterRegistry()


# ---------------------------------------------------------------------------
# Adaptateur dict
# ---------------------------------------------------------------------------
@registry.register
class DictAdapter(ObjectAdapter):
    """Édition d'un ``dict`` (imbrications de ``dict`` et ``list`` incluses)."""

    file_types = [("JSON", "*.json"), ("Tous les fichiers", "*.*")]

    @classmethod
    def supports(cls, obj: Any) -> bool:
        return isinstance(obj, dict)

    # -- lecture ----------------------------------------------------------
    @staticmethod
    def _resolve(obj: Any, path: Path) -> Any:
        current = obj
        for key in path:
            current = current[key]
        return current

    @staticmethod
    def _items(container: Any) -> list[tuple[Any, Any]]:
        if isinstance(container, dict):
            return list(container.items())
        return list(enumerate(container))

    def children(self, obj: Any, path: Path) -> list[Node]:
        container = self._resolve(obj, path)
        in_dict = isinstance(container, dict)
        nodes = []
        for key, value in self._items(container):
            is_container = isinstance(value, (dict, list))
            display = f"({len(value)} élément(s))" if is_container else repr(value)
            nodes.append(
                Node(
                    path=path + (key,),
                    label=str(key),
                    display=display,
                    type_name=type(value).__name__,
                    edit_text=repr(value),
                    is_container=is_container,
                    editable=not is_container,
                    renamable=in_dict,
                    key_edit_text=key if isinstance(key, str) else repr(key),
                    path_kind=guess_path_kind(value),
                )
            )
        return nodes

    # -- écriture ---------------------------------------------------------
    def set_value(self, obj: Any, path: Path, raw: str) -> None:
        if not path:
            raise ValueError("Le nœud racine ne peut pas être remplacé.")
        self._resolve(obj, path[:-1])[path[-1]] = parse_literal(raw)

    def can_add(self, obj: Any, path: Path) -> bool:
        return isinstance(self._resolve(obj, path), (dict, list))

    def requires_key(self, obj: Any, path: Path) -> bool:
        return isinstance(self._resolve(obj, path), dict)

    def rename_key(self, obj: Any, path: Path, raw: str) -> Path:
        """Renomme une clé de dict en conservant l'ordre des éléments.

        Une clé ``str`` reste une ``str`` (le texte saisi est pris tel quel) ;
        une clé d'un autre type (int, tuple…) est interprétée par ``parse_literal``.
        """
        if not path:
            raise ValueError("Le nœud racine ne peut pas être renommé.")
        parent = self._resolve(obj, path[:-1])
        if not isinstance(parent, dict):
            raise TypeError("Seules les clés d'un dict peuvent être renommées.")
        old_key = path[-1]
        new_key = raw if isinstance(old_key, str) else parse_literal(raw)
        if new_key == "":
            raise ValueError("La clé ne peut pas être vide.")
        try:
            hash(new_key)
        except TypeError:
            raise TypeError(f"{new_key!r} ne peut pas servir de clé (non hachable).") from None
        if new_key == old_key:
            return path
        if new_key in parent:
            raise ValueError(f"La clé {new_key!r} existe déjà.")
        items = list(parent.items())
        parent.clear()
        for key, value in items:
            parent[new_key if key == old_key else key] = value
        return path[:-1] + (new_key,)

    def add_item(self, obj: Any, path: Path, key: str | None, raw: str) -> Path:
        container = self._resolve(obj, path)
        value = parse_literal(raw)
        if isinstance(container, dict):
            if not key:
                raise ValueError("Une clé est requise.")
            parsed_key = parse_literal(key)
            if parsed_key in container:
                raise KeyError(f"La clé {parsed_key!r} existe déjà.")
            container[parsed_key] = value
            return path + (parsed_key,)
        container.append(value)
        return path + (len(container) - 1,)

    def delete_item(self, obj: Any, path: Path) -> None:
        if not path:
            raise ValueError("Le nœud racine ne peut pas être supprimé.")
        del self._resolve(obj, path[:-1])[path[-1]]

    # -- fichiers ---------------------------------------------------------
    def read(self, filename: str) -> dict:
        with open(filename, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("Le fichier JSON doit contenir un objet (dict).")
        return data

    def write(self, obj: dict, filename: str) -> None:
        with open(filename, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Adaptateur xarray.Dataset
# ---------------------------------------------------------------------------
def _require_xarray() -> None:
    if xr is None:
        raise ImportError("Le module xarray (avec numpy et pandas) n'est pas installé.")


def _scalar_texts(value: Any) -> tuple[str, str, str]:
    """(texte affiché, texte d'édition, nom de type) d'un élément de tableau numpy."""
    if isinstance(value, np.datetime64):
        text = str(pd.Timestamp(value))
        return text, repr(text), "datetime64"
    if isinstance(value, np.timedelta64):
        text = str(pd.Timedelta(value))
        return text, repr(text), "timedelta64"
    if isinstance(value, np.generic):
        value = value.item()
    return repr(value), repr(value), type(value).__name__


def _preview(values: Any, limit: int = 6) -> str:
    """Premières valeurs d'un tableau, aplati : « 1.0, 2.0, 3.0, … »."""
    flat = np.ravel(values)
    texts = [_scalar_texts(v)[0] for v in flat[:limit]]
    return ", ".join(texts) + (", …" if flat.size > limit else "")


def _coerce(value: Any, dtype: np.dtype) -> Any:
    """Convertit une valeur saisie pour l'écrire dans un tableau de type ``dtype``."""
    kind = dtype.kind
    if kind == "M":  # dates : '2024-01-31', '2024-01-31 12:00'…
        return np.datetime64("NaT") if value is None else pd.Timestamp(value).to_datetime64()
    if kind == "m":  # durées : '1 days', '02:30:00'…
        return np.timedelta64("NaT") if value is None else pd.Timedelta(value).to_timedelta64()
    if kind == "b":
        if not isinstance(value, bool):
            raise TypeError("Valeur booléenne attendue (True ou False).")
        return value
    if kind in "iufc":
        if value is None and kind in "fc":
            return float("nan")
        if isinstance(value, str):  # 'nan', 'inf'… ne sont pas des littéraux Python
            try:
                return {"i": int, "u": int, "f": float, "c": complex}[kind](value)
            except ValueError:
                pass
        if not isinstance(value, (int, float, complex)):
            raise TypeError(f"Valeur numérique attendue (type {dtype}).")
        return value
    if kind == "U":
        return value if isinstance(value, str) else str(value)
    if kind == "S":
        return value if isinstance(value, bytes) else str(value).encode()
    return value  # object : valeur prise telle quelle


def _widen(arr: np.ndarray, value: Any) -> np.ndarray:
    """Élargit le type de ``arr`` si ``value`` n'y tient pas (chaîne plus longue,
    réel dans un tableau d'entiers…), pour éviter une troncature silencieuse."""
    kind = arr.dtype.kind
    if kind in "US":
        needed = np.dtype(f"{kind}{max(len(value), 1)}")
    elif kind in "iufc":
        needed = np.result_type(arr.dtype, value)
    else:
        return arr
    new_dtype = np.promote_types(arr.dtype, needed)
    return arr if new_dtype == arr.dtype else arr.astype(new_dtype)


@registry.register
class DatasetAdapter(ObjectAdapter):
    """Édition d'un ``xarray.Dataset``.

    Arborescence et chemins :

    - ``("dims",)`` : tailles des dimensions (lecture seule) ;
    - ``("coords", nom)`` / ``("data_vars", nom)`` : une variable. Ses enfants :
      ``(…, "attrs", clé)`` pour ses attributs, puis ses valeurs :
      1 dimension : ``(…, i)`` un élément par ligne ;
      2 dimensions : ``(…, i)`` une ligne par indice de la 1re dimension, et
      ``(…, i, j)`` une cellule par indice de la 2de ;
      0 dimension : la valeur s'édite directement sur la ligne de la variable ;
      3 dimensions ou plus : pas de valeurs détaillées, seulement le résumé
      « (dimensions) | valeurs… » ;
    - ``("attrs", clé)`` : attributs globaux.

    Les attributs sont des ``dict`` : leur édition est confiée à ``DictAdapter``.
    """

    file_types = [("NetCDF", "*.nc *.nc4 *.cdf"), ("Tous les fichiers", "*.*")]
    expand_depth = 2  # groupes ouverts, variables fermées
    GROUPS = {"coords": "Coordonnées", "data_vars": "Variables"}
    ATTRS = "attrs"
    MORE = "…"  # dernier élément du chemin d'une ligne « n éléments non affichés »
    # Nombre maximal de lignes / de cellules par ligne affichées pour un tableau.
    MAX_ROWS = 50
    MAX_COLUMNS = 20

    @classmethod
    def supports(cls, obj: Any) -> bool:
        return xr is not None and isinstance(obj, xr.Dataset)

    # -- outils -----------------------------------------------------------
    def _split_attrs(self, ds: Any, path: Path) -> tuple[dict | None, Path]:
        """Si ``path`` est dans des attributs : (dict des attributs, chemin du groupe)."""
        if path[:1] == (self.ATTRS,):
            return ds.attrs, path[:1]
        if len(path) >= 3 and path[0] in self.GROUPS and path[2] == self.ATTRS:
            return ds.variables[path[1]].attrs, path[:3]
        return None, ()

    def _variable_path(self, path: Path) -> bool:
        return len(path) == 2 and path[0] in self.GROUPS

    @staticmethod
    def _group_of(ds: Any, name: Any) -> str:
        return "coords" if name in ds.coords else "data_vars"

    # -- lecture ----------------------------------------------------------
    def children(self, ds: Any, path: Path) -> list[Node]:
        attrs, prefix = self._split_attrs(ds, path)
        if attrs is not None:
            return self._attr_nodes(attrs, prefix, path[len(prefix) :])
        if not path:
            sizes = ", ".join(f"{dim}: {size}" for dim, size in ds.sizes.items())
            return [
                Node(("dims",), "Dimensions", sizes, "dims", "", False, False),
                *(self._group_node(ds, group) for group in self.GROUPS),
                self._attrs_node(ds.attrs, (self.ATTRS,), "Attributs"),
            ]
        if len(path) == 1:
            names = ds.coords if path[0] == "coords" else ds.data_vars
            return [self._variable_node(ds, path + (name,)) for name in names]
        var = ds.variables[path[1]]
        index = path[2:]
        nodes = [] if index else [self._attrs_node(var.attrs, path + (self.ATTRS,), "attrs")]
        if 1 <= var.ndim <= 2:
            nodes += self._element_nodes(ds, path, var)
        return nodes

    def _group_node(self, ds: Any, group: str) -> Node:
        count = len(ds.coords if group == "coords" else ds.data_vars)
        return Node(
            (group,), self.GROUPS[group], f"({count} variable(s))", "Dataset", "",
            is_container=True, editable=False, type_label=group,
        )

    @staticmethod
    def _attrs_node(attrs: dict, path: Path, label: str) -> Node:
        return Node(path, label, f"({len(attrs)} élément(s))", "dict", "", True, False)

    def _attr_nodes(self, attrs: dict, prefix: Path, rest: Path) -> list[Node]:
        nodes = []
        for node in DictAdapter().children(attrs, rest):
            value = DictAdapter._resolve(attrs, node.path)
            changes: dict[str, Any] = {"path": prefix + node.path}
            if isinstance(value, (np.ndarray, np.generic)):  # éditable comme liste / scalaire
                text = repr(value.tolist())
                changes.update(display=text, edit_text=text)
            nodes.append(dataclasses.replace(node, **changes))
        return nodes

    def _variable_node(self, ds: Any, path: Path) -> Node:
        name = path[1]
        var = ds.variables[name]
        if var.ndim == 0:
            display, edit_text, _ = _scalar_texts(var.values[()])
        else:
            sizes = ", ".join(f"{dim}: {size}" for dim, size in var.sizes.items())
            display, edit_text = f"({sizes}) | {_preview(var.values)}", ""
        return Node(
            path=path,
            label=str(name),
            display=display,
            type_name="Variable",
            edit_text=edit_text,
            is_container=True,  # contient au moins ses attributs
            editable=var.ndim == 0,
            renamable=True,
            key_edit_text=str(name),
            type_label=str(var.dtype),
        )

    def _element_nodes(self, ds: Any, path: Path, var: Any) -> list[Node]:
        """Éléments (1-D), lignes (2-D) ou cellules d'une ligne (2-D) d'une variable."""
        name, index = path[1], path[2:]
        dim = var.dims[len(index)]
        values = var.values[index]
        # Valeurs de la coordonnée de dimension, pour repérer chaque indice.
        coord = ds.variables[dim].values if dim in ds.coords and dim != name else None
        limit = self.MAX_ROWS if not index else self.MAX_COLUMNS
        is_row = var.ndim == 2 and not index
        nodes = []
        for i in range(min(len(values), limit)):
            label = f"[{i}]"
            if coord is not None:
                label += f"  {dim} = {_scalar_texts(coord[i])[0]}"
            if is_row:
                nodes.append(
                    Node(
                        path + (i,), label, _preview(values[i]), "list", "",
                        is_container=True, editable=False,
                        type_label=f"ligne ({var.dims[1]}: {len(values[i])})",
                    )
                )
                continue
            display, edit_text, type_name = _scalar_texts(values[i])
            nodes.append(
                Node(
                    path + (i,), label, display, type_name, edit_text,
                    is_container=False, editable=True,
                    path_kind=guess_path_kind(values[i]),
                )
            )
        if len(values) > limit:
            hidden = f"{len(values) - limit} élément(s) non affiché(s)"
            nodes.append(Node(path + (self.MORE,), self.MORE, hidden, "", "", False, False))
        return nodes

    # -- écriture ---------------------------------------------------------
    def set_value(self, ds: Any, path: Path, raw: str) -> NewRoot | None:
        attrs, prefix = self._split_attrs(ds, path)
        if attrs is not None:
            return DictAdapter().set_value(attrs, path[len(prefix) :], raw)
        if len(path) < 2 or path[0] not in self.GROUPS:
            raise ValueError("Ce nœud n'est pas modifiable.")
        name, index = path[1], path[2:]
        var = ds.variables[name]
        if len(index) != var.ndim:
            raise ValueError("Seules les valeurs individuelles sont modifiables.")
        arr = np.array(var.values)  # copie : les données d'origine peuvent être en lecture seule
        value = _coerce(parse_literal(raw), arr.dtype)
        arr = _widen(arr, value)
        arr[index] = value
        if name in ds.xindexes:
            # Coordonnée indexée : la réaffecter reconstruit aussi l'index pandas,
            # puis on rétablit l'ordre d'origine des variables.
            updated = ds.assign_coords({name: var.copy(deep=False, data=arr)})
            return NewRoot(updated[list(ds.variables)], path)
        var.data = arr  # en place : conserve l'ordre des variables
        return None

    def can_add(self, ds: Any, path: Path) -> bool:
        attrs, prefix = self._split_attrs(ds, path)
        if attrs is not None:
            return DictAdapter().can_add(attrs, path[len(prefix) :])
        return len(path) == 1 and path[0] in self.GROUPS

    def requires_key(self, ds: Any, path: Path) -> bool:
        attrs, prefix = self._split_attrs(ds, path)
        if attrs is not None:
            return DictAdapter().requires_key(attrs, path[len(prefix) :])
        return True

    def add_hint(self, ds: Any, path: Path) -> str:
        if len(path) == 1 and path[0] in self.GROUPS:
            return "Valeur : scalaire (ex : 3.5) ou (dimensions, données),\nex : ('time', [1, 2, 3]) ou (('y', 'x'), [[1, 2], [3, 4]])"
        return super().add_hint(ds, path)

    def rename_key(self, ds: Any, path: Path, raw: str) -> Path | NewRoot:
        attrs, prefix = self._split_attrs(ds, path)
        if attrs is not None and path != prefix:
            return prefix + DictAdapter().rename_key(attrs, path[len(prefix) :], raw)
        if not self._variable_path(path):
            raise ValueError("Ce nœud ne peut pas être renommé.")
        old = path[1]
        if not raw:
            raise ValueError("Le nom ne peut pas être vide.")
        if raw == old:
            return path
        if raw in ds.variables:
            raise ValueError(f"La variable {raw!r} existe déjà.")
        # Renomme aussi la dimension s'il s'agit d'une coordonnée de dimension.
        renamed = ds.rename({old: raw})
        return NewRoot(renamed, (self._group_of(renamed, raw), raw))

    def add_item(self, ds: Any, path: Path, key: str | None, raw: str) -> Path:
        attrs, prefix = self._split_attrs(ds, path)
        if attrs is not None:
            return prefix + DictAdapter().add_item(attrs, path[len(prefix) :], key, raw)
        if not (len(path) == 1 and path[0] in self.GROUPS):
            raise ValueError("Sélectionnez le groupe Coordonnées ou Variables.")
        if not key:
            raise ValueError("Un nom de variable est requis.")
        if key in ds.variables:
            raise KeyError(f"La variable {key!r} existe déjà.")
        target = ds.coords if path[0] == "coords" else ds
        target[key] = parse_literal(raw)
        return (self._group_of(ds, key), key)

    def delete_item(self, ds: Any, path: Path) -> None:
        attrs, prefix = self._split_attrs(ds, path)
        if attrs is not None and path != prefix:
            return DictAdapter().delete_item(attrs, path[len(prefix) :])
        if not self._variable_path(path):
            raise ValueError("Seuls les variables et les attributs peuvent être supprimés.")
        del ds[path[1]]
        return None

    # -- fichiers ---------------------------------------------------------
    def read(self, filename: str) -> Any:
        _require_xarray()
        with xr.open_dataset(filename) as ds:
            return ds.load()  # charge tout en mémoire avant de fermer le fichier

    def write(self, ds: Any, filename: str) -> None:
        ds.to_netcdf(filename)


# ---------------------------------------------------------------------------
# Interface graphique
# ---------------------------------------------------------------------------
KEY_COLUMN = "#0"
VALUE_COLUMN = "#1"


@dataclass
class _InlineEditor:
    """Champ de saisie superposé à une cellule du Treeview."""

    entry: ttk.Entry
    iid: str
    column: str  # KEY_COLUMN ou VALUE_COLUMN
    node: Node


class ObjectEditor(ttk.Frame):
    """Widget d'édition générique, piloté par un ``ObjectAdapter``.

    C'est un simple ``ttk.Frame`` : il se place dans n'importe quelle fenêtre
    (``pack``/``grid``) sans toucher à la fenêtre hôte (titre, menu, raccourcis,
    styles ttk globaux). L'objet édité est une copie, lisible dans ``obj``.

    - ``on_validate(obj)`` / ``on_cancel()`` : si fournis, ajoute les boutons
      « Valider » / « Annuler » qui appellent ces fonctions.
    - L'événement virtuel ``<<ObjectChanged>>`` est émis après chaque
      modification faite par l'utilisateur (édition, ajout, annulation…).
    - ``bind_shortcuts(fenêtre)`` active Ctrl+O, Ctrl+S et Ctrl+Z sur la fenêtre
      choisie (seul Ctrl+Z est actif par défaut, quand l'arbre a le focus).
    """

    UNDO_LIMIT = 50

    # Couleur du texte (et de la pastille) selon le type de la valeur.
    TYPE_COLORS: dict[str, str] = {
        "dict": "#1f5fa8",
        "list": "#2e7d32",
        "tuple": "#00796b",
        "str": "#b03a2e",
        "int": "#b35c00",
        "float": "#8e6a00",
        "bool": "#6a3fa0",
        "NoneType": "#808080",
        "datetime64": "#ad1457",
        "timedelta64": "#ad1457",
        "Dataset": "#4a148c",
        "Variable": "#37474f",
    }
    DEFAULT_TYPE_COLOR = "#333333"
    # Fond des lignes conteneurs (affichées en gras).
    CONTAINER_BACKGROUNDS: dict[str, str] = {
        "dict": "#e6effa",
        "list": "#e8f5e9",
        "tuple": "#e0f2f1",
        "Dataset": "#ede7f6",
        "Variable": "#eceff1",
    }
    DEFAULT_CONTAINER_BACKGROUND = "#f0f0f0"
    SELECTION_BACKGROUND = "#3a6ea5"
    ICON_SIZE = 12
    PATH_KIND_LABELS = {"dir": "dossier", "file": "fichier"}

    # Styles ttk propres à l'éditeur : ceux de l'application hôte restent intacts.
    TREE_STYLE = "ObjectEditor.Treeview"
    PATH_BUTTON_STYLE = "ObjectEditor.Path.TButton"

    def __init__(
        self,
        master: tk.Misc | None,
        obj: Any,
        adapter_registry: AdapterRegistry = registry,
        on_validate: Callable[[Any], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
        **frame_options: Any,
    ) -> None:
        super().__init__(master, **frame_options)
        self._registry = adapter_registry
        self._on_validate_cb = on_validate
        self._on_cancel_cb = on_cancel

        self._undo: deque[Any] = deque(maxlen=self.UNDO_LIMIT)
        self._nodes: dict[str, Node] = {}
        self._icons: dict[str, tk.PhotoImage] = {}  # références à conserver (sinon GC)
        self._styled_tags: set[str] = set()
        self._editor: _InlineEditor | None = None
        self._path_buttons: dict[str, ttk.Button] = {}  # iid -> bouton « parcourir »
        self._pending: set[str] = set()  # appels after_idle à annuler si détruit

        self._build_tree()
        self._build_buttons()
        self.tree.bind("<Control-z>", lambda _e: self.undo())
        self.set_object(obj)

    # -- API publique -----------------------------------------------------
    def set_object(self, obj: Any) -> None:
        """Remplace l'objet édité (par une copie) et vide l'historique."""
        self._finish_edit(commit=False)
        self.obj = copy.deepcopy(obj)  # on ne modifie jamais l'original
        self.adapter = self._registry.resolve(self.obj)
        self._undo.clear()
        self.refresh(expand_all=True)

    def bind_shortcuts(self, widget: tk.Misc) -> None:
        """Associe Ctrl+O / Ctrl+S / Ctrl+Z de ``widget`` (en général la fenêtre) à l'éditeur."""
        widget.bind("<Control-o>", lambda _e: self.open_file())
        widget.bind("<Control-s>", lambda _e: self.save_file())
        widget.bind("<Control-z>", lambda _e: self.undo())
        self.tree.unbind("<Control-z>")  # sinon Ctrl+Z annulerait deux actions

    def destroy(self) -> None:
        # Un appel différé exécuté après destruction déclencherait une erreur Tcl.
        for after_id in self._pending:
            self.after_cancel(after_id)
        self._pending.clear()
        super().destroy()

    def _defer(self, func: Callable[[], None]) -> None:
        """``after_idle`` annulé automatiquement si le widget est détruit avant."""

        def run() -> None:
            self._pending.discard(after_id)
            func()

        after_id = self.after_idle(run)
        self._pending.add(after_id)

    def _changed(self) -> None:
        self.event_generate("<<ObjectChanged>>")

    # -- construction -----------------------------------------------------
    def _build_style(self) -> None:
        style = ttk.Style(self)
        default_font = tkfont.nametofont("TkDefaultFont")
        self._bold_font = default_font.copy()
        self._bold_font.configure(weight="bold")
        style.configure(self.TREE_STYLE, rowheight=int(default_font.metrics("linespace") * 1.6))
        style.configure(f"{self.TREE_STYLE}.Heading", font=self._bold_font)
        style.configure(self.PATH_BUTTON_STYLE, padding=0)
        # Garde la ligne sélectionnée lisible malgré les couleurs des tags.
        style.map(
            self.TREE_STYLE,
            background=[("selected", self.SELECTION_BACKGROUND)],
            foreground=[("selected", "white")],
        )

    def _build_tree(self) -> None:
        self._build_style()
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, pady=(0, 4))

        self.tree = ttk.Treeview(
            frame, columns=("value", "type"), selectmode="browse", style=self.TREE_STYLE
        )
        self.tree.heading("#0", text="Clé")
        self.tree.heading("value", text="Valeur")
        self.tree.heading("type", text="Type")
        self.tree.column("#0", width=220, stretch=False)
        self.tree.column("value", width=380)
        self.tree.column("type", width=90, stretch=False)

        self._scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=self._on_tree_scroll)
        self.tree.pack(side="left", fill="both", expand=True)
        self._scrollbar.pack(side="right", fill="y")

        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-1>", lambda _e: self._finish_edit(commit=True), add=True)
        self.tree.bind("<Configure>", lambda _e: self._place_overlays(), add=True)
        # Ouverture/fermeture d'un nœud, redimensionnement d'une colonne : l'arbre
        # n'est à jour qu'après le traitement de l'événement, d'où after_idle.
        for sequence in ("<<TreeviewOpen>>", "<<TreeviewClose>>", "<B1-Motion>", "<ButtonRelease-1>"):
            self.tree.bind(sequence, lambda _e: self._defer(self._place_overlays), add=True)
        self.tree.bind("<Return>", lambda _e: self._edit_selected(VALUE_COLUMN))
        self.tree.bind("<F2>", lambda _e: self._edit_selected(KEY_COLUMN))
        self.tree.bind("<Tab>", lambda _e: self._tab_from_tree(+1))
        self.tree.bind("<Shift-Tab>", lambda _e: self._tab_from_tree(-1))
        self.tree.bind("<Delete>", lambda _e: self._on_delete())

    def _build_buttons(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(4, 0))
        ttk.Button(bar, text="Ajouter", command=self._on_add).pack(side="left")
        ttk.Button(bar, text="Supprimer", command=self._on_delete).pack(side="left", padx=4)
        ttk.Button(bar, text="Annuler l'action", command=self.undo).pack(side="left")
        if self._on_validate_cb is not None:
            ttk.Button(bar, text="Valider", command=self.validate).pack(side="right")
        if self._on_cancel_cb is not None:
            ttk.Button(bar, text="Annuler", command=self.cancel).pack(side="right", padx=4)

    # -- affichage --------------------------------------------------------
    def refresh(
        self,
        expand_all: bool = False,
        select: Path | None = None,
        moved: tuple[Path, Path] | None = None,
    ) -> None:
        """Reconstruit l'arbre en conservant les nœuds ouverts et la sélection.

        ``select`` : chemin à sélectionner (par défaut, la sélection courante).
        ``moved`` : (ancien, nouveau) chemin d'un nœud renommé, pour que lui et
        ses descendants restent ouverts.
        """
        self._finish_edit(commit=False)
        current = self._selected()
        if select is None and current is not None:
            select = current.path
        expanded = {self._nodes[i].path for i in self._walk("") if self.tree.item(i, "open")}
        if moved is not None:
            old, new = moved
            expanded = {new + p[len(old):] if p[: len(old)] == old else p for p in expanded}
        for button in self._path_buttons.values():
            button.destroy()
        self._path_buttons.clear()
        self.tree.delete(*self.tree.get_children())
        self._nodes.clear()
        self._populate("", (), expanded, expand_all)
        iid = self._iid_for(select) if select is not None else None
        if iid is not None:
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.see(iid)
        self._defer(self._place_overlays)

    def _populate(self, parent_iid: str, path: Path, expanded: set[Path], expand_all: bool) -> None:
        depth = self.adapter.expand_depth
        for node in self.adapter.children(self.obj, path):
            type_label = node.type_label or node.type_name
            if node.path_kind:
                type_label += f" ({self.PATH_KIND_LABELS[node.path_kind]})"
            iid = self.tree.insert(
                parent_iid,
                "end",
                text=" " + node.label,
                image=self._icon(node.type_name),
                values=(node.display, type_label),
                tags=self._tags(node),
                open=node.path in expanded
                or (expand_all and (depth is None or len(node.path) < depth)),
            )
            self._nodes[iid] = node
            if node.path_kind and node.editable:
                self._path_buttons[iid] = ttk.Button(
                    self.tree,
                    image=self._path_icon(node.path_kind),
                    style=self.PATH_BUTTON_STYLE,
                    takefocus=False,
                    command=lambda n=node: self._browse_path(n),
                )
            if node.is_container:
                self._populate(iid, node.path, expanded, expand_all)

    def _path_icon(self, kind: str) -> tk.PhotoImage:
        """Icône dessinée du bouton « parcourir » : dossier ou feuille."""
        name = f"path:{kind}"
        if name not in self._icons:
            icon = tk.PhotoImage(width=16, height=14)
            if kind == "dir":
                icon.put("#c98a00", to=(1, 1, 7, 4))  # onglet
                icon.put("#c98a00", to=(1, 3, 15, 13))  # contour
                icon.put("#f2c14e", to=(2, 4, 14, 12))  # corps
            else:
                icon.put("#7a7a7a", to=(3, 0, 13, 14))  # contour
                icon.put("#ffffff", to=(4, 1, 12, 13))  # page
                for y in (4, 7, 10):
                    icon.put("#9aa7b8", to=(6, y, 11, y + 1))  # lignes de texte
            self._icons[name] = icon
        return self._icons[name]

    def _icon(self, type_name: str) -> tk.PhotoImage:
        """Pastille carrée de la couleur du type (créée une seule fois)."""
        if type_name not in self._icons:
            color = self.TYPE_COLORS.get(type_name, self.DEFAULT_TYPE_COLOR)
            size = self.ICON_SIZE
            icon = tk.PhotoImage(width=size, height=size)
            icon.put(color, to=(2, 2, size - 1, size - 1))
            self._icons[type_name] = icon
        return self._icons[type_name]

    def _tags(self, node: Node) -> tuple[str, ...]:
        """Tags de style d'une ligne, configurés à la première utilisation.

        Le tag ``type:`` ne fixe que la couleur du texte et le tag ``container:``
        que le fond et la police : aucun conflit de priorité entre les deux.
        """
        tags = [f"type:{node.type_name}"]
        if node.is_container:
            tags.append(f"container:{node.type_name}")
        for tag in tags:
            if tag in self._styled_tags:
                continue
            kind, type_name = tag.split(":", 1)
            if kind == "type":
                color = self.TYPE_COLORS.get(type_name, self.DEFAULT_TYPE_COLOR)
                self.tree.tag_configure(tag, foreground=color)
            else:
                background = self.CONTAINER_BACKGROUNDS.get(
                    type_name, self.DEFAULT_CONTAINER_BACKGROUND
                )
                self.tree.tag_configure(tag, background=background, font=self._bold_font)
            self._styled_tags.add(tag)
        return tuple(tags)

    def _walk(self, parent_iid: str):
        for iid in self.tree.get_children(parent_iid):
            yield iid
            yield from self._walk(iid)

    def _selected(self) -> Node | None:
        selection = self.tree.selection()
        return self._nodes.get(selection[0]) if selection else None

    def _iid_for(self, path: Path) -> str | None:
        return next((iid for iid, node in self._nodes.items() if node.path == path), None)

    # -- édition en place -------------------------------------------------
    def _on_double_click(self, event: tk.Event) -> str | None:
        iid = self.tree.identify_row(event.y)
        if not iid or self.tree.identify_region(event.x, event.y) not in ("tree", "cell"):
            return None
        column = KEY_COLUMN if self.tree.identify_column(event.x) == KEY_COLUMN else VALUE_COLUMN
        # "break" empêche le double-clic d'ouvrir/fermer le nœud en plus.
        return "break" if self._start_edit(iid, column) else None

    def _edit_selected(self, column: str) -> str:
        selection = self.tree.selection()
        if selection:
            self._start_edit(selection[0], column)
        return "break"

    def _start_edit(self, iid: str, column: str, text: str | None = None) -> bool:
        """Ouvre un champ de saisie sur la cellule ; False si non éditable."""
        node = self._nodes.get(iid)
        if node is None:
            return False
        if self._editor is not None:
            # Valider la saisie en cours reconstruit l'arbre : les iid changent.
            self._finish_edit(commit=True)
            if self._editor is not None:  # saisie refusée, rouverte pour correction
                return False
            iid = self._iid_for(node.path)
            node = self._nodes.get(iid) if iid is not None else None
            if node is None:
                return False
        if column == KEY_COLUMN and node.renamable:
            initial = node.key_edit_text
        elif column == VALUE_COLUMN and node.editable:
            initial = node.edit_text
        else:
            return False

        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self.tree.see(iid)
        entry = ttk.Entry(self.tree)
        entry.insert(0, initial if text is None else text)
        entry.select_range(0, "end")
        entry.icursor("end")
        entry.bind("<Return>", lambda _e: self._close_edit(commit=True))
        entry.bind("<KP_Enter>", lambda _e: self._close_edit(commit=True))
        entry.bind("<Escape>", lambda _e: self._close_edit(commit=False))
        entry.bind("<Tab>", lambda _e: self._move_edit(+1))
        entry.bind("<Shift-Tab>", lambda _e: self._move_edit(-1))
        entry.bind("<Down>", lambda _e: self._move_edit(+1, same_column=True))
        entry.bind("<Up>", lambda _e: self._move_edit(-1, same_column=True))
        entry.bind("<FocusOut>", lambda _e: self._defer(self._on_editor_focus_out))
        self._editor = _InlineEditor(entry, iid, column, node)
        self.update_idletasks()  # bbox n'est fiable qu'une fois l'arbre affiché
        self._place_overlays()
        entry.focus_set()
        return True

    def _place_overlays(self) -> None:
        """(Re)positionne les widgets superposés à l'arbre : boutons « parcourir »
        et champ de saisie (après défilement, redimensionnement, ouverture…)."""
        try:
            if not self.tree.winfo_exists():
                return
        except tk.TclError:  # appel arrivé après la fermeture de l'application
            return
        for iid, button in self._path_buttons.items():
            bbox = self.tree.bbox(iid, VALUE_COLUMN)
            if not bbox:  # ligne masquée ou hors de la zone visible
                button.place_forget()
                continue
            x, y, width, height = bbox
            size = height - 2  # bouton carré
            button.place(x=x + width - size - 1, y=y + 1, width=size, height=size)

        editor = self._editor
        if editor is None:
            return
        bbox = self.tree.bbox(editor.iid, editor.column)
        if not bbox:  # cellule hors de la zone visible
            editor.entry.place_forget()
            return
        x, y, width, height = bbox
        if editor.column == KEY_COLUMN:
            # bbox tient déjà compte de la profondeur ; on saute l'indicateur et la pastille.
            offset = self._indent() + self.ICON_SIZE
            x, width = x + offset, max(width - offset, 40)
        elif editor.iid in self._path_buttons:
            width = max(width - (height - 2) - 2, 40)  # laisse le bouton accessible
        editor.entry.place(x=x, y=y, width=width, height=height)

    def _browse_path(self, node: Node) -> None:
        """Choisit un dossier ou un fichier pour la valeur ``node``.

        Si la cellule est en cours d'édition, le chemin choisi remplace le texte
        du champ (à valider ensuite) ; sinon la valeur est modifiée directement.
        """
        editor = self._editor
        editing = (
            editor is not None and editor.node.path == node.path and editor.column == VALUE_COLUMN
        )
        current = parse_literal(editor.entry.get() if editing else node.edit_text)
        current = current if isinstance(current, str) else ""
        initial = os.path.expanduser(current)
        initial_dir = initial if os.path.isdir(initial) else os.path.dirname(initial)
        if node.path_kind == "dir":
            chosen = filedialog.askdirectory(
                parent=self, initialdir=initial_dir or None, mustexist=False
            )
        else:
            chosen = filedialog.askopenfilename(
                parent=self,
                initialdir=initial_dir or None,
                initialfile=os.path.basename(initial) or None,
            )
        if not chosen:
            return
        if "\\" in current and "/" not in current:  # conserve le style de séparateur
            chosen = chosen.replace("/", "\\")
        text = repr(chosen)
        editor = self._editor  # le champ a pu être fermé pendant le dialogue
        if editing and editor is not None and editor.node.path == node.path:
            editor.entry.delete(0, "end")
            editor.entry.insert(0, text)
            editor.entry.focus_set()
        else:
            self._mutate(lambda: self.adapter.set_value(self.obj, node.path, text))

    def _indent(self) -> int:
        try:
            return int(ttk.Style(self).lookup(self.TREE_STYLE, "indent") or 20)
        except (ValueError, tk.TclError):
            return 20

    def _on_tree_scroll(self, first: str, last: str) -> None:
        self._scrollbar.set(first, last)
        self._place_overlays()

    def _on_editor_focus_out(self) -> None:
        editor = self._editor
        if editor is None:
            return
        try:
            focused = self.focus_get()
        except KeyError:  # widget interne de Tk (menu déroulant…)
            focused = None
        # Fenêtre inactive (Alt+Tab…) : on garde la saisie en cours.
        if focused is not None and focused is not editor.entry:
            self._finish_edit(commit=True)

    def _finish_edit(self, commit: bool) -> Path | None:
        """Ferme le champ de saisie, en appliquant ou non la valeur saisie.

        Retourne le chemin du nœud édité (nouveau chemin s'il a été renommé),
        ou None si aucun champ n'était ouvert ou si la saisie a été refusée.
        """
        editor, self._editor = self._editor, None
        if editor is None:
            return None
        raw = editor.entry.get()
        editor.entry.destroy()
        self.tree.focus_set()
        node = editor.node
        initial = node.key_edit_text if editor.column == KEY_COLUMN else node.edit_text
        if not commit or raw == initial:
            return node.path
        if editor.column == KEY_COLUMN:
            ok = self._mutate(
                lambda: self.adapter.rename_key(self.obj, node.path, raw), origin=node.path
            )
        else:
            ok = self._mutate(lambda: self.adapter.set_value(self.obj, node.path, raw))
        if ok:
            selected = self._selected()  # _mutate sélectionne le nœud (éventuellement renommé)
            return selected.path if selected is not None else node.path
        # Saisie refusée : on rouvre le champ pour correction.
        iid = self._iid_for(node.path)
        if iid is not None:
            self._start_edit(iid, editor.column, text=raw)
        return None

    def _close_edit(self, commit: bool) -> str:
        """Gestionnaire clavier (Entrée / Échap) : ferme le champ sans propager l'événement."""
        self._finish_edit(commit)
        return "break"

    # -- navigation clavier entre cellules -------------------------------
    def _walk_visible(self, parent_iid: str):
        """Lignes affichées, dans l'ordre (les enfants des nœuds fermés sont ignorés)."""
        for iid in self.tree.get_children(parent_iid):
            yield iid
            if self.tree.item(iid, "open"):
                yield from self._walk_visible(iid)

    def _editable_cells(self) -> list[tuple[str, str]]:
        """Cellules éditables visibles, dans l'ordre de lecture : (iid, colonne)."""
        cells = []
        for iid in self._walk_visible(""):
            node = self._nodes[iid]
            if node.renamable:
                cells.append((iid, KEY_COLUMN))
            if node.editable:
                cells.append((iid, VALUE_COLUMN))
        return cells

    def _move_edit(self, step: int, same_column: bool = False) -> str:
        """Valide la saisie puis ouvre la cellule voisine.

        ``step`` : +1 (suivante) ou -1 (précédente). Avec ``same_column``, on
        reste dans la même colonne (flèches haut/bas) et on s'arrête aux bords ;
        sinon on parcourt toutes les cellules (Tab) en revenant au début.
        """
        editor = self._editor
        if editor is None:
            return "break"
        cells = self._editable_cells()
        index = cells.index((editor.iid, editor.column))
        if same_column:
            candidates = [
                cell for cell in (cells[index + step :] if step > 0 else cells[:index][::-1])
                if cell[1] == editor.column
            ]
            target = candidates[0] if candidates else None
        else:
            target = cells[(index + step) % len(cells)]
        if target is None:
            return "break"
        # Les iid changent si la saisie modifie l'objet : on repère la cible par chemin.
        target_path, target_column = self._nodes[target[0]].path, target[1]
        old_path = editor.node.path
        new_path = self._finish_edit(commit=True)
        if new_path is None:  # saisie refusée, le champ est rouvert
            return "break"
        if target_path[: len(old_path)] == old_path:  # cible dans le nœud renommé
            target_path = new_path + target_path[len(old_path) :]
        iid = self._iid_for(target_path)
        if iid is not None:
            self._start_edit(iid, target_column)
        return "break"

    def _tab_from_tree(self, step: int) -> str:
        """Tab depuis l'arbre : ouvre la première (ou dernière) cellule de la ligne."""
        cells = self._editable_cells()
        if not cells:
            return "break"
        selection = self.tree.selection()
        row_cells = [cell for cell in cells if selection and cell[0] == selection[0]]
        if row_cells:
            target = row_cells[0] if step > 0 else row_cells[-1]
        else:
            target = cells[0] if step > 0 else cells[-1]
        self._start_edit(*target)
        return "break"

    # -- mutation avec historique ----------------------------------------
    def _mutate(self, action: Callable[[], Any], origin: Path | None = None) -> bool:
        """Applique ``action`` avec possibilité d'annulation ; False en cas d'erreur.

        Si ``action`` retourne un chemin, celui-ci est sélectionné après mise à
        jour ; avec ``origin``, il est considéré comme le nouveau chemin du nœud
        ``origin`` (renommage). Un ``NewRoot`` remplace l'objet édité.
        """
        snapshot = copy.deepcopy(self.obj)
        try:
            new_path = action()
            if isinstance(new_path, NewRoot):
                self.obj, new_path = new_path.obj, new_path.path
        except Exception as exc:  # noqa: BLE001 (erreur affichée à l'utilisateur)
            self.obj = snapshot
            self.refresh()
            messagebox.showerror("Erreur", str(exc), parent=self)
            return False
        self._undo.append(snapshot)
        if not isinstance(new_path, tuple):
            new_path = None
        moved = (origin, new_path) if origin is not None and new_path is not None else None
        self.refresh(select=new_path, moved=moved)
        self._changed()
        return True

    # -- actions ----------------------------------------------------------

    def _on_add(self) -> None:
        node = self._selected()
        target: Path = () if node is None else (node.path if node.is_container else node.path[:-1])
        if not self.adapter.can_add(self.obj, target):
            messagebox.showinfo("Ajout impossible", "Sélectionnez un conteneur.", parent=self)
            return
        key = None
        if self.adapter.requires_key(self.obj, target):
            key = simpledialog.askstring("Nouvelle clé", "Nom de la clé :", parent=self)
            if not key:
                return
        raw = simpledialog.askstring(
            "Nouvelle valeur", self.adapter.add_hint(self.obj, target), parent=self
        )
        if raw is not None:
            self._mutate(lambda: self.adapter.add_item(self.obj, target, key, raw))

    def _on_delete(self) -> None:
        node = self._selected()
        if node is not None:
            self._mutate(lambda: self.adapter.delete_item(self.obj, node.path))

    def undo(self) -> None:
        """Annule la dernière modification."""
        if self._undo:
            self.obj = self._undo.pop()
            self.adapter = self._registry.resolve(self.obj)  # l'objet a pu changer de type
            self.refresh()
            self._changed()

    def open_file(self) -> None:
        """Remplace l'objet édité par le contenu d'un fichier choisi par l'utilisateur."""
        filename = filedialog.askopenfilename(
            filetypes=self._registry.file_types(first=self.adapter), parent=self
        )
        if not filename:
            return
        adapter = self._registry.for_file(filename) or self.adapter
        try:
            new_obj = adapter.read(filename)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Erreur de lecture", str(exc), parent=self)
            return
        self._undo.append(copy.deepcopy(self.obj))
        self.obj = new_obj
        self.adapter = self._registry.resolve(self.obj)
        self.refresh(expand_all=True)
        self._changed()

    def save_file(self) -> None:
        """Enregistre l'objet édité dans un fichier choisi par l'utilisateur."""
        pattern = self.adapter.file_types[0][1].split()[0]  # ex : "*.json"
        filename = filedialog.asksaveasfilename(
            filetypes=self.adapter.file_types,
            defaultextension=pattern[1:] if pattern != "*.*" else "",
            parent=self,
        )
        if not filename:
            return
        try:
            self.adapter.write(self.obj, filename)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Erreur d'écriture", str(exc), parent=self)

    def commit_edit(self) -> bool:
        """Applique la saisie en cours, s'il y en a une ; False si elle est refusée."""
        if self._editor is None:
            return True
        return self._finish_edit(commit=True) is not None

    def validate(self) -> None:
        """Applique la saisie en cours puis transmet l'objet à ``on_validate``."""
        if self.commit_edit() and self._on_validate_cb is not None:
            self._on_validate_cb(self.obj)

    def cancel(self) -> None:
        self._finish_edit(commit=False)
        if self._on_cancel_cb is not None:
            self._on_cancel_cb()


# ---------------------------------------------------------------------------
# Fenêtres prêtes à l'emploi
# ---------------------------------------------------------------------------
class _EditorWindow:
    """Fenêtre (``tk.Tk`` ou ``tk.Toplevel``) contenant un ``ObjectEditor``,
    un menu « Fichier » et les boutons Valider / Annuler.

    Après fermeture, ``result`` contient l'objet modifié, ou None si annulation.
    """

    def _build_window(self, obj: Any, adapter_registry: AdapterRegistry, title: str) -> None:
        self.title(title)
        self.geometry("760x500")
        self.minsize(520, 320)
        self.result: Any | None = None
        self.editor = ObjectEditor(
            self, obj, adapter_registry, on_validate=self._on_validate, on_cancel=self._on_cancel
        )
        self.editor.pack(fill="both", expand=True, padx=8, pady=8)
        self.editor.bind_shortcuts(self)
        self._build_menu()
        self.protocol("WM_DELETE_WINDOW", self.editor.cancel)

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Ouvrir…", command=self.editor.open_file, accelerator="Ctrl+O")
        file_menu.add_command(
            label="Enregistrer sous…", command=self.editor.save_file, accelerator="Ctrl+S"
        )
        file_menu.add_separator()
        file_menu.add_command(label="Valider et fermer", command=self.editor.validate)
        file_menu.add_command(label="Annuler et fermer", command=self.editor.cancel)
        menubar.add_cascade(label="Fichier", menu=file_menu)
        self.config(menu=menubar)

    def _on_validate(self, obj: Any) -> None:
        self.result = obj
        self.destroy()

    def _on_cancel(self) -> None:
        self.result = None
        self.destroy()


class ObjectEditorApp(_EditorWindow, tk.Tk):
    """Application autonome (fenêtre racine). À lancer avec ``mainloop()``."""

    def __init__(
        self,
        obj: Any,
        adapter_registry: AdapterRegistry = registry,
        title: str = "Éditeur d'objet",
    ) -> None:
        tk.Tk.__init__(self)
        self._build_window(obj, adapter_registry, title)


class ObjectEditorDialog(_EditorWindow, tk.Toplevel):
    """Boîte de dialogue modale, à ouvrir depuis une application tkinter existante."""

    def __init__(
        self,
        parent: tk.Misc,
        obj: Any,
        adapter_registry: AdapterRegistry = registry,
        title: str = "Éditeur d'objet",
    ) -> None:
        tk.Toplevel.__init__(self, parent)
        self._build_window(obj, adapter_registry, title)
        self.transient(parent.winfo_toplevel())

    def show(self) -> Any | None:
        """Bloque jusqu'à la fermeture et retourne l'objet modifié (None si annulation)."""
        self.wait_visibility()
        self.grab_set()
        self.editor.tree.focus_set()
        self.wait_window()
        return self.result


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------
def edit_object(
    obj: Any, title: str = "Éditeur d'objet", parent: tk.Misc | None = None
) -> Any | None:
    """Ouvre l'éditeur et retourne l'objet modifié (None si annulation).

    Sans ``parent``, crée sa propre application tkinter. Depuis une application
    tkinter existante, passer un de ses widgets en ``parent`` : l'éditeur
    s'ouvre alors en boîte de dialogue modale.
    """
    if parent is not None:
        return ObjectEditorDialog(parent, obj, title=title).show()
    app = ObjectEditorApp(obj, title=title)
    app.mainloop()
    return app.result


def demo_dataset() -> Any:
    """Petit ``xarray.Dataset`` d'exemple : variables de 0 à 3 dimensions."""
    _require_xarray()
    time = pd.date_range("2024-01-01", periods=4, freq="D")
    stations = ["Tours", "Paris", "Lyon"]
    rng = np.random.default_rng(0)
    return xr.Dataset(
        data_vars={
            "temperature": (("time", "station"), rng.normal(12, 3, (4, 3)).round(1), {"units": "°C"}),
            "debit": ("time", [1.2, 3.4, 2.2, 0.9], {"units": "m3/s"}),
            "cube": (("time", "station", "niveau"), rng.random((4, 3, 2)).round(2)),
            "seuil": ((), 15.0),
        },
        coords={"time": time, "station": stations, "altitude": ("station", [60, 35, 170])},
        attrs={"titre": "Exemple", "source": "C:/Users"},
    )


def main(argv: list[str]) -> None:
    if len(argv) > 1 and argv[1] == "--xarray":
        initial = demo_dataset()
    elif len(argv) > 1:
        initial = (registry.for_file(argv[1]) or DictAdapter()).read(argv[1])
    else:
        initial = {
            "nom": "Projet",
            "version": 1.0,
            "actif": True,
            "tags": ["python", "tkinter"],
            "base": {"hote": "localhost", "port": 5432, "timeout": None},
            "paths": ["C:/Users", "D:/"],
        }
    result = edit_object(initial)
    if result is None:
        print("Annulé.")
    elif isinstance(result, dict):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result)


if __name__ == "__main__":
    main([sys.argv, "--xarray"])