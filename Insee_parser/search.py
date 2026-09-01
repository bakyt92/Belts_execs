#!/usr/bin/env python3
"""
Command-line search over French company data.

Examples:
    ./search.py --activite 56.10A --departement 93
    ./search.py --mot boulangerie --code-postal 93500 --limit 20
    ./search.py --nom "buffalo grill" --format json
    ./search.py --activite 56.10A --departement 93 --etablissements
    ./search.py --naf boulangerie          # just resolve words to codes
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys

from sirene_client import (
    ApiError,
    NafTable,
    RechercheClient,
    SireneClient,
    sirene_query,
)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def rows_from_units(results: list[dict]) -> list[dict]:
    """One row per legal unit, showing its head office."""
    out = []
    for r in results:
        siege = r.get("siege") or {}
        out.append(
            {
                "siren": r.get("siren", ""),
                "nom": r.get("nom_complet", ""),
                "naf": r.get("activite_principale", ""),
                "cp": siege.get("code_postal", ""),
                "commune": siege.get("libelle_commune", ""),
                "etabs": r.get("nombre_etablissements_ouverts", ""),
            }
        )
    return out


def rows_from_establishments(results: list[dict]) -> list[dict]:
    """
    One row per matching establishment.

    Use this when you asked a geographic question: `siege` is the head
    office, which is often outside the area you filtered on.
    """
    out = []
    for r in results:
        for e in r.get("matching_etablissements") or []:
            out.append(
                {
                    "siret": e.get("siret", ""),
                    "nom": r.get("nom_complet", ""),
                    "naf": e.get("activite_principale", ""),
                    "cp": e.get("code_postal", ""),
                    "commune": e.get("libelle_commune", ""),
                    "siege": "oui" if e.get("est_siege") else "",
                }
            )
    return out


def format_dirigeant(d: dict) -> str:
    """One director as a short string. Handles both person and company."""
    if d.get("type_dirigeant") == "personne morale":
        name = d.get("denomination") or "?"
        siren = d.get("siren")
        label = f"{name} [{siren}]" if siren else name
    else:
        label = " ".join(
            filter(None, [d.get("prenoms"), d.get("nom")])
        ) or "?"
    qualite = d.get("qualite")
    return f"{label} ({qualite})" if qualite else label


def rows_from_dirigeants(results: list[dict]) -> list[dict]:
    """
    One row per director.

    `qualite` is often 'Autre' — the RNE role string doesn't always map
    cleanly, so this tells you who is involved, not reliably who runs it.
    Query the INPI RNE directly if you need the actual role.

    Note: this is directors, not shareholders. The open recherche API does
    not expose beneficial owners; only INPI's RNE (restricted access) does.
    """
    out = []
    for r in results:
        for d in r.get("dirigeants") or []:
            is_company = d.get("type_dirigeant") == "personne morale"
            out.append(
                {
                    "siren": r.get("siren", ""),
                    "societe": r.get("nom_complet", ""),
                    "dirigeant": (
                        d.get("denomination", "")
                        if is_company
                        else " ".join(
                            filter(None, [d.get("prenoms"), d.get("nom")])
                        )
                    ),
                    "qualite": d.get("qualite") or "",
                    "type": "morale" if is_company else "physique",
                    "naissance": d.get("annee_de_naissance") or "",
                    "siren_dir": d.get("siren") or "" if is_company else "",
                }
            )
    return out


def print_table(rows: list[dict], stream=sys.stdout) -> None:
    if not rows:
        print("no results", file=stream)
        return
    cols = list(rows[0].keys())
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    widths = {c: min(w, 45) for c, w in widths.items()}

    header = "  ".join(c.upper().ljust(widths[c]) for c in cols)
    print(header, file=stream)
    print("  ".join("-" * widths[c] for c in cols), file=stream)
    for r in rows:
        print(
            "  ".join(str(r[c])[: widths[c]].ljust(widths[c]) for c in cols),
            file=stream,
        )


def emit(rows: list[dict], fmt: str) -> None:
    if fmt == "json":
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
        print()
    elif fmt == "csv":
        if not rows:
            return
        writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    else:
        print_table(rows)


# --------------------------------------------------------------------------
# Arguments
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="search.py",
        description="Search French companies by activity, location or name.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    what = p.add_argument_group("what to search for")
    what.add_argument("--nom", "-n", help="free-text name, address or director")
    what.add_argument(
        "--activite",
        "-a",
        help="NAF code(s), comma-separated (e.g. 56.10A,56.10C)",
    )
    what.add_argument(
        "--mot",
        "-m",
        help="activity in words (e.g. boulangerie) — resolved to NAF codes",
    )
    what.add_argument(
        "--section",
        "-s",
        help="NAF section letter (A-U), e.g. I for hébergement/restauration",
    )

    where = p.add_argument_group("where")
    where.add_argument("--departement", "-d", help="département code, e.g. 93")
    where.add_argument("--code-postal", "-p", help="postcode, e.g. 93500")
    where.add_argument("--commune", help="INSEE commune code, e.g. 93055")

    how = p.add_argument_group("how")
    how.add_argument(
        "--etat",
        choices=["A", "C", "all"],
        default="A",
        help="A=active (default), C=ceased, all=both",
    )
    how.add_argument(
        "--etablissements",
        "-e",
        action="store_true",
        help="one row per matching establishment instead of per company",
    )
    how.add_argument(
        "--dirigeants",
        action="store_true",
        help="one row per director (source: INPI RNE, via the open API)",
    )
    how.add_argument(
        "--with-dirigeants",
        action="store_true",
        help="keep one row per company, adding a directors column",
    )
    how.add_argument(
        "--limit",
        "-l",
        type=int,
        default=25,
        help="max companies fetched (default 25); director/establishment "
        "rows can exceed this since one company yields several",
    )
    how.add_argument(
        "--format", "-f", choices=["table", "json", "csv"], default="table"
    )
    how.add_argument(
        "--source",
        choices=["recherche", "sirene"],
        default="recherche",
        help="recherche = open API (default); sirene = authenticated INSEE",
    )
    how.add_argument(
        "--naf-csv",
        default=os.getenv("NAF_CSV", "naf.csv"),
        help="path to the NAF reference CSV (for --mot / --section)",
    )
    how.add_argument("--count", action="store_true", help="print the total only")
    how.add_argument(
        "--naf",
        metavar="TERM",
        help="resolve words to NAF codes and exit (no company search)",
    )
    return p


def load_naf(path: str) -> NafTable:
    try:
        return NafTable.from_csv(path)
    except FileNotFoundError:
        sys.exit(
            f"NAF reference file not found: {path}\n"
            "Download it from data.gouv.fr ('Nomenclature d'activités française') "
            "or set --naf-csv / NAF_CSV."
        )


# --------------------------------------------------------------------------
# Search paths
# --------------------------------------------------------------------------


def resolve_codes(args: argparse.Namespace) -> list[str] | None:
    """Turn --activite / --mot / --section into a list of NAF codes."""
    if args.activite:
        return [NafTable.normalise(c) for c in args.activite.split(",")]

    if args.mot:
        naf = load_naf(args.naf_csv)
        hits = naf.search(args.mot, limit=10)
        if not hits:
            sys.exit(f"no NAF code matches '{args.mot}'")
        print(f"# '{args.mot}' matched {len(hits)} code(s):", file=sys.stderr)
        for e in hits:
            print(f"#   {e.code}  {e.libelle}", file=sys.stderr)
        return [e.code for e in hits]

    if args.section:
        naf = load_naf(args.naf_csv)
        codes = naf.section_codes(args.section)
        if not codes:
            sys.exit(f"no codes for section '{args.section}'")
        print(f"# section {args.section.upper()}: {len(codes)} codes", file=sys.stderr)
        return codes

    return None


def run_recherche(args: argparse.Namespace, codes: list[str] | None) -> None:
    client = RechercheClient()

    include = ["siege"]
    if args.etablissements:
        include.append("matching_etablissements")
    if args.dirigeants or args.with_dirigeants:
        include.append("dirigeants")

    kwargs = {
        "q": args.nom,
        "activite_principale": codes,
        "departement": args.departement,
        "code_postal": args.code_postal,
        "code_commune": args.commune,
        "etat_administratif": None if args.etat == "all" else args.etat,
        "minimal": True,
        "include": include,
    }
    kwargs = {k: v for k, v in kwargs.items() if v is not None}

    if args.count:
        payload = client.search(per_page=1, **kwargs)
        print(payload.get("total_results", 0))
        return

    probe = client.search(per_page=1, **kwargs)
    total = probe.get("total_results", 0)
    if total > args.limit:
        print(f"# {total} companies match, showing {args.limit}", file=sys.stderr)

    results = []
    for item in client.iter_all(per_page=25, **kwargs):
        if len(results) >= args.limit:
            break
        results.append(item)

    if args.dirigeants:
        rows = rows_from_dirigeants(results)
    elif args.etablissements:
        rows = rows_from_establishments(results)
    else:
        rows = rows_from_units(results)
        if args.with_dirigeants:
            for row, r in zip(rows, results):
                dirs = r.get("dirigeants") or []
                row["dirigeants"] = "; ".join(format_dirigeant(d) for d in dirs[:3])
                if len(dirs) > 3:
                    row["dirigeants"] += f" (+{len(dirs) - 3})"

    emit(rows, args.format)


def run_sirene(args: argparse.Namespace, codes: list[str] | None) -> None:
    if not os.getenv("INSEE_CLIENT_ID"):
        sys.exit("--source sirene needs INSEE_CLIENT_ID and INSEE_CLIENT_SECRET")

    client = SireneClient()
    q = sirene_query(
        activitePrincipaleEtablissement=codes,
        etatAdministratifEtablissement=None if args.etat == "all" else args.etat,
        codePostalEtablissement=args.code_postal,
        codeCommuneEtablissement=args.commune,
    )
    if not q:
        sys.exit("sirene needs at least one filter (--activite, --code-postal, ...)")

    if args.count:
        print(client.count(q))
        return

    rows = []
    for etab in client.search_etablissements(q, max_results=args.limit):
        ul = etab.get("uniteLegale", {})
        adr = etab.get("adresseEtablissement", {})
        rows.append(
            {
                "siret": etab.get("siret", ""),
                "nom": ul.get("denominationUniteLegale")
                or " ".join(
                    filter(None, [ul.get("prenom1UniteLegale"), ul.get("nomUniteLegale")])
                ),
                "naf": ul.get("activitePrincipaleUniteLegale", ""),
                "cp": adr.get("codePostalEtablissement", ""),
                "commune": adr.get("libelleCommuneEtablissement", ""),
            }
        )
    emit(rows, args.format)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    # --naf: code lookup only, no company search
    if args.naf:
        naf = load_naf(args.naf_csv)
        hits = naf.search(args.naf, limit=25)
        emit([{"code": e.code, "libelle": e.libelle} for e in hits], args.format)
        return

    if not any([args.nom, args.activite, args.mot, args.section]):
        parser.error("give at least one of --nom, --activite, --mot or --section")

    if args.source == "sirene" and (args.dirigeants or args.with_dirigeants):
        parser.error(
            "--dirigeants / --with-dirigeants need --source recherche "
            "(the authenticated Sirene API has no director data)"
        )

    codes = resolve_codes(args)

    try:
        if args.source == "sirene":
            run_sirene(args, codes)
        else:
            run_recherche(args, codes)
    except ApiError as exc:
        sys.exit(f"API error: {exc}")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()