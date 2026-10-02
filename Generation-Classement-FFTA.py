import json
import os
import re
import sys
import time
import unicodedata

import requests

FFTA_URL = "https://extranet.ffta.fr/iframe/classements.html"

def env_or_default(name, default):
    value = os.environ.get(name, "").strip()
    return value or default


FILTERS = {
    "Saison": env_or_default("FFTA_SAISON", "2026"),
    "Type": "Individuel",
    "Sexe": env_or_default("FFTA_SEXE", "Homme"),
    "Discipline": "Tir à 18m",
    "Catégorie d'âge": env_or_default("FFTA_CATEGORIE", "Senior 2"),
    "Arme": env_or_default("FFTA_ARME", "Arc Classique"),
}

STRUCTURES = {
    "regional": "CR06 - COMITE REGIONAL DU GRAND EST",
    "departemental": "57000 - COMITE DEPARTEMENTAL MOSELLE",
}


RAW_DIR = ".ffta_raw"
FINAL_DIR = "."



def slug(value):
    """Convertit une valeur d'affichage en nom de fichier stable et lisible."""
    value = unicodedata.normalize("NFD", str(value or ""))
    value = "".join(c for c in value if unicodedata.category(c) != "Mn")
    value = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-")
    return value


FILE_PREFIX = (
    "Classement_FFTA_"
    + slug(FILTERS["Saison"]) + "_"
    + slug(FILTERS["Sexe"]) + "_"
    + slug(FILTERS["Catégorie d'âge"]) + "_"
    + slug(FILTERS["Arme"])
)

def normalize(value):
    return " ".join(
        unicodedata.normalize("NFD", str(value or ""))
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
        .split()
    )



# ---------------------------------------------------------------------------
# Mode HTTP direct (sans navigateur)
# ---------------------------------------------------------------------------
# Le site FFTA fonctionne avec de simples formulaires POST + cookie de session :
#   1. classements.html          operation=search  -> liste des classements
#   2. classements/<id>.html     operation=enregistrer (niveau FED/LIG/DEP)
#   3. classements/<id>.html     operation=filtre      (structure) -> tableau
# Les codes (Catage=S1, Arme=CO, Struc=CR06...) sont lus dans les listes de la
# page plutôt que codés en dur.

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) Gecko/20100101 Firefox/156.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr,fr-FR;q=0.9,en-US;q=0.8,en;q=0.7",
}


def _norm(text):
    text = unicodedata.normalize("NFD", text or "")
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", text).strip().lower()


def _cell_text(node):
    for br in node.find_all("br"):
        br.replace_with(" ")
    return re.sub(r"\s+", " ", node.get_text("")).strip()


def _select_options(soup, name):
    """Options (texte, valeur) de la liste <select name=...>."""
    select = soup.find("select", attrs={"name": name})
    if select is None:
        return []
    return [
        (re.sub(r"\s+", " ", o.get_text("")).strip(), o.get("value", ""))
        for o in select.find_all("option")
    ]


def _option_value(options, wanted, label):
    """Valeur de l'option dont le texte correspond au libellé demandé."""
    target = _norm(wanted)
    for text, value in options:
        if _norm(text) == target:
            return value
    for text, value in options:
        if target and target in _norm(text):
            return value
    shown = " / ".join(t for t, _ in options[:15])
    raise RuntimeError(f"Option « {wanted} » introuvable pour {label}. Options vues : {shown}")


def _structure_value(options, wanted):
    """Code de structure (CR06, 57000...) à partir du libellé configuré."""
    target = _norm(wanted)
    stop = {"-", "du", "de", "des", "la", "le", "comite", "regional", "departemental"}
    tokens = [t for t in re.split(r"[\s-]+", target) if t and t not in stop]
    code = target.split(" - ")[0].strip() if " - " in target else ""

    def texts():
        return [(_norm(t), v) for t, v in options]

    for txt, value in texts():
        if txt == target:
            return value
    for txt, value in texts():
        if target and target in txt:
            return value
    if code:
        for txt, value in texts():
            if txt.startswith(code) or _norm(value) == code:
                return value
    if tokens:
        for txt, value in texts():
            if all(t in txt for t in tokens):
                return value
    # Dernier recours : le libellé configuré commence par un code.
    if code:
        return wanted.split(" - ")[0].strip()
    shown = " / ".join(t for t, _ in options[:15])
    raise RuntimeError(f"Structure « {wanted} » introuvable. Options vues : {shown}")


# Identifiants des pages de classement FFTA (extranet.ffta.fr/iframe/classements/<id>.html),
# lus dans un fichier texte à part : voir ffta_ids.txt pour le format et les consignes.
IDS_FILE = os.environ.get(
    "FFTA_IDS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffta_ids.txt"),
)

# Mot qui identifie chaque arme dans le texte d'une page de classement.
ARME_KEYWORDS = {"arc classique": "classique", "arc a poulies": "poulies", "arc nu": "arc nu"}


def _canonical_arme(text):
    """Nom d'arme normalisé : « Classique », « Arc à Poulie »... -> clé comparable."""
    value = _norm(text)
    if "classique" in value:
        return "arc classique"
    if "poulie" in value:
        return "arc a poulies"
    if re.fullmatch(r"(arc )?nu", value):
        return "arc nu"
    return value


def _ranking_key(saison, sexe, arme, categorie):
    return (_norm(saison), _norm(sexe), _canonical_arme(arme), _norm(categorie))


def load_ranking_ids(path=None):
    """Lit le fichier d'identifiants. Renvoie (confirmés, déduits), deux dictionnaires
    {(saison, sexe, arme, catégorie): identifiant}. Un fichier absent donne deux
    dictionnaires vides : le script utilise alors la recherche du site."""
    path = path or IDS_FILE
    known, probable = {}, {}
    if not os.path.exists(path):
        print(f"  Fichier d'identifiants absent ({path}) : recherche classique uniquement.")
        return known, probable

    target = known
    with open(path, encoding="utf-8") as handle:
        for number, raw in enumerate(handle, start=1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue

            section = re.fullmatch(r"\[(.+)\]", line)
            if section:
                name = _norm(section.group(1))
                target = probable if name.startswith("deduit") else known
                continue

            match = re.fullmatch(r"(.+?)\s*:\s*(\d+)", line)
            parts = re.split(r"\s+-\s+", match.group(1)) if match else []
            if not match or len(parts) != 4:
                print(f"  ffta_ids.txt ligne {number} ignorée (format attendu : "
                      f"saison - sexe - arme - catégorie : identifiant) : {raw.strip()[:80]}")
                continue

            saison, sexe, arme, categorie = (part.strip() for part in parts)
            target[_ranking_key(saison, sexe, arme, categorie)] = int(match.group(2))

    return known, probable


_RANKING_IDS = None


def _known_ranking_id():
    """(numéro, origine) pour les filtres demandés, origine = "connu" ou "déduit".
    Renvoie (None, None) si la combinaison n'est pas dans le fichier d'identifiants."""
    global _RANKING_IDS
    if _norm(FILTERS["Type"]) != "individuel" or _norm(FILTERS["Discipline"]) != "tir a 18m":
        return None, None

    if _RANKING_IDS is None:
        _RANKING_IDS = load_ranking_ids()
        print(f"  Identifiants chargés : {len(_RANKING_IDS[0])} confirmés, {len(_RANKING_IDS[1])} déduits")
    known, probable = _RANKING_IDS

    key = _ranking_key(FILTERS["Saison"], FILTERS["Sexe"], FILTERS["Arme"], FILTERS["Catégorie d'âge"])
    if key in known:
        return known[key], "connu"
    if key in probable:
        return probable[key], "déduit"
    return None, None


def _ids_line(ranking_id):
    """Ligne à recopier dans ffta_ids.txt pour les filtres demandés."""
    categorie = FILTERS["Catégorie d'âge"]
    return f"{FILTERS['Saison']} - {FILTERS['Sexe']} - {FILTERS['Arme']} - {categorie} : {ranking_id}"


def _parse_ids_line(raw):
    """(clé, identifiant) d'une ligne du fichier d'identifiants, ou None."""
    line = raw.split("#", 1)[0].strip()
    match = re.fullmatch(r"(.+?)\s*:\s*(\d+)", line)
    parts = re.split(r"\s+-\s+", match.group(1)) if match else []
    if not match or len(parts) != 4:
        return None
    return _ranking_key(*(part.strip() for part in parts)), int(match.group(2))


def save_ranking_id(ranking_id, reason):
    """Enregistre (ou corrige) l'identifiant des filtres demandés dans la section
    [confirmes] du fichier d'identifiants. Désactivable avec FFTA_IDS_AUTOSAVE=0."""
    if os.environ.get("FFTA_IDS_AUTOSAVE", "1") == "0":
        return

    try:
        _write_ranking_id(ranking_id, reason)
    except OSError as exc:
        print(f"  Enregistrement dans ffta_ids.txt impossible ({exc}) : {_ids_line(ranking_id)}")


def _write_ranking_id(ranking_id, reason):
    key = _ranking_key(FILTERS["Saison"], FILTERS["Sexe"], FILTERS["Arme"], FILTERS["Catégorie d'âge"])
    new_line = f"{_ids_line(ranking_id)}   # {reason} le {time.strftime('%Y-%m-%d')}"

    lines = []
    if os.path.exists(IDS_FILE):
        with open(IDS_FILE, encoding="utf-8") as handle:
            lines = handle.read().splitlines()

    # On retire toute ligne existante pour cette combinaison (confirmée ou déduite).
    lines = [raw for raw in lines
             if not (_parse_ids_line(raw) and _parse_ids_line(raw)[0] == key)]

    def is_header(raw, prefix=None):
        found = re.fullmatch(r"\s*\[(.+)\]\s*", raw)
        return bool(found) and (prefix is None or _norm(found.group(1)).startswith(prefix))

    start = next((i for i, raw in enumerate(lines) if is_header(raw, "confirm")), None)
    if start is None:
        lines = ["[confirmes]"] + lines
        start = 0
    end = next((i for i in range(start + 1, len(lines)) if is_header(lines[i])), len(lines))

    # Place la ligne après la dernière entrée de même saison et même arme,
    # sinon après la dernière entrée de la section.
    last_entry = start
    last_same_group = None
    for i in range(start + 1, end):
        parsed = _parse_ids_line(lines[i])
        if parsed:
            last_entry = i
            if parsed[0][0] == key[0] and parsed[0][2] == key[2]:
                last_same_group = i
    position = (last_same_group if last_same_group is not None else last_entry) + 1
    lines.insert(position, new_line)

    temporary = IDS_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    os.replace(temporary, IDS_FILE)

    global _RANKING_IDS
    _RANKING_IDS = None  # le prochain accès relira le fichier
    print(f"  Identifiant {reason} dans ffta_ids.txt : {_ids_line(ranking_id)}")


def _ranking_page_check(page):
    """Vérifie que la page ouverte est bien le classement demandé.
    Renvoie (ok, détail). On s'appuie d'abord sur le titre de la page, de la forme
    « Classement National Tir à 18m - U18 Homme Arc à Poulies 2026 - 154 archers » :
    il doit correspondre exactement (une page regroupée « U18-U21 » est donc refusée).
    Si ce titre est introuvable, on vérifie au moins la présence de la saison, du sexe,
    de la catégorie et de l'arme dans le texte (hors listes déroulantes)."""
    ignored = {"select", "option", "script", "style"}
    text = re.sub(r"\s+", " ", " ".join(
        str(node) for node in page.find_all(string=True)
        if node.parent is not None and node.parent.name not in ignored
    ))

    heading = re.search(r"Classement National\s+(.+?)\s+-\s+(.+?)\s+-\s+[\d\s]+archers?", text, re.I)
    if heading:
        category = FILTERS["Catégorie d'âge"]
        expected = f"{category} {FILTERS['Sexe']} {FILTERS['Arme']} {FILTERS['Saison']}"
        same_label = _norm(heading.group(2)) == _norm(expected)
        same_discipline = _norm(heading.group(1)) == _norm(FILTERS["Discipline"])
        return same_label and same_discipline, heading.group(0)[:160]

    norm_text = _norm(text)
    arme = ARME_KEYWORDS.get(_norm(FILTERS["Arme"]), _norm(FILTERS["Arme"]))
    expected = [
        _norm(FILTERS["Saison"]),
        _norm(FILTERS["Sexe"]),
        _norm(FILTERS["Catégorie d'âge"]),
        arme,
    ]
    ok = all(item in norm_text for item in expected)
    return ok, "titre de classement introuvable, vérification sur les mots seuls"


def _html_table(soup, url=""):
    """Même extraction que le code navigateur : tableau Rang / Archer / Club."""
    best = None
    best_score = -1
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for i, row in enumerate(rows):
            cells = row.find_all(["td", "th"], recursive=False)
            labels = [_norm(_cell_text(c)) for c in cells]
            if not ("rang" in labels and "archer" in labels and "club" in labels):
                continue
            score = len(rows) * 10 + len(cells)
            if score > best_score:
                best_score = score
                best = (rows, i)

    title = soup.title.get_text(strip=True) if soup.title else ""
    if best is None:
        body = re.sub(r"\s+", " ", soup.get_text(" "))[:1500]
        return {"found": False, "headers": [], "rows": [], "pageTitle": title,
                "url": url, "bodyPreview": body}

    rows, header_index = best
    header_cells = rows[header_index].find_all(["td", "th"], recursive=False)
    headers = []
    for cell in header_cells:
        label = _cell_text(cell)
        try:
            colspan = int(cell.get("colspan") or 1)
        except ValueError:
            colspan = 1
        if colspan <= 1:
            headers.append(label)
        elif _norm(label) == "archer" and colspan == 2:
            headers.extend(["Licence", "Archer"])
        elif _norm(label) == "club" and colspan == 2:
            headers.extend(["Code club", "Club"])
        else:
            headers.append(label)
            headers.extend(f"{label}_{i}" for i in range(2, colspan + 1))

    nom = next((i for i, h in enumerate(headers) if _norm(h) == "nom"), -1)
    if nom >= 1:
        headers[nom] = "Archer"
        if not headers[nom - 1] or re.fullmatch(r"_\d+", headers[nom - 1]):
            headers[nom - 1] = "Licence"
    club = next((i for i, h in enumerate(headers) if _norm(h) == "club"), -1)
    if club >= 1:
        headers[club] = "Club"
        if not headers[club - 1] or re.fullmatch(r"_\d+", headers[club - 1]):
            headers[club - 1] = "Code club"

    extracted = []
    for row in rows[header_index + 1:]:
        cells = row.find_all(["td", "th"], recursive=False)
        if not cells:
            continue
        values = [_cell_text(c) for c in cells]
        if not any(v for v in values):
            continue
        extracted.append(values)

    return {
        "found": True,
        "headers": headers,
        "rows": extracted,
        "pageTitle": title,
        "url": url,
        "rowCount": len(extracted),
        "populated": any(re.fullmatch(r"\d+", v.strip()) for row in extracted for v in row),
    }


def _http_flow(scope, structure, use_known_ids=True):
    from bs4 import BeautifulSoup

    base = FFTA_URL.rsplit("/", 1)[0]
    session = requests.Session()
    session.headers.update(HTTP_HEADERS)
    origin = re.match(r"https?://[^/]+", FFTA_URL).group(0)
    steps = []
    t0 = time.time()

    def mark(text):
        line = f"{time.time() - t0:.1f}s {text}"
        steps.append(line)
        print("  [http] " + line)

    def fetch(method, url, data=None, referer=None):
        headers = {"Referer": referer or url}
        if method == "POST":
            headers["Origin"] = origin
        response = session.request(method, url, data=data, headers=headers, timeout=90)
        response.raise_for_status()
        if "charset" not in response.headers.get("Content-Type", "").lower():
            response.encoding = "utf-8"
        return response, BeautifulSoup(response.text, "html.parser")

    def search_ranking_id():
        """Retrouve le numéro du classement avec la recherche du site (3 requêtes)."""
        mark("GET classements.html")
        _, form = fetch("GET", FFTA_URL)

        def code_for(field, label, page):
            options = _select_options(page, f"search[{field}]")
            return _option_value(options, FILTERS[label], label)

        search = {
            "operation": "search",
            "urlretour": "",
            "search[Annee]": code_for("Annee", "Saison", form),
            "search[Type]": code_for("Type", "Type", form),
            "search[Sexe]": code_for("Sexe", "Sexe", form),
            "search[Discipline]": code_for("Discipline", "Discipline", form),
            "search[Catage]": code_for("Catage", "Catégorie d'âge", form),
        }

        # La liste des armes est vide tant qu'une catégorie n'a pas été choisie :
        # le navigateur recharge alors le formulaire (POST search sans "Filtrer",
        # Arme=all et Distance=all). On fait pareil pour récupérer les codes.
        mark("POST search (mise à jour des listes)")
        _, form = fetch("POST", FFTA_URL,
                        dict(search, **{"search[Arme]": "all", "search[Distance]": "all"}))
        try:
            arme = code_for("Arme", "Arme", form)
        except RuntimeError as exc:
            defaults = {"arc classique": "CL", "arc a poulies": "CO", "arc nu": "BB"}
            arme = defaults.get(_norm(FILTERS["Arme"]))
            if not arme:
                raise
            print(f"  Liste des armes vide, code par défaut utilisé : {arme} ({str(exc)[:120]})")
        search["search[Arme]"] = arme
        search["StartGen"] = "Filtrer"

        # Liste des classements : on retrouve la ligne demandée et son identifiant.
        mark("POST search")
        _, results = fetch("POST", FFTA_URL, search)
        category = FILTERS["Catégorie d'âge"]
        wanted = _norm(f"{category} {FILTERS['Sexe']} {FILTERS['Arme']} {FILTERS['Saison']}")
        discipline = _norm(FILTERS["Discipline"])
        for row in results.find_all("tr"):
            first = row.find(["td", "th"])
            if first is None:
                continue
            if wanted in _norm(_cell_text(first)) and discipline in _norm(_cell_text(row)):
                found = re.search(r"classements/(\d+)\.html", str(row))
                if found:
                    return found.group(1)

        preview = re.sub(r"\s+", " ", results.get_text(" "))[:600]
        raise RuntimeError(f"Classement « {wanted} » introuvable dans la liste. Page : {preview}")

    # Raccourci : numéro de classement déjà connu -> on ouvre directement la page.
    ranking_id = None
    soup = None
    file_id, file_source = _known_ranking_id()
    known_id, known_source = (file_id, file_source) if use_known_ids else (None, None)
    if known_id:
        mark(f"GET classement {known_source} {known_id}")
        try:
            _, page = fetch("GET", f"{base}/classements/{known_id}.html", referer=FFTA_URL)
            matches, detail = _ranking_page_check(page)
            if matches:
                ranking_id, soup = str(known_id), page
                mark(f"page vérifiée : {detail}")
                # Un numéro déduit dont le titre de page correspond exactement est confirmé.
                if known_source == "déduit" and detail.startswith("Classement National"):
                    save_ranking_id(known_id, "confirmé")
            else:
                preview = re.sub(r"\s+", " ", page.get_text(" "))[:300]
                mark(f"page {known_id} non reconnue ({detail}), recherche classique. Début : {preview}")
        except Exception as exc:  # noqa: BLE001
            mark(f"page {known_id} inaccessible ({str(exc)[:120]}), recherche classique")

    if ranking_id is None:
        ranking_id = search_ranking_id()
        mark(f"GET classement {ranking_id}")
        _, soup = fetch("GET", f"{base}/classements/{ranking_id}.html", referer=FFTA_URL)
        # L'identifiant trouvé par la recherche n'est enregistré que si le titre de la
        # page correspond bien au classement demandé.
        verified, detail = _ranking_page_check(soup)
        if verified and detail.startswith("Classement National"):
            if not file_id:
                save_ranking_id(ranking_id, "ajouté")
            elif str(file_id) != ranking_id:
                print(f"  Numéro {file_source} {file_id} périmé ou faux, remplacé par {ranking_id}.")
                save_ranking_id(ranking_id, "corrigé")
            elif file_source == "déduit":
                save_ranking_id(ranking_id, "confirmé")
        elif file_id and str(file_id) != ranking_id:
            print(f"  ATTENTION : numéro {file_source} {file_id} différent du numéro trouvé {ranking_id}"
                  f" (page non vérifiable, fichier non modifié) : {_ids_line(ranking_id)}")
        elif not file_id:
            print(f"  Ligne à ajouter dans ffta_ids.txt (page non vérifiable) : {_ids_line(ranking_id)}")

    ranking_url = f"{base}/classements/{ranking_id}.html"

    def hidden(soup_):
        node = soup_.find("input", attrs={"name": "urlretour"})
        return node.get("value", "") if node is not None else ""

    def post(fields, label):
        nonlocal soup
        data = {"operation": fields["operation"], "urlretour": hidden(soup)}
        data.update({k: v for k, v in fields.items() if k != "operation"})
        mark(label)
        _, soup = fetch("POST", ranking_url, data, referer=ranking_url)

    if scope != "federation":
        # Niveau régional (toujours nécessaire, même pour le départemental).
        post({"operation": "enregistrer", "search[Pers]": "LIG", "search[oldPers]": "FED"},
             "POST niveau régional")
        region = _structure_value(
            _select_options(soup, "search[Struc]"),
            STRUCTURES["regional"] if scope == "departemental" else structure,
        )
        post({"operation": "filtre", "search[Pers]": "LIG", "search[oldPers]": "LIG",
              "search[Struc]": region, "StartGen": "Filtrer"},
             f"POST filtre régional {region}")

        if scope == "departemental":
            post({"operation": "enregistrer", "search[Pers]": "DEP", "search[oldPers]": "LIG",
                  "search[Struc]": region}, "POST niveau départemental")
            dept = _structure_value(_select_options(soup, "search[Struc]"), structure)
            post({"operation": "filtre", "search[Pers]": "DEP", "search[oldPers]": "DEP",
                  "search[Struc]": dept, "StartGen": "Filtrer"},
                 f"POST filtre départemental {dept}")

    table = _html_table(soup, ranking_url)
    mark(f"tableau : {table.get('rowCount', 0)} lignes")
    if not table.get("found") or not table.get("rows"):
        raise RuntimeError("Tableau introuvable ou vide : " + str(table.get("bodyPreview", ""))[:600])

    return {
        "type": "application/json",
        "data": {
            "scope": scope,
            "structure": structure,
            "filters": FILTERS,
            "table": table,
            "steps": steps,
        },
    }


def call_http(scope, structure=""):
    """Récupère un classement par requêtes HTTP directes (2 tentatives).
    Renvoie None en cas d'échec."""
    try:
        import bs4  # noqa: F401
    except ImportError:
        print("  Mode HTTP indisponible : beautifulsoup4 non installé.")
        return None

    print(f"\n--- HTTP direct : {scope} ---")
    if structure:
        print("Structure :", structure)

    for attempt in range(1, 3):
        print(f"  Exécution HTTP : tentative {attempt}/2")
        try:
            # Le raccourci par numéro connu n'est tenté qu'au premier essai.
            return _http_flow(scope, structure, use_known_ids=(attempt == 1))
        except Exception as exc:  # noqa: BLE001 - on veut tout attraper ici
            print(f"  Échec HTTP : {str(exc)[:1500]}")
            if attempt < 2:
                time.sleep(10)

    return None


def fetch_ranking(scope, structure=""):
    """Récupère un niveau du classement FFTA par requêtes HTTP directes."""
    result = call_http(scope, structure)
    if result is None:
        print(f"ERREUR : impossible de récupérer le niveau « {scope} » (voir les détails ci-dessus).")
        sys.exit(1)
    return finalize_result(result)


def finalize_result(result):
    data = result.get("data", result)
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]

    table = data.get("table", {}) if isinstance(data, dict) else {}
    rows = table.get("rows", []) if isinstance(table, dict) else []
    if not table.get("found") or not rows:
        print("ERREUR : tableau introuvable ou vide.")
        print(json.dumps(data, ensure_ascii=False)[:5000])
        sys.exit(1)

    print("Lignes récupérées :", table.get("rowCount", len(rows)))
    return data


def write_raw(scope, data):
    """Sauvegarde temporairement un classement brut en JSON."""
    os.makedirs(RAW_DIR, exist_ok=True)
    json_path = os.path.join(RAW_DIR, f"{FILE_PREFIX}_{scope}.json")

    # On ajoute les informations de contexte pour rendre le fichier exploitable
    # plus tard comme archive historique.
    data["metadata"] = {
        "date_recuperation_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "saison": FILTERS["Saison"],
        "type": FILTERS["Type"],
        "sexe": FILTERS["Sexe"],
        "discipline": FILTERS["Discipline"],
        "categorie": FILTERS["Catégorie d'âge"],
        "arme": FILTERS["Arme"],
        "niveau": scope,
        "structure": data.get("structure", ""),
    }

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print("  JSON :", json_path)


def parse_table(data):
    """Parse les lignes FFTA sans dépendre d'un alignement parfait des cellules.

    FFTA utilise des en-têtes groupés (Archer et Club) et certaines lignes HTML
    peuvent contenir des cellules supplémentaires/vides. On repère donc les
    éléments structurants directement dans la ligne : licence FFTA, code club,
    puis les valeurs de classement. Cela évite notamment de tronquer un nom
    comme ``DESEMERY ALEXANDRE`` en ``ALEXANDRE``.
    """
    table = data.get("table", {})
    headers = table.get("headers", [])
    rows = table.get("rows", [])

    def clean(value):
        return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()

    def is_licence(value):
        return bool(re.fullmatch(r"\d{7}[A-Z]", clean(value), re.I))

    def split_licence(value):
        value = clean(value)
        m = re.match(r"^(\d{7}[A-Z])(?:\s+(.+))?$", value, re.I)
        if m:
            return m.group(1), clean(m.group(2) or "")
        return "", value

    def split_club(value):
        value = clean(value)
        m = re.match(r"^(\d{5,8})(?:\s+(.+))?$", value)
        if m:
            return m.group(1), clean(m.group(2) or "")
        return "", value

    def header_index(name):
        wanted = normalize(name)
        for i, h in enumerate(headers):
            if normalize(h) == wanted:
                return i
        return -1

    def first_numeric_or_text(values, names):
        for name in names:
            i = header_index(name)
            if 0 <= i < len(values):
                value = clean(values[i])
                if value:
                    return value
        return ""

    parsed = []
    for values in rows:
        cells = [clean(v) for v in values]
        if not any(cells):
            continue

        licence = ""
        nom = ""
        licence_idx = -1

        # 1) Cherche une cellule contenant exactement la licence FFTA.
        for i, value in enumerate(cells):
            if is_licence(value):
                licence = value.upper()
                licence_idx = i
                break

        # 2) Si la licence et le nom sont dans la même cellule, les séparer.
        if licence_idx < 0:
            for i, value in enumerate(cells):
                lic, name = split_licence(value)
                if lic:
                    licence = lic.upper()
                    nom = name
                    licence_idx = i
                    break

        # 3) Le nom est la prochaine cellule non vide après la licence, sauf
        # si cette cellule est déjà un code club.
        club_idx = -1
        code_club = ""
        ville = ""
        for i, value in enumerate(cells):
            code, city = split_club(value)
            if code:
                club_idx = i
                code_club = code
                ville = city
                break

        if not nom and licence_idx >= 0:
            for i in range(licence_idx + 1, len(cells)):
                value = cells[i]
                if not value:
                    continue
                code, _ = split_club(value)
                if code:
                    break
                # Les colonnes de rang/points sont numériques ; un nom FFTA
                # est conservé tel quel, y compris NOM PRENOM.
                if not re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?", value):
                    nom = value
                    break

        # Secours par en-tête lorsque le HTML est parfaitement aligné.
        if not licence:
            i = header_index("Licence")
            if 0 <= i < len(cells):
                licence, inline_name = split_licence(cells[i])
                licence = licence.upper()
                if inline_name and not nom:
                    nom = inline_name

        if not nom:
            i = header_index("Archer")
            if 0 <= i < len(cells):
                lic, inline_name = split_licence(cells[i])
                if lic:
                    licence = licence or lic.upper()
                    nom = inline_name
                else:
                    nom = cells[i]

        if not code_club:
            i = header_index("Code club")
            if 0 <= i < len(cells):
                code_club, inline_city = split_club(cells[i])
                ville = ville or inline_city

        if not ville:
            i = header_index("Club")
            if 0 <= i < len(cells):
                code, inline_city = split_club(cells[i])
                if code:
                    code_club = code_club or code
                    ville = inline_city
                else:
                    ville = cells[i]

        parsed.append({
            "rang": first_numeric_or_text(cells, ["Rang"]),
            "licence": licence,
            "nom": nom,
            "code_club": code_club,
            "ville": ville,
            "s1": first_numeric_or_text(cells, ["S1"]),
            "s2": first_numeric_or_text(cells, ["S2"]),
            "s3": first_numeric_or_text(cells, ["S3"]),
            "moyenne": first_numeric_or_text(cells, ["Moy.", "Moyenne"]),
        })

    return parsed

def index_by_licence(rows):
    result = {}
    for row in rows:
        key = normalize(row.get("licence", ""))
        if key:
            result[key] = row
    return result


def merge_final():
    """Consolide l'intégralité des trois classements FFTA.

    Chaque licence présente dans au moins un des trois niveaux est conservée.
    Les trois rangs sont fusionnés sur la licence, sans filtre de club ou de ville.
    """
    required = ["federation", "regional", "departemental"]
    raw_data = {}

    for scope in required:
        path = os.path.join(RAW_DIR, f"{FILE_PREFIX}_{scope}.json")
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            print(f"ERREUR : fichier brut manquant : {path}")
            sys.exit(1)
        with open(path, encoding="utf-8") as f:
            raw_data[scope] = json.load(f)

    parsed = {scope: parse_table(raw_data[scope]) for scope in required}
    indexes = {scope: index_by_licence(parsed[scope]) for scope in required}

    # Union de toutes les licences présentes dans les trois classements.
    licences = set()
    for scope in required:
        licences.update(indexes[scope].keys())

    final_rows = []
    for key in licences:
        n = indexes["federation"].get(key, {})
        r = indexes["regional"].get(key, {})
        d = indexes["departemental"].get(key, {})

        # La fédération est prioritaire pour les informations générales,
        # puis le régional et enfin le départemental si elles n'existent pas.
        sources = (n, r, d)

        def first_value(field):
            for source in sources:
                value = str(source.get(field, "")).strip()
                if value:
                    return value
            return ""

        final_rows.append({
            "Rang Nat.": n.get("rang", ""),
            "Rang Reg.": r.get("rang", ""),
            "Rang Dep.": d.get("rang", ""),
            "Licence": first_value("licence"),
            "Nom": first_value("nom"),
            "Club": first_value("code_club"),
            "Ville": first_value("ville"),
            "S1": first_value("s1"),
            "S2": first_value("s2"),
            "S3": first_value("s3"),
            "Moy.": first_value("moyenne"),
        })

    def rank_num(value):
        m = re.search(r"\d+", str(value or ""))
        return int(m.group()) if m else 999999

    final_rows.sort(key=lambda row: (
        rank_num(row["Rang Nat."]),
        rank_num(row["Rang Reg."]),
        rank_num(row["Rang Dep."]),
        row["Nom"].upper(),
    ))

    # Nombre de lignes récupérées pour chaque niveau FFTA.
    lignes_recuperees = {}
    for scope in required:
        table = raw_data[scope].get("table", {})
        lignes_recuperees[scope] = table.get(
            "rowCount",
            len(table.get("rows", []))
        )

    final_json = {
        "source": FFTA_URL,
        "filtres": FILTERS,
        "structures": STRUCTURES,
        "lignes_recuperees": lignes_recuperees,
        "classement": final_rows,
    }

    final_json_path = os.path.join(FINAL_DIR, f"{FILE_PREFIX}.json")

    with open(final_json_path, "w", encoding="utf-8") as f:
        json.dump(final_json, f, ensure_ascii=False, indent=2)

    print("\n=== CONSOLIDATION TERMINÉE ===")
    print("Licences consolidées :", len(final_rows))
    print("Fédération :", len(parsed["federation"]))
    print("Régional :", len(parsed["regional"]))
    print("Départemental :", len(parsed["departemental"]))
    print("JSON final :", final_json_path)


def run_scope(scope):
    structure = STRUCTURES.get(scope, "")
    data = fetch_ranking(scope, structure)
    write_raw(scope, data)


def run_all_scopes():
    """Récupère les trois niveaux FFTA pour la combinaison courante."""
    import time

    os.makedirs(RAW_DIR, exist_ok=True)
    for index, scope in enumerate(["federation", "regional", "departemental"]):
        if index > 0:
            time.sleep(2)
        run_scope(scope)


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Récupère trois classements FFTA puis les consolide en un JSON final."
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help="Consolide les trois JSON temporaires sans interroger la FFTA."
    )
    parser.add_argument(
        "--scope",
        choices=["federation", "regional", "departemental"],
        help="Récupère uniquement le niveau FFTA demandé."
    )
    args = parser.parse_args()

    if args.merge and args.scope:
        parser.error("--merge et --scope ne peuvent pas être utilisés ensemble.")

    if args.merge:
        merge_final()
        return

    if args.scope:
        run_scope(args.scope)
        return

    run_all_scopes()
    merge_final()
if __name__ == "__main__":
    main()
